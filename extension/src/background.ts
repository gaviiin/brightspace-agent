// Thin chrome adapter: wires chrome.runtime messaging/storage/alarms to the
// pure sync-engine. NO orchestration logic lives here -- see sync-engine.ts
// for that (and for why it has to be this way: MV3 service workers can be
// killed and restarted mid-sync at any time).

import { BackendClient } from "./lib/backend-client";
import { D2LClient, RateLimitedFetcher } from "./lib/d2l-client";
import { resolveLtiCandidates } from "./lib/lti-resolver";
import type { LtiResolverDeps, TabDriver } from "./lib/lti-resolver";
import { discover, resume as resumeSync, syncCourse } from "./lib/sync-engine";
import type { SyncDeps, SyncProgress, SyncState } from "./lib/sync-engine";
import type { KnownCourse } from "./lib/types";

const BACKEND_URL = "http://127.0.0.1:8730";
const WATCHDOG_ALARM = "sync-watchdog";

// True only while a sync loop is actively awaiting inside *this*
// service-worker lifetime. If the SW is killed and restarted mid-sync, this
// resets to false but chrome.storage.session's persisted state still says
// phase: 'running' -- that mismatch is exactly what the watchdog alarm
// looks for below.
let syncInFlight = false;

// ---------------------------------------------------------------------------
// Storage helpers
// ---------------------------------------------------------------------------

async function getToken(): Promise<string> {
  const { pairingToken } = await chrome.storage.local.get("pairingToken");
  return typeof pairingToken === "string" ? pairingToken : "";
}

async function getStoredOrigin(): Promise<string | undefined> {
  const { brightspaceOrigin } = await chrome.storage.local.get("brightspaceOrigin");
  return typeof brightspaceOrigin === "string" ? brightspaceOrigin : undefined;
}

async function getStoredState(): Promise<SyncState | undefined> {
  const { syncState } = await chrome.storage.session.get("syncState");
  return syncState as SyncState | undefined;
}

// ---------------------------------------------------------------------------
// Deps wiring
// ---------------------------------------------------------------------------

function makeDeps(origin: string): SyncDeps {
  const d2l = new D2LClient(origin, new RateLimitedFetcher());
  const backend = new BackendClient(BACKEND_URL, getToken);

  return {
    d2l,
    backend,
    saveState: (state) => chrome.storage.session.set({ syncState: state }),
    onProgress: (progress) => {
      // The popup may be closed -- nobody listening is not an error.
      chrome.runtime.sendMessage({ evt: "progress", progress }).catch(() => {});
      updateBadge(progress);
    },
  };
}

function updateBadge(progress: SyncProgress): void {
  if (progress.phase === "needs-login") {
    chrome.action.setBadgeBackgroundColor({ color: "#d93025" });
    chrome.action.setBadgeText({ text: "!" });
  } else if (progress.phase === "complete" || progress.phase === "failed") {
    chrome.action.setBadgeText({ text: "" });
  } else {
    chrome.action.setBadgeBackgroundColor({ color: "#1a73e8" });
    chrome.action.setBadgeText({ text: `${progress.done}/${progress.total}` });
  }
}

// ---------------------------------------------------------------------------
// LTI resolver (M2.7): chrome.tabs adapter + post-sync wiring
// ---------------------------------------------------------------------------

/** The only chrome-specific machinery lti-resolver.ts needs: open a
 * background tab, read its URL, close it, and watch chrome.tabs.onUpdated/
 * onRemoved for the settle policy the pure module hands in (quietMs after a
 * `status: "complete"`, or a hard timeoutMs cap, or early removal). */
function createTabDriver(): TabDriver {
  return {
    async open(url: string): Promise<number> {
      const tab = await chrome.tabs.create({ url, active: false });
      if (tab.id === undefined) throw new Error("chrome.tabs.create returned no tab id");
      return tab.id;
    },

    async currentUrl(tabId: number): Promise<string | null> {
      try {
        const tab = await chrome.tabs.get(tabId);
        return tab.url ?? null;
      } catch {
        // Tab no longer exists -- most likely the user closed it.
        return null;
      }
    },

    async close(tabId: number): Promise<void> {
      try {
        await chrome.tabs.remove(tabId);
      } catch {
        // Already gone -- nothing left to close.
      }
    },

    onNavigationSettled(tabId: number, quietMs: number, timeoutMs: number): Promise<void> {
      return new Promise((resolve) => {
        let quietTimer: ReturnType<typeof setTimeout> | undefined;
        let settled = false;

        const finish = (): void => {
          if (settled) return;
          settled = true;
          if (quietTimer !== undefined) clearTimeout(quietTimer);
          clearTimeout(hardCapTimer);
          chrome.tabs.onUpdated.removeListener(onUpdated);
          chrome.tabs.onRemoved.removeListener(onRemoved);
          resolve();
        };

        const onUpdated = (updatedTabId: number, info: chrome.tabs.OnUpdatedInfo): void => {
          if (updatedTabId !== tabId) return;
          if (info.status === "complete") {
            if (quietTimer !== undefined) clearTimeout(quietTimer);
            quietTimer = setTimeout(finish, quietMs);
          } else if (quietTimer !== undefined) {
            // A new navigation started mid-quiet-period (e.g. an LTI
            // redirect chain isn't done yet) -- cancel and wait for the
            // next "complete" instead of settling early.
            clearTimeout(quietTimer);
            quietTimer = undefined;
          }
        };

        const onRemoved = (removedTabId: number): void => {
          if (removedTabId === tabId) finish();
        };

        const hardCapTimer = setTimeout(finish, timeoutMs);
        chrome.tabs.onUpdated.addListener(onUpdated);
        chrome.tabs.onRemoved.addListener(onRemoved);
      });
    },
  };
}

/** Runs the LTI resolver for one course after a completed sync, forwarding
 * its progress as `{evt: "lti-progress", done, total}` messages (ignored if
 * no popup is listening, same as sync progress) and a final message
 * carrying the resolved/unrecognized/failed breakdown so the popup can
 * render the "N resolved, M needs a look" summary. Wrapped end-to-end: a
 * resolver failure (e.g. the backend unreachable while fetching candidates)
 * never turns a completed sync into a failed one -- at worst it reports
 * `{evt: "lti-progress", error}`. */
async function runLtiResolver(origin: string, orgUnitId: number): Promise<void> {
  try {
    const backend = new BackendClient(BACKEND_URL, getToken);
    const deps: LtiResolverDeps = {
      backend,
      tabs: createTabDriver(),
      onProgress: (p) => {
        chrome.runtime.sendMessage({ evt: "lti-progress", done: p.done, total: p.total }).catch(() => {});
      },
    };
    const summary = await resolveLtiCandidates(deps, origin, orgUnitId);
    if (summary.total > 0) {
      chrome.runtime
        .sendMessage({
          evt: "lti-progress",
          done: summary.total,
          total: summary.total,
          resolved: summary.resolved,
          unrecognized: summary.unrecognized,
          failed: summary.failed,
        })
        .catch(() => {});
    }
  } catch (err) {
    chrome.runtime.sendMessage({ evt: "lti-progress", error: errorMessage(err) }).catch(() => {});
  }
}

// ---------------------------------------------------------------------------
// Command handlers
// ---------------------------------------------------------------------------

async function handleDiscover(origin: string): Promise<{ knownCourses: KnownCourse[] }> {
  const deps = makeDeps(origin);
  const knownCourses = await discover(deps, origin);
  // Cache origin + known courses for the popup to render without asking
  // the backend/D2L again on every open.
  await chrome.storage.local.set({ brightspaceOrigin: origin, knownCourses });
  return { knownCourses };
}

async function handleSync(origin: string, orgUnitId: number): Promise<{ state: SyncState } | { error: string }> {
  // Only one syncState (chrome.storage.session) and one badge exist per
  // service worker -- running two syncCourse loops concurrently would race
  // on both. Reject the second start instead of corrupting either run.
  if (syncInFlight) {
    return { error: "a sync is already running" };
  }
  syncInFlight = true;
  try {
    const deps = makeDeps(origin);
    const state = await syncCourse(deps, origin, orgUnitId);
    if (state.phase === "complete") {
      await runLtiResolver(origin, orgUnitId);
    }
    return { state };
  } finally {
    syncInFlight = false;
  }
}

async function handleResume(): Promise<{ state: SyncState } | { error: string }> {
  if (syncInFlight) {
    return { error: "a sync is already running" };
  }
  const origin = await getStoredOrigin();
  const state = await getStoredState();
  if (!origin || !state) {
    return { error: "nothing to resume" };
  }
  syncInFlight = true;
  try {
    const deps = makeDeps(origin);
    const resumed = await resumeSync(deps, origin, state);
    if (resumed.phase === "complete") {
      await runLtiResolver(origin, resumed.orgUnitId);
    }
    return { state: resumed };
  } finally {
    syncInFlight = false;
  }
}

async function handleGetState(): Promise<{ state: SyncState | null }> {
  const state = await getStoredState();
  return { state: state ?? null };
}

// ---------------------------------------------------------------------------
// Message router
// ---------------------------------------------------------------------------

interface IncomingMessage {
  cmd: string;
  origin?: string;
  orgUnitId?: number;
}

function isIncomingMessage(message: unknown): message is IncomingMessage {
  return (
    typeof message === "object" && message !== null && typeof (message as { cmd?: unknown }).cmd === "string"
  );
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
  if (!isIncomingMessage(message)) return undefined;

  const respondWithError = (err: unknown): void => sendResponse({ error: errorMessage(err) });

  switch (message.cmd) {
    case "discover":
      if (typeof message.origin !== "string") {
        sendResponse({ error: "missing origin" });
        return undefined;
      }
      handleDiscover(message.origin).then(sendResponse).catch(respondWithError);
      return true;

    case "sync":
      if (typeof message.origin !== "string" || typeof message.orgUnitId !== "number") {
        sendResponse({ error: "missing origin or orgUnitId" });
        return undefined;
      }
      handleSync(message.origin, message.orgUnitId).then(sendResponse).catch(respondWithError);
      return true;

    case "resume":
      handleResume().then(sendResponse).catch(respondWithError);
      return true;

    case "getState":
      handleGetState().then(sendResponse).catch(respondWithError);
      return true;

    default:
      return undefined;
  }
});

// ---------------------------------------------------------------------------
// Watchdog: resume a sync interrupted by an MV3 service-worker restart
// ---------------------------------------------------------------------------

chrome.alarms.create(WATCHDOG_ALARM, { periodInMinutes: 0.5 });

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== WATCHDOG_ALARM || syncInFlight) return;
  getStoredState()
    .then((state) => {
      if (state && state.phase === "running") {
        return handleResume();
      }
      return undefined;
    })
    .catch(() => {
      // Best-effort: the next alarm tick (30s later) will retry.
    });
});
