// Pure background-tab LTI resolver (M2.7 zero-paste discovery, Task 2).
//
// ARCHITECTURE RULE (same as sync-engine.ts): this file touches NO chrome.*
// APIs. Every side effect -- opening/reading/closing a tab, waiting for a
// navigation to settle, talking to the backend, reporting progress -- is an
// injected dependency (LtiResolverDeps) described by a structural interface,
// so vitest can drive the whole loop with plain fakes (including a fake
// TabDriver whose onNavigationSettled is timed with vi.useFakeTimers()) and
// background.ts (the thin chrome adapter) can pass a real implementation
// built on chrome.tabs.create/get/remove and the onUpdated/onRemoved events.
//
// Split of responsibility for "settle": the DECISION FLOW -- what quietMs/
// timeoutMs values to use, and what to do once a navigation is deemed
// settled (read currentUrl, close the tab, POST the resolution) -- lives
// here, in the pure module, so it's covered by ordinary vitest assertions.
// The actual mechanics of detecting "settled" (watching chrome.tabs.onUpdated
// for a quiet period after a `status: "complete"`, or hitting the hard cap)
// can only be done with real chrome.tabs events, so that lives in
// background.ts's TabDriver implementation behind the single
// `onNavigationSettled(tabId, quietMs, timeoutMs): Promise<void>` call.

import type { LtiCandidate, LtiCandidatesResponse, LtiResolutionPayload, LtiResolutionResponse } from "./types";

// ---------------------------------------------------------------------------
// Injected dependency interfaces
// ---------------------------------------------------------------------------

/** The subset of BackendClient the resolver needs. */
export interface LtiResolverBackendLike {
  ltiCandidates(orgUnitId: number): Promise<LtiCandidatesResponse>;
  reportLtiResolution(payload: LtiResolutionPayload): Promise<LtiResolutionResponse>;
}

/** Everything the resolver needs from a browser tab, background.ts's only
 * job when implementing this against real chrome.tabs is to make each of
 * these four calls behave exactly as described -- the resolver itself never
 * touches chrome.tabs directly. */
export interface TabDriver {
  /** Opens `url` in a new, non-active tab and resolves with its tab id. */
  open(url: string): Promise<number>;
  /** The tab's current URL, or null if the tab no longer exists (e.g. the
   * user closed it). */
  currentUrl(tabId: number): Promise<string | null>;
  /** Closes the tab. Safe to call even if the tab is already gone. */
  close(tabId: number): Promise<void>;
  /** Resolves once the tab's navigation is "settled": a `status: "complete"`
   * update followed by `quietMs` with no further update, or `timeoutMs`
   * elapsed since open, whichever comes first. Also resolves (early) if the
   * tab is removed before either of those. Never rejects for either of
   * those ordinary outcomes. */
  onNavigationSettled(tabId: number, quietMs: number, timeoutMs: number): Promise<void>;
}

export interface LtiResolverDeps {
  backend: LtiResolverBackendLike;
  tabs: TabDriver;
  /** Notified once per candidate as it finishes (not before the first one
   * finishes -- there's nothing to report before that), same "progress
   * ticks as work completes" shape sync-engine.ts's onProgress uses. */
  onProgress(p: { done: number; total: number }): void;
}

export interface LtiResolveSummary {
  resolved: number;
  unrecognized: number;
  failed: number;
  total: number;
}

type LtiOutcome = "resolved" | "unrecognized" | "failed";

// ---------------------------------------------------------------------------
// Settle policy (the values are the "policy" the header comment refers to)
// ---------------------------------------------------------------------------

export const LTI_SETTLE_QUIET_MS = 2000;
export const LTI_SETTLE_TIMEOUT_MS = 25000;

/** Never more than this many background tabs per sync -- the whole point of
 * this feature is to be unobtrusive. Candidates beyond the cap are simply
 * left alone: only resolved/unrecognized rows drop out of the backend's
 * candidate list, so the next sync's run picks them up. */
export const LTI_MAX_CANDIDATES_PER_RUN = 8;

// ---------------------------------------------------------------------------
// resolveLtiCandidates
// ---------------------------------------------------------------------------

/** Fetches the still-unresolved LTI candidates for one course and works
 * through them sequentially -- never in parallel; opening several
 * background tabs at once against a live D2L session is exactly the kind
 * of noisy behavior this feature exists to avoid. A failure fetching the
 * candidate list itself propagates uncaught (the caller -- background.ts --
 * wraps the whole call so a resolver failure never turns a completed sync
 * into a failed one); every per-candidate failure past that point is caught
 * and counted, never aborting the loop. */
export async function resolveLtiCandidates(
  deps: LtiResolverDeps,
  origin: string,
  orgUnitId: number,
): Promise<LtiResolveSummary> {
  const { candidates } = await deps.backend.ltiCandidates(orgUnitId);

  const batch = candidates.slice(0, LTI_MAX_CANDIDATES_PER_RUN);
  const skipped = candidates.length - batch.length;
  if (skipped > 0) {
    console.warn(
      `lti-resolver: capping at ${LTI_MAX_CANDIDATES_PER_RUN} candidates this run ` +
        `(${skipped} left for the next sync)`,
    );
  }

  const summary: LtiResolveSummary = { resolved: 0, unrecognized: 0, failed: 0, total: batch.length };

  let done = 0;
  for (const candidate of batch) {
    const outcome = await resolveOneCandidate(deps, origin, orgUnitId, candidate);
    summary[outcome] += 1;
    done += 1;
    deps.onProgress({ done, total: summary.total });
  }

  return summary;
}

// ---------------------------------------------------------------------------
// Per-candidate pipeline
// ---------------------------------------------------------------------------

async function resolveOneCandidate(
  deps: LtiResolverDeps,
  origin: string,
  orgUnitId: number,
  candidate: LtiCandidate,
): Promise<LtiOutcome> {
  const absoluteUrl = resolveOnOriginUrl(candidate.launchUrl, origin);
  if (absoluteUrl === null) {
    // Safety gate (docs/plan-m27.md Global Constraints: "The extension only
    // ever opens launch URLs on the tenant origin it is syncing"). This
    // classification into 'failed' happens entirely client-side -- unlike
    // the bounced-back-to-origin case below, there's no ambiguity here for
    // the backend to adjudicate, so tabs.open is never even called.
    return reportResolution(deps, orgUnitId, candidate.materialId, null, "launch URL not on tenant origin");
  }

  let tabId: number | null = null;
  let finalUrl: string | null = null;
  let error: string | null = null;

  try {
    tabId = await deps.tabs.open(absoluteUrl);
    await deps.tabs.onNavigationSettled(tabId, LTI_SETTLE_QUIET_MS, LTI_SETTLE_TIMEOUT_MS);
    finalUrl = await deps.tabs.currentUrl(tabId);
    if (finalUrl === null) {
      // The tab is gone once settled -- most likely the user closed it
      // before the navigation ever finished.
      error = "tab closed";
    }
  } catch (err) {
    finalUrl = null;
    error = errorMessage(err);
  } finally {
    // Never leak a tab, including on a hard-cap timeout -- close is
    // attempted even when nothing above succeeded, and even when the tab
    // is suspected already gone (best-effort; see the catch below).
    if (tabId !== null) {
      try {
        await deps.tabs.close(tabId);
      } catch {
        // Nothing more useful to do if closing an already-gone tab fails.
      }
    }
  }

  return reportResolution(deps, orgUnitId, candidate.materialId, finalUrl, error);
}

/** POSTs one candidate's outcome and maps the response (or a backend/network
 * failure) onto this run's local summary bucket. A failure here -- expand
 * errors on the backend surface as a non-2xx per Task 1, same as any other
 * network error -- never aborts resolveLtiCandidates's loop; the candidate
 * is simply counted as failed locally and, having no lti_resolutions row at
 * all (or, for a genuine backend 'failed' status, a retryable one), gets
 * offered again next sync. */
async function reportResolution(
  deps: LtiResolverDeps,
  orgUnitId: number,
  materialId: number,
  finalUrl: string | null,
  error: string | null,
): Promise<LtiOutcome> {
  try {
    const response = await deps.backend.reportLtiResolution({ orgUnitId, materialId, finalUrl, error });
    return response.status;
  } catch {
    return "failed";
  }
}

// ---------------------------------------------------------------------------
// URL helpers
// ---------------------------------------------------------------------------

/** Resolves `launchUrl` (absolute or relative) against `origin` and returns
 * its string form, or null if it can't be parsed at all or doesn't land on
 * `origin`. Compares `URL.origin` rather than a raw string prefix test --
 * `.origin` always normalizes to scheme://host[:port] with nothing after
 * it, so a lookalike host like "https://tenant.example.evil.com" can never
 * pass a naive `startsWith(origin)` check that this deliberately avoids. */
function resolveOnOriginUrl(launchUrl: string, origin: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(launchUrl, origin);
  } catch {
    return null;
  }
  if (parsed.origin !== origin) return null;
  return parsed.toString();
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** Normalizes a tenant origin string to `URL.origin`'s canonical form
 * (lowercase host, no trailing slash, default port stripped) -- exactly
 * what `resolveOnOriginUrl` above compares `launchUrl`'s parsed origin
 * against. `origin` here comes from background.ts, which reads it back out
 * of the popup message or `chrome.storage`; it is NOT guaranteed to already
 * be canonical (e.g. `https://Tenant.edu`, `https://tenant.edu/`, or
 * `https://tenant.edu:443` are all plausible stored forms that a browser
 * URL bar or a hand-typed tenant URL could produce), and a mismatch here
 * fails EVERY candidate closed, forever, with no visible error. Exported so
 * background.ts can call it at the adapter seam -- before `origin` ever
 * reaches `resolveLtiCandidates` -- rather than this pure module reaching
 * out to canonicalize its own input; it lives here only because, like
 * everything else in this file, it touches zero chrome.* itself and so is
 * directly testable. Falls back to `origin` unchanged if it isn't parseable
 * at all -- `resolveOnOriginUrl`'s own try/catch and mismatch check then
 * reject every candidate exactly as they do today for a badly-stored
 * origin, rather than this helper silently hiding the problem. */
export function canonicalizeOrigin(origin: string): string {
  try {
    return new URL(origin).origin;
  } catch {
    return origin;
  }
}
