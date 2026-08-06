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
  EnrollmentIn,
  HandshakePayload,
  HandshakeResponse,
  KnownCourse,
  NeededItem,
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
  news(le: string, orgUnitId: number): Promise<unknown[]>;
  dropboxFolders(le: string, orgUnitId: number): Promise<unknown[]>;
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
  syncRunId: number;
  orgUnitId: number;
  /** Remaining items still to upload -- already-done items are not kept
   * here, so persisting/resuming this never re-fetches them. */
  queue: NeededItem[];
  done: number;
  total: number;
  errors: { d2lTopicId: number | null; message: string }[];
  phase: SyncPhase;
}

/** Snapshot handed to onProgress after every state change -- everything a
 * popup or badge needs to render progress without holding the (potentially
 * large) remaining-queue array. */
export interface SyncProgress {
  syncRunId: number;
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
 * needed), then drains the needed queue. */
export async function syncCourse(deps: SyncDeps, origin: string, orgUnitId: number): Promise<SyncState> {
  void origin; // kept for signature symmetry with discover/resume; d2l is already origin-bound
  const versions = await deps.d2l.discoverVersions();

  const [toc, news, dropbox] = await Promise.all([
    deps.d2l.courseToc(versions.le, orgUnitId),
    deps.d2l.news(versions.le, orgUnitId),
    deps.d2l.dropboxFolders(versions.le, orgUnitId),
  ]);

  const tocPayload: TocPayload = { orgUnitId, toc, extras: { news, dropbox } };
  const tocResponse = await deps.backend.toc(tocPayload);

  const initialState: SyncState = {
    syncRunId: tocResponse.syncRunId,
    orgUnitId,
    queue: tocResponse.needed,
    done: 0,
    total: tocResponse.needed.length,
    errors: [],
    phase: "running",
  };

  return drainQueue(deps, versions.le, initialState);
}

/** Continues an interrupted sync run from its persisted remaining queue.
 * Does NOT re-run /toc -- the syncRunId and queue are exactly as they were
 * left (by a per-file SessionExpiredError, or a mid-sync SW restart). */
export async function resume(deps: SyncDeps, origin: string, state: SyncState): Promise<SyncState> {
  void origin; // kept for signature symmetry with discover/syncCourse; d2l is already origin-bound
  const versions = await deps.d2l.discoverVersions();
  return drainQueue(deps, versions.le, { ...state, phase: "running" });
}

/** The shared loop body: fetchTopicFile -> uploadFile per remaining item,
 * sequentially. Persists + reports progress after every item. A
 * SessionExpiredError pauses the whole run (queue left intact, /complete
 * NOT called); any other per-file failure is recorded and the loop moves
 * on. Once the queue empties, calls /complete. */
async function drainQueue(deps: SyncDeps, le: string, initialState: SyncState): Promise<SyncState> {
  let state = initialState;

  while (state.queue.length > 0) {
    const item = state.queue[0];

    try {
      const fileResponse = await deps.d2l.fetchTopicFile(le, state.orgUnitId, item.d2lTopicId);
      await deps.backend.uploadFile(state.syncRunId, item.d2lTopicId, fileResponse, {
        sourceUrl: item.url,
        title: item.title,
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
