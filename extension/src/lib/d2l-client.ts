// Owns ALL Brightspace (D2L Valence API) endpoint/auth/rate-limit knowledge.
// Pure fetch-based — no chrome.* APIs — so it runs both inside the MV3
// service worker and under vitest in Node. Every `/d2l/api/` path must live
// in this file; nothing downstream should hand-build one.

import type { D2LEnrollmentItem, D2LPagedResultSet, D2LVersionInfo } from "./types";

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
  /** Total attempts per request (initial try + retries) before giving up on
   * repeated 429s. Default 3. */
  maxRetries?: number;
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

/**
 * Wraps global fetch with:
 *  - a concurrency cap (at most `maxConcurrent` requests in flight),
 *  - a shared cooldown when D2L reports a low X-Rate-Limit-Remaining,
 *  - 429 handling (Retry-After if present, else jittered exponential
 *    backoff), and
 *  - immediate, non-retried failure on 401/403 (SessionExpiredError).
 */
export class RateLimitedFetcher {
  private readonly maxConcurrent: number;
  private readonly maxRetries: number;
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
    this.maxRetries = options.maxRetries ?? 3;
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
    for (let attempt = 0; attempt < this.maxRetries; attempt++) {
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

      const isLastAttempt = attempt === this.maxRetries - 1;
      if (isLastAttempt) {
        break;
      }

      const retryAfter = response.headers.get("Retry-After");
      const delayMs =
        retryAfter !== null ? Number(retryAfter) * 1000 : Math.random() * this.baseBackoffMs * 2 ** attempt;
      await this.sleepImpl(delayMs);
    }

    throw new RateLimitError(`D2L rate limit exceeded after ${this.maxRetries} attempts`);
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
   * swallowed and reported as an empty list rather than aborting the sync. */
  async news(le: string, orgUnitId: number): Promise<unknown[]> {
    try {
      const response = await this.get(`/d2l/api/le/${le}/${orgUnitId}/news/`);
      if (!response.ok) return [];
      return (await response.json()) as unknown[];
    } catch {
      return [];
    }
  }

  async dropboxFolders(le: string, orgUnitId: number): Promise<unknown[]> {
    try {
      const response = await this.get(`/d2l/api/le/${le}/${orgUnitId}/dropbox/folders/`);
      if (!response.ok) return [];
      return (await response.json()) as unknown[];
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
