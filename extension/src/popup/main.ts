// Popup UI. Plain TS + inline CSS (in popup.html) — no framework, no extra
// bundle weight. All actual orchestration lives in background.ts / the pure
// sync-engine; this file only reads/writes chrome.storage.local for simple
// field persistence and talks to background.ts via chrome.runtime messages.

import { BackendClient } from "../lib/backend-client";
import type { SyncProgress, SyncState } from "../lib/sync-engine";
import type { KnownCourse } from "../lib/types";

const BACKEND_URL = "http://127.0.0.1:8730";

// ---------------------------------------------------------------------------
// Elements
// ---------------------------------------------------------------------------

const tokenInput = document.getElementById("token") as HTMLInputElement;
const testButton = document.getElementById("test-connection") as HTMLButtonElement;
const backendStatusDot = document.getElementById("backend-status-dot") as HTMLSpanElement;
const backendStatusText = document.getElementById("backend-status-text") as HTMLSpanElement;

const originInput = document.getElementById("origin") as HTMLInputElement;
const useCurrentTabButton = document.getElementById("use-current-tab") as HTMLButtonElement;
const connectButton = document.getElementById("connect") as HTMLButtonElement;
const brightspaceStatus = document.getElementById("brightspace-status") as HTMLParagraphElement;

const discoverButton = document.getElementById("discover") as HTMLButtonElement;
const courseList = document.getElementById("course-list") as HTMLUListElement;

const progressBarFill = document.getElementById("progress-bar-fill") as HTMLDivElement;
const progressText = document.getElementById("progress-text") as HTMLParagraphElement;
const errorCountText = document.getElementById("error-count") as HTMLParagraphElement;
const ltiStatusText = document.getElementById("lti-status") as HTMLParagraphElement;
const needsLoginBanner = document.getElementById("needs-login-banner") as HTMLDivElement;
const resumeButton = document.getElementById("resume") as HTMLButtonElement;

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// ---------------------------------------------------------------------------
// Backend section
// ---------------------------------------------------------------------------

chrome.storage.local.get("pairingToken", ({ pairingToken }) => {
  if (typeof pairingToken === "string") tokenInput.value = pairingToken;
});

tokenInput.addEventListener("change", () => {
  chrome.storage.local.set({ pairingToken: tokenInput.value });
});

testButton.addEventListener("click", async () => {
  backendStatusText.textContent = "Testing...";
  backendStatusDot.className = "status-dot";
  try {
    const response = await fetch(`${BACKEND_URL}/api/health`, {
      headers: { Authorization: `Bearer ${tokenInput.value}` },
    });
    const data = await response.json();
    backendStatusDot.className = `status-dot ${data.paired ? "ok" : "bad"}`;
    backendStatusText.textContent = `status: ${data.status}, paired: ${data.paired}`;
    // The manually-pasted token may have just become the paired one --
    // reflect that in the Connect section too, without requiring the
    // popup to be reopened.
    void refreshPairedUi();
  } catch {
    backendStatusDot.className = "status-dot bad";
    backendStatusText.textContent = "unreachable";
  }
});

// ---------------------------------------------------------------------------
// One-click pairing (M2.7) -- request -> approve -> claim. Replaces the
// copy/paste flow above with a click: the popup requests, the user clicks
// Approve on the app's Settings page, the popup claims the real token and
// writes it to the SAME chrome.storage.local key the manual paste field
// above uses (`tokenInput`'s change listener), so either path ends up in
// the identical place. The paste field stays as the fallback below the
// divider (see popup.html) -- nothing here disables or hides it.
// ---------------------------------------------------------------------------

const SETTINGS_URL = `${BACKEND_URL}/settings`;
const PAIR_POLL_INTERVAL_MS = 2000;
// 3 minutes -- matches the backend's 180s pending-request TTL (api/pair.py),
// so this never keeps polling something the server has already forgotten.
const PAIR_POLL_TIMEOUT_MS = 3 * 60 * 1000;

async function pairGetToken(): Promise<string> {
  const { pairingToken } = await chrome.storage.local.get("pairingToken");
  return typeof pairingToken === "string" ? pairingToken : "";
}

const pairBackend = new BackendClient(BACKEND_URL, pairGetToken);

const pairConnectButton = document.getElementById("pair-connect") as HTMLButtonElement;
const pairStatusText = document.getElementById("pair-status") as HTMLParagraphElement;
const pairPairedStatus = document.getElementById("pair-paired") as HTMLParagraphElement;

// Popup scripts are torn down the instant the popup closes -- there is no
// unload hook to write, and there doesn't need to be one: the interval
// simply stops firing, and the only storage write in this whole flow
// happens inside a single successful claim response (see
// `handlePairPollTick` below), never mid-poll. No partial state is
// possible either way.
let pairPollTimer: ReturnType<typeof setInterval> | undefined;

function stopPairPolling(): void {
  if (pairPollTimer !== undefined) {
    clearInterval(pairPollTimer);
    pairPollTimer = undefined;
  }
}

/** Reflects whether the backend currently considers this extension paired
 * -- Connect button + status line vs a plain "Connected" line. Called on
 * popup open and again after anything that could have changed pairing
 * (a successful claim, or a successful manual "Test connection"). */
async function refreshPairedUi(): Promise<void> {
  let paired: boolean;
  try {
    ({ paired } = await pairBackend.health());
  } catch {
    // Backend unreachable -- leave whatever was last rendered (normally
    // the Connect button) rather than guessing at a state.
    return;
  }
  pairConnectButton.style.display = paired ? "none" : "";
  pairPairedStatus.style.display = paired ? "" : "none";
  if (paired) {
    stopPairPolling();
    pairStatusText.textContent = "";
  }
}

function renderPairWaitingForApproval(): void {
  pairStatusText.textContent = "";
  pairStatusText.append(
    "Open the app's Settings page and click Approve: ",
    Object.assign(document.createElement("a"), {
      href: SETTINGS_URL,
      target: "_blank",
      rel: "noopener",
      textContent: SETTINGS_URL,
    }),
  );
}

async function handlePairPollTick(requestId: string, deadline: number): Promise<void> {
  if (Date.now() >= deadline) {
    stopPairPolling();
    pairConnectButton.disabled = false;
    pairStatusText.textContent = "Connect request timed out. Click Connect to try again.";
    return;
  }

  let response;
  try {
    response = await pairBackend.pairClaim(requestId);
  } catch {
    // One failed poll (a transient backend hiccup, or -- once the
    // request's server-side TTL has passed -- a 404 on every subsequent
    // attempt) is never fatal by itself; the deadline check above is what
    // eventually gives up, so an expired request naturally times out here
    // instead of polling forever.
    return;
  }

  if (response.status !== "approved") return;

  stopPairPolling();
  pairConnectButton.disabled = false;

  if (!response.pairingToken) {
    // Shouldn't happen (the backend always sets it on 'approved'), but
    // never write storage on a malformed response.
    pairStatusText.textContent = "Connect failed: malformed response from backend.";
    return;
  }

  await chrome.storage.local.set({ pairingToken: response.pairingToken });
  tokenInput.value = response.pairingToken;
  pairStatusText.textContent = "Connected.";
  await refreshPairedUi();
}

async function startPairing(): Promise<void> {
  stopPairPolling();
  pairConnectButton.disabled = true;
  pairStatusText.textContent = "Requesting...";

  let requestId: string;
  try {
    ({ requestId } = await pairBackend.pairRequest());
  } catch (err) {
    pairStatusText.textContent = `Connect failed: ${errorMessage(err)}`;
    pairConnectButton.disabled = false;
    return;
  }

  renderPairWaitingForApproval();
  const deadline = Date.now() + PAIR_POLL_TIMEOUT_MS;
  pairPollTimer = setInterval(() => void handlePairPollTick(requestId, deadline), PAIR_POLL_INTERVAL_MS);
}

pairConnectButton.addEventListener("click", () => void startPairing());

void refreshPairedUi();

// ---------------------------------------------------------------------------
// Brightspace section
// ---------------------------------------------------------------------------

chrome.storage.local.get("brightspaceOrigin", ({ brightspaceOrigin }) => {
  if (typeof brightspaceOrigin === "string") originInput.value = brightspaceOrigin;
});

useCurrentTabButton.addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url) {
    brightspaceStatus.textContent = "Could not read the current tab's URL.";
    return;
  }
  try {
    originInput.value = new URL(tab.url).origin;
  } catch {
    brightspaceStatus.textContent = "Current tab is not a valid URL.";
  }
});

connectButton.addEventListener("click", async () => {
  const origin = originInput.value.trim();
  if (!origin) {
    brightspaceStatus.textContent = "Enter a Brightspace origin first.";
    return;
  }
  brightspaceStatus.textContent = "Requesting permission...";
  try {
    // A popup button click is a valid user gesture for
    // chrome.permissions.request — the school origin isn't known until
    // runtime, so it's requested here rather than baked into the manifest.
    const granted = await chrome.permissions.request({ origins: [`${origin}/*`] });
    if (!granted) {
      brightspaceStatus.textContent = "Permission denied.";
      return;
    }
    await chrome.storage.local.set({ brightspaceOrigin: origin });
    brightspaceStatus.textContent = "Connected.";
  } catch (err) {
    brightspaceStatus.textContent = `Connect failed: ${errorMessage(err)}`;
  }
});

// ---------------------------------------------------------------------------
// Courses section
// ---------------------------------------------------------------------------

function renderCourses(courses: KnownCourse[]): void {
  courseList.innerHTML = "";
  for (const course of courses) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = course.name;
    label.title = course.name;
    const syncButton = document.createElement("button");
    syncButton.type = "button";
    syncButton.textContent = "Sync";
    syncButton.addEventListener("click", () => void startSync(course.orgUnitId));
    li.append(label, syncButton);
    courseList.append(li);
  }
}

chrome.storage.local.get("knownCourses", ({ knownCourses }) => {
  if (Array.isArray(knownCourses)) renderCourses(knownCourses as KnownCourse[]);
});

discoverButton.addEventListener("click", async () => {
  const origin = originInput.value.trim();
  if (!origin) {
    brightspaceStatus.textContent = "Connect to a Brightspace origin first.";
    return;
  }
  discoverButton.disabled = true;
  brightspaceStatus.textContent = "Discovering courses...";
  try {
    const response = await chrome.runtime.sendMessage({ cmd: "discover", origin });
    if (response?.error) {
      brightspaceStatus.textContent = `Discover failed: ${response.error}`;
      return;
    }
    const knownCourses = (response?.knownCourses ?? []) as KnownCourse[];
    renderCourses(knownCourses);
    brightspaceStatus.textContent = `Found ${knownCourses.length} course(s).`;
  } catch (err) {
    brightspaceStatus.textContent = `Discover failed: ${errorMessage(err)}`;
  } finally {
    discoverButton.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Progress section
// ---------------------------------------------------------------------------

function stateToProgress(state: SyncState | null | undefined): SyncProgress | null {
  if (!state) return null;
  return {
    syncRunId: state.syncRunId,
    orgUnitId: state.orgUnitId,
    done: state.done,
    total: state.total,
    errorCount: state.errors.length,
    phase: state.phase,
  };
}

function renderProgress(progress: SyncProgress | null): void {
  if (!progress) {
    progressBarFill.style.width = "0%";
    progressText.textContent = "No sync running";
    errorCountText.textContent = "";
    needsLoginBanner.classList.remove("visible");
    return;
  }

  const pct = progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 100;
  progressBarFill.style.width = `${pct}%`;
  progressText.textContent = `${progress.done}/${progress.total} — ${progress.phase}`;
  errorCountText.textContent = progress.errorCount > 0 ? `${progress.errorCount} error(s)` : "";
  needsLoginBanner.classList.toggle("visible", progress.phase === "needs-login");
}

// ---------------------------------------------------------------------------
// LTI resolver progress (M2.7) — one line under sync progress, driven by
// background.ts's `{evt: "lti-progress"}` messages. No new screens: while a
// run is in flight this shows "N/M"; once it finishes, background.ts sends
// one more message carrying the resolved/unrecognized/failed breakdown and
// this switches to the summary line instead.
// ---------------------------------------------------------------------------

interface LtiProgressMessage {
  done: number;
  total: number;
  resolved?: number;
  unrecognized?: number;
  failed?: number;
  error?: string;
}

function renderLtiProgress(msg: LtiProgressMessage): void {
  if (msg.error) {
    ltiStatusText.textContent = `Recording link resolution failed: ${msg.error}`;
    return;
  }
  if (msg.resolved !== undefined && msg.unrecognized !== undefined && msg.failed !== undefined) {
    const needsLook = msg.unrecognized + msg.failed;
    const parts = [`${msg.resolved} resolved`];
    if (needsLook > 0) parts.push(`${needsLook} needs a look`);
    ltiStatusText.textContent = `Recording links: ${parts.join(", ")}`;
    return;
  }
  ltiStatusText.textContent = `Resolving recording links… ${msg.done}/${msg.total}`;
}

async function startSync(orgUnitId: number): Promise<void> {
  const origin = originInput.value.trim();
  if (!origin) {
    brightspaceStatus.textContent = "Connect to a Brightspace origin first.";
    return;
  }
  ltiStatusText.textContent = "";
  try {
    const response = await chrome.runtime.sendMessage({ cmd: "sync", origin, orgUnitId });
    if (response?.error) {
      progressText.textContent = `Sync failed: ${response.error}`;
      return;
    }
    renderProgress(stateToProgress(response?.state as SyncState | undefined));
  } catch (err) {
    progressText.textContent = `Sync failed: ${errorMessage(err)}`;
  }
}

resumeButton.addEventListener("click", async () => {
  resumeButton.disabled = true;
  ltiStatusText.textContent = "";
  try {
    const response = await chrome.runtime.sendMessage({ cmd: "resume" });
    if (response?.error) {
      progressText.textContent = `Resume failed: ${response.error}`;
      return;
    }
    renderProgress(stateToProgress(response?.state as SyncState | undefined));
  } catch (err) {
    progressText.textContent = `Resume failed: ${errorMessage(err)}`;
  } finally {
    resumeButton.disabled = false;
  }
});

// Live progress updates pushed from background.ts while this popup is open.
chrome.runtime.onMessage.addListener((message) => {
  if (message && typeof message === "object" && message.evt === "progress") {
    renderProgress(message.progress as SyncProgress);
  } else if (message && typeof message === "object" && message.evt === "lti-progress") {
    renderLtiProgress(message as LtiProgressMessage);
  }
});

// Restore last-known progress on popup open — the popup itself holds no
// state across opens; background.ts persists it in chrome.storage.session.
chrome.runtime
  .sendMessage({ cmd: "getState" })
  .then((response) => {
    renderProgress(stateToProgress(response?.state as SyncState | null | undefined));
  })
  .catch(() => {
    // background not ready yet — fine, popup just shows the empty state.
  });
