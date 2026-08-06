// Thin chrome adapter: wires chrome.runtime messaging/storage/alarms to the
// pure sync-engine. NO orchestration logic lives here -- see sync-engine.ts
// for that (and for why it has to be this way: MV3 service workers can be
// killed and restarted mid-sync at any time).

import { BackendClient } from "./lib/backend-client";
import { D2LClient, RateLimitedFetcher } from "./lib/d2l-client";
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
