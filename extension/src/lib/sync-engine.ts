// Pure orchestration for the pair -> connect -> discover -> sync loop.
//
// ARCHITECTURE RULE: this file touches NO chrome.* APIs. Every side effect
// (D2L/backend HTTP, state persistence, progress reporting) is an injected
// dependency (SyncDeps), described by structural interfaces rather than the
// concrete D2LClient/BackendClient classes -- so vitest can pass plain
// fakes with zero chrome mocking, and background.ts (the thin chrome
// adapter) can pass real client instances, which satisfy the same
// interfaces structurally. This is what makes the loop unit-testable and
// resilient to MV3 service-worker restarts (background.ts just re-injects
// the same deps against persisted state via `resume`).

import { SessionExpiredError } from "./d2l-client";
import type {
  CompletePayload,
  D2LEnrollmentItem,
  DropboxExtra,
  EnrollmentIn,
  HandshakePayload,
  HandshakeResponse,
  KnownCourse,
  NeededItem,
  NewsExtra,
  TocPayload,
  TocResponse,
} from "./types";

// ---------------------------------------------------------------------------
// Injected dependency interfaces
// ---------------------------------------------------------------------------

/** The subset of D2LClient the sync engine needs. */
export interface D2LClientLike {
  discoverVersions(): Promise<{ lp: string; le: string }>;
  whoami(lp: string): Promise<unknown>;
  myEnrollments(lp: string): Promise<D2LEnrollmentItem[]>;
  courseToc(le: string, orgUnitId: number): Promise<unknown>;
  /** Already reshaped into the backend's extras contract by d2l-client.ts
   * -- this engine forwards them verbatim and does no mapping of its own. */
  news(le: string, orgUnitId: number): Promise<NewsExtra[]>;
  dropboxFolders(le: string, orgUnitId: number): Promise<DropboxExtra[]>;
  fetchTopicFile(le: string, orgUnitId: number, topicId: number): Promise<Response>;
}

/** The subset of BackendClient the sync engine needs. */
export interface BackendClientLike {
  handshake(payload: HandshakePayload): Promise<HandshakeResponse>;
  toc(payload: TocPayload): Promise<TocResponse>;
  uploadFile(
    syncRunId: number,
    d2lTopicId: number,
    res: Response,
    meta: { sourceUrl: string; title: string; d2lUpdated?: string },
  ): Promise<{ materialId: number; sha256: string; deduped: boolean }>;
  complete(payload: CompletePayload): Promise<{ status: string }>;
}

export type SyncPhase = "running" | "needs-login" | "complete" | "failed";

export interface SyncState {
  /** Null until backend.toc has actually succeeded -- i.e. no backend sync
   * run exists yet. This happens when a SessionExpiredError pauses
   * syncCourse before /toc ever runs (see EstablishedSyncState below):
   * there's nothing server-side to resume, so `resume()` treats a null
   * syncRunId as "retry syncCourse from scratch" rather than draining a
   * queue that was never established. */
  syncRunId: number | null;
  orgUnitId: number;
  /** Remaining items still to upload -- already-done items are not kept
   * here, so persisting/resuming this never re-fetches them. */
  queue: NeededItem[];
  done: number;
  total: number;
  errors: { d2lTopicId: number | null; message: string }[];
  phase: SyncPhase;
}

/** SyncState once a real backend sync run exists (syncRunId narrowed to
 * non-null) -- what drainQueue actually operates on, so every
 * backend.uploadFile/complete call it makes is type-checked against a
 * real id with no runtime assertions needed. */
type EstablishedSyncState = Omit<SyncState, "syncRunId"> & { syncRunId: number };

/** Snapshot handed to onProgress after every state change -- everything a
 * popup or badge needs to render progress without holding the (potentially
 * large) remaining-queue array. */
export interface SyncProgress {
  syncRunId: number | null;
  orgUnitId: number;
  done: number;
  total: number;
  errorCount: number;
  phase: SyncPhase;
}

export interface SyncDeps {
  d2l: D2LClientLike;
  backend: BackendClientLike;
  /** Persist the current state (chrome.storage.session.set in prod) so a
   * restarted MV3 service worker can resume mid-sync. */
  saveState(state: SyncState): Promise<void>;
  /** Notify the popup / badge of a state change. */
  onProgress(progress: SyncProgress): void;
}

function toProgress(state: SyncState): SyncProgress {
  return {
    syncRunId: state.syncRunId,
    orgUnitId: state.orgUnitId,
    done: state.done,
    total: state.total,
    errorCount: state.errors.length,
    phase: state.phase,
  };
}

async function persist(deps: SyncDeps, state: SyncState): Promise<void> {
  await deps.saveState(state);
  deps.onProgress(toProgress(state));
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

type Guarded<T> = { ok: true; value: T } | { ok: false; pausedState: SyncState };

/** Runs a D2L call and converts a SessionExpiredError into a persisted
 * 'needs-login' state instead of letting it escape uncaught. drainQueue
 * already gives this guarantee to every per-file D2L call inside the
 * loop; this extends the same guarantee to the D2L calls syncCourse/resume
 * make *before* there's a queue to drain (discoverVersions, courseToc) --
 * without it, a session that's already dead when the user clicks
 * Sync/Resume throws raw out of these functions instead of pausing
 * cleanly, which is exactly the gap a self-healing watchdog can't recover
 * from (see background.ts's alarm handler). */
async function guardSessionExpired<T>(
  deps: SyncDeps,
  state: SyncState,
  run: () => Promise<T>,
): Promise<Guarded<T>> {
  try {
    return { ok: true, value: await run() };
  } catch (err) {
    if (!(err instanceof SessionExpiredError)) throw err;
    const pausedState: SyncState = { ...state, phase: "needs-login" };
    await persist(deps, pausedState);
    return { ok: false, pausedState };
  }
}

// ---------------------------------------------------------------------------
// discover
// ---------------------------------------------------------------------------

/** versions -> whoami (auth probe; SessionExpiredError propagates as-is) ->
 * myEnrollments -> backend.handshake. Returns the backend's knownCourses so
 * the popup can render a Sync button per course. */
export async function discover(deps: SyncDeps, origin: string): Promise<KnownCourse[]> {
  const versions = await deps.d2l.discoverVersions();
  const whoami = await deps.d2l.whoami(versions.lp);
  const enrollments = await deps.d2l.myEnrollments(versions.lp);

  const enrollmentPayload: EnrollmentIn[] = enrollments.map((item) => ({
    orgUnitId: item.OrgUnit.Id,
    name: item.OrgUnit.Name,
    code: item.OrgUnit.Code,
  }));

  const handshakeResponse = await deps.backend.handshake({
    tenantOrigin: origin,
    apiVersions: { lp: versions.lp, le: versions.le },
    whoami,
    enrollments: enrollmentPayload,
  });

  return handshakeResponse.knownCourses;
}

// ---------------------------------------------------------------------------
// syncCourse / resume (share one queue-draining loop)
// ---------------------------------------------------------------------------

/** Starts a fresh sync run for one course: fetches the ToC + extras, sends
 * them to the backend's /toc (which diffs and returns what's actually
 * needed), then drains the needed queue. A SessionExpiredError from either
 * D2L call made before /toc (discoverVersions, courseToc) pauses cleanly
 * (phase 'needs-login', syncRunId stays null -- no backend sync run was
 * ever established, so there's nothing concrete to resume except retrying
 * this whole function again) instead of throwing. */
export async function syncCourse(deps: SyncDeps, origin: string, orgUnitId: number): Promise<SyncState> {
  void origin; // kept for signature symmetry with discover/resume; d2l is already origin-bound

  const placeholderState: SyncState = {
    syncRunId: null,
    orgUnitId,
    queue: [],
    done: 0,
    total: 0,
    errors: [],
    phase: "running",
  };

  const versionsResult = await guardSessionExpired(deps, placeholderState, () => deps.d2l.discoverVersions());
  if (!versionsResult.ok) return versionsResult.pausedState;
  const { le } = versionsResult.value;

  // news()/dropboxFolders() are fail-soft in d2l-client.ts (they swallow
  // their own errors, including SessionExpiredError, and resolve to []),
  // so courseToc() is the only one of these three that can actually reject
  // with SessionExpiredError.
  const extrasResult = await guardSessionExpired(deps, placeholderState, () =>
    Promise.all([
      deps.d2l.courseToc(le, orgUnitId),
      deps.d2l.news(le, orgUnitId),
      deps.d2l.dropboxFolders(le, orgUnitId),
    ]),
  );
  if (!extrasResult.ok) return extrasResult.pausedState;
  const [toc, news, dropbox] = extrasResult.value;

  const tocPayload: TocPayload = { orgUnitId, toc, extras: { news, dropbox } };
  const tocResponse = await deps.backend.toc(tocPayload);

  const initialState: EstablishedSyncState = {
    syncRunId: tocResponse.syncRunId,
    orgUnitId,
    queue: tocResponse.needed,
    done: 0,
    total: tocResponse.needed.length,
    errors: [],
    phase: "running",
  };

  return drainQueue(deps, le, initialState);
}

/** Continues an interrupted sync run. If `state.syncRunId` is null, no
 * backend sync run was ever established (syncCourse paused before /toc
 * ran) -- there's no queue to resume, so this just retries syncCourse from
 * scratch. Otherwise it does NOT re-run /toc: the syncRunId and remaining
 * queue are exactly as they were left (by a per-file SessionExpiredError,
 * or a mid-sync SW restart). Re-checks the D2L session (discoverVersions)
 * before resuming the drain -- if it's still dead, pauses again instead of
 * throwing, same as syncCourse. */
export async function resume(deps: SyncDeps, origin: string, state: SyncState): Promise<SyncState> {
  if (state.syncRunId === null) {
    return syncCourse(deps, origin, state.orgUnitId);
  }
  const syncRunId = state.syncRunId;

  const versionsResult = await guardSessionExpired(deps, state, () => deps.d2l.discoverVersions());
  if (!versionsResult.ok) return versionsResult.pausedState;

  const activeState: EstablishedSyncState = { ...state, syncRunId, phase: "running" };
  return drainQueue(deps, versionsResult.value.le, activeState);
}

/** The shared loop body: fetchTopicFile -> uploadFile per remaining item,
 * sequentially. Persists + reports progress after every item. A
 * SessionExpiredError pauses the whole run (queue left intact, /complete
 * NOT called); any other per-file failure is recorded and the loop moves
 * on. Once the queue empties, calls /complete. */
async function drainQueue(deps: SyncDeps, le: string, initialState: EstablishedSyncState): Promise<SyncState> {
  let state = initialState;

  while (state.queue.length > 0) {
    const item = state.queue[0];

    try {
      const fileResponse = await deps.d2l.fetchTopicFile(le, state.orgUnitId, item.d2lTopicId);
      await deps.backend.uploadFile(state.syncRunId, item.d2lTopicId, fileResponse, {
        sourceUrl: item.url,
        title: item.title,
        d2lUpdated: item.lastModified ?? undefined,
      });
      state = { ...state, queue: state.queue.slice(1), done: state.done + 1 };
    } catch (err) {
      if (err instanceof SessionExpiredError) {
        // Leave `item` (and everything after it) in the queue untouched --
        // it was never actually processed.
        state = { ...state, phase: "needs-login" };
        await persist(deps, state);
        return state;
      }
      state = {
        ...state,
        queue: state.queue.slice(1),
        done: state.done + 1,
        errors: [...state.errors, { d2lTopicId: item.d2lTopicId, message: errorMessage(err) }],
      };
    }

    await persist(deps, state);
  }

  const completeResponse = await deps.backend.complete({ syncRunId: state.syncRunId, errors: state.errors });
  state = { ...state, phase: completeResponse.status === "failed" ? "failed" : "complete" };
  await persist(deps, state);

  return state;
}
