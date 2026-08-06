// Owns ALL Brightspace (D2L Valence API) endpoint/auth/rate-limit knowledge.
// Pure fetch-based — no chrome.* APIs — so it runs both inside the MV3
// service worker and under vitest in Node. Every `/d2l/api/` path must live
// in this file; nothing downstream should hand-build one.

import type {
  D2LDropboxFolder,
  D2LEnrollmentItem,
  D2LNewsItem,
  D2LPagedResultSet,
  D2LVersionInfo,
  DropboxExtra,
  NewsExtra,
} from "./types";

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/** Generic D2L API failure (unexpected shape, non-ok response, etc). */
export class D2LError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "D2LError";
  }
}

/** The Brightspace session cookie is gone/expired (401/403). No retry —
 * the sync loop should tell the user to refresh their Brightspace tab. */
export class SessionExpiredError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SessionExpiredError";
  }
}

/** 429s kept coming back after exhausting all retries. */
export class RateLimitError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RateLimitError";
  }
}

// ---------------------------------------------------------------------------
// RateLimitedFetcher
// ---------------------------------------------------------------------------

export interface RateLimitedFetcherOptions {
  /** Max in-flight requests at once. Default 2. */
  maxConcurrent?: number;
  /** Total HTTP attempts per request (including the first try) before
   * giving up on repeated 429s. Default 3. */
  maxAttempts?: number;
  /** X-Rate-Limit-Remaining below this triggers a shared cooldown. Default 20. */
  minRemainingThreshold?: number;
  /** Base delay (ms) for the cooldown and the exponential backoff. Default 1000. */
  baseBackoffMs?: number;
  /** Injectable for tests; defaults to global fetch. */
  fetchImpl?: typeof fetch;
  /** Injectable for tests; defaults to a real setTimeout-based sleep. */
  sleepImpl?: (ms: number) => Promise<void>;
}

const defaultSleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/** Ceiling on any single 429 wait. A tenant is free to send
 * `Retry-After: 86400`; honoring that literally would park the sync
 * (and the service worker's alarm-driven watchdog) for a day. Two minutes
 * is long enough to be a real backoff and short enough that the user's
 * next retry still happens today. */
const MAX_RETRY_DELAY_MS = 120_000;

/**
 * `Retry-After` in milliseconds, or null if it isn't a usable delay.
 *
 * The header is allowed to be an HTTP-date rather than a delay in seconds,
 * and a proxy in front of a tenant can put anything at all there. `Number()`
 * on either gives NaN, and `sleep(NaN)` resolves on the next tick -- so a
 * single malformed header turned the backoff into a hot retry loop hammering
 * a server that just told us to slow down. Anything not a finite,
 * non-negative number of seconds is a miss, and the caller falls back to its
 * exponential backoff (the HTTP-date form included: parsing it would need a
 * clock-skew-tolerant date diff for a header D2L doesn't send).
 */
function parseRetryAfterMs(header: string | null): number | null {
  if (header === null) return null;
  const seconds = Number.parseFloat(header);
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  return seconds * 1000;
}

/**
 * Wraps global fetch with:
 *  - a concurrency cap (at most `maxConcurrent` requests in flight),
 *  - a shared cooldown when D2L reports a low X-Rate-Limit-Remaining,
 *  - 429 handling (Retry-After when it parses as a usable delay, else
 *    jittered exponential backoff -- either way capped at
 *    MAX_RETRY_DELAY_MS), and
 *  - immediate, non-retried failure on 401/403 (SessionExpiredError).
 */
export class RateLimitedFetcher {
  private readonly maxConcurrent: number;
  private readonly maxAttempts: number;
  private readonly minRemainingThreshold: number;
  private readonly baseBackoffMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly sleepImpl: (ms: number) => Promise<void>;

  private inFlight = 0;
  private readonly waiters: Array<() => void> = [];

  // Single shared cooldown: a low X-Rate-Limit-Remaining reading schedules
  // one sleep that every request in flight (or queued) awaits, rather than
  // each request accumulating its own delay.
  private cooldown: Promise<void> | null = null;

  constructor(options: RateLimitedFetcherOptions = {}) {
    this.maxConcurrent = options.maxConcurrent ?? 2;
    this.maxAttempts = options.maxAttempts ?? 3;
    this.minRemainingThreshold = options.minRemainingThreshold ?? 20;
    this.baseBackoffMs = options.baseBackoffMs ?? 1000;
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.sleepImpl = options.sleepImpl ?? defaultSleep;
  }

  async fetch(input: string, init?: RequestInit): Promise<Response> {
    await this.acquireSlot();
    try {
      return await this.requestWithRetry(input, init);
    } finally {
      this.releaseSlot();
    }
  }

  private acquireSlot(): Promise<void> {
    if (this.inFlight < this.maxConcurrent) {
      this.inFlight++;
      return Promise.resolve();
    }
    return new Promise<void>((resolve) => {
      this.waiters.push(() => {
        this.inFlight++;
        resolve();
      });
    });
  }

  private releaseSlot(): void {
    this.inFlight--;
    const next = this.waiters.shift();
    if (next) next();
  }

  private async requestWithRetry(input: string, init: RequestInit | undefined): Promise<Response> {
    for (let attempt = 0; attempt < this.maxAttempts; attempt++) {
      if (this.cooldown) {
        await this.cooldown;
      }

      const response = await this.fetchImpl(input, init);

      if (response.status === 401 || response.status === 403) {
        throw new SessionExpiredError(`D2L session expired (HTTP ${response.status})`);
      }

      this.maybeStartCooldown(response);

      if (response.status !== 429) {
        return response;
      }

      const isLastAttempt = attempt === this.maxAttempts - 1;
      if (isLastAttempt) {
        break;
      }

      const backoffMs = Math.random() * this.baseBackoffMs * 2 ** attempt;
      const retryAfterMs = parseRetryAfterMs(response.headers.get("Retry-After"));
      await this.sleepImpl(Math.min(retryAfterMs ?? backoffMs, MAX_RETRY_DELAY_MS));
    }

    throw new RateLimitError(`D2L rate limit exceeded after ${this.maxAttempts} attempts`);
  }

  private maybeStartCooldown(response: Response): void {
    const remainingHeader = response.headers.get("X-Rate-Limit-Remaining");
    if (remainingHeader === null) return;
    if (Number(remainingHeader) >= this.minRemainingThreshold) return;
    if (this.cooldown) return; // already cooling down — shared, not stacked

    const cooldown = this.sleepImpl(this.baseBackoffMs).then(() => {
      if (this.cooldown === cooldown) {
        this.cooldown = null;
      }
    });
    this.cooldown = cooldown;
  }
}

// ---------------------------------------------------------------------------
// D2LClient
// ---------------------------------------------------------------------------

const COURSE_TYPE_PATTERN = /course/i;

function isCourseEnrollment(item: D2LEnrollmentItem): boolean {
  const { Code, Name } = item.OrgUnit.Type;
  return COURSE_TYPE_PATTERN.test(Code) || COURSE_TYPE_PATTERN.test(Name);
}

// ---------------------------------------------------------------------------
// Extras reshaping — D2L PascalCase -> the backend's camelCase contract
//
// This mapping is the ONLY place the two shapes meet. Valence returns
// `{Id, Title, Body: {Text, Html}}` / `{Id, Name, CustomInstructions:
// {Text, Html}}`; the backend's /toc `extras` contract (see api/ingest.py's
// NewsExtra/DropboxExtra) is `{id, title, html}` / `{id, name,
// instructionsText}`. Forwarding the raw Valence objects would have every
// item rejected server-side.
//
// Everything is optional-chained and defaulted because these responses come
// from a tenant we don't control: a missing `Body`, a null `Title`, or a
// non-object array entry must not throw here. An item with no usable
// numeric `Id` is dropped outright -- the backend keys extras materials on
// `d2l:news:{id}` / `d2l:dropbox:{id}`, so there is nothing to store it as.
// (The backend skips unusable items too; this is the first of two layers,
// not the only one.)
// ---------------------------------------------------------------------------

function asRecord<T>(value: unknown): T | null {
  return typeof value === "object" && value !== null ? (value as T) : null;
}

function toNewsExtras(items: unknown[]): NewsExtra[] {
  const extras: NewsExtra[] = [];
  for (const raw of items) {
    const item = asRecord<D2LNewsItem>(raw);
    if (typeof item?.Id !== "number") continue;
    extras.push({ id: item.Id, title: item.Title ?? "", html: item.Body?.Html ?? "" });
  }
  return extras;
}

function toDropboxExtras(items: unknown[]): DropboxExtra[] {
  const extras: DropboxExtra[] = [];
  for (const raw of items) {
    const item = asRecord<D2LDropboxFolder>(raw);
    if (typeof item?.Id !== "number") continue;
    extras.push({
      id: item.Id,
      name: item.Name ?? "",
      instructionsText: item.CustomInstructions?.Text ?? null,
    });
  }
  return extras;
}

export class D2LClient {
  constructor(
    private readonly origin: string,
    private readonly fetcher: RateLimitedFetcher,
  ) {}

  private get(path: string): Promise<Response> {
    return this.fetcher.fetch(`${this.origin}${path}`, { credentials: "include" });
  }

  async discoverVersions(): Promise<{ lp: string; le: string }> {
    const response = await this.get("/d2l/api/versions/");
    if (!response.ok) {
      throw new D2LError(`Failed to discover D2L API versions (HTTP ${response.status})`);
    }
    const versions = (await response.json()) as D2LVersionInfo[];
    const lp = versions.find((v) => v.ProductCode === "lp");
    const le = versions.find((v) => v.ProductCode === "le");
    if (!lp || !le) {
      throw new D2LError("D2L versions response is missing the 'lp' or 'le' product");
    }
    return { lp: lp.LatestVersion, le: le.LatestVersion };
  }

  /** Also serves as the auth probe: 401/403 surfaces as SessionExpiredError. */
  async whoami(lp: string): Promise<unknown> {
    const response = await this.get(`/d2l/api/lp/${lp}/users/whoami`);
    if (!response.ok) {
      throw new D2LError(`whoami failed (HTTP ${response.status})`);
    }
    return response.json();
  }

  async myEnrollments(lp: string): Promise<D2LEnrollmentItem[]> {
    const items: D2LEnrollmentItem[] = [];
    let bookmark: string | null = null;

    for (;;) {
      const path = bookmark
        ? `/d2l/api/lp/${lp}/enrollments/myenrollments/?bookmark=${encodeURIComponent(bookmark)}`
        : `/d2l/api/lp/${lp}/enrollments/myenrollments/`;
      const response = await this.get(path);
      if (!response.ok) {
        throw new D2LError(`myenrollments failed (HTTP ${response.status})`);
      }
      const page = (await response.json()) as D2LPagedResultSet<D2LEnrollmentItem>;
      items.push(...page.Items);
      if (!page.PagingInfo.HasMoreItems) break;
      bookmark = page.PagingInfo.Bookmark;
    }

    return items.filter(isCourseEnrollment);
  }

  async courseToc(le: string, orgUnitId: number): Promise<unknown> {
    const response = await this.get(`/d2l/api/le/${le}/${orgUnitId}/content/toc`);
    if (!response.ok) {
      throw new D2LError(`courseToc failed (HTTP ${response.status})`);
    }
    return response.json();
  }

  /** Extras are optional: any failure (bad status or network error) is
   * swallowed and reported as an empty list rather than aborting the sync.
   * The returned items are already in the backend's `extras.news` shape --
   * see toNewsExtras above for why the reshaping belongs here. */
  async news(le: string, orgUnitId: number): Promise<NewsExtra[]> {
    try {
      const response = await this.get(`/d2l/api/le/${le}/${orgUnitId}/news/`);
      if (!response.ok) return [];
      const payload = await response.json();
      return Array.isArray(payload) ? toNewsExtras(payload) : [];
    } catch {
      return [];
    }
  }

  /** Same contract as `news()`: fail-soft, and already reshaped into the
   * backend's `extras.dropbox` items. */
  async dropboxFolders(le: string, orgUnitId: number): Promise<DropboxExtra[]> {
    try {
      const response = await this.get(`/d2l/api/le/${le}/${orgUnitId}/dropbox/folders/`);
      if (!response.ok) return [];
      const payload = await response.json();
      return Array.isArray(payload) ? toDropboxExtras(payload) : [];
    } catch {
      return [];
    }
  }

  /** Returns the raw Response — its body stays streamable so Task 5 can
   * pipe it straight through to the backend without buffering it here. */
  fetchTopicFile(le: string, orgUnitId: number, topicId: number): Promise<Response> {
    return this.get(`/d2l/api/le/${le}/${orgUnitId}/content/topics/${topicId}/file`);
  }
}
