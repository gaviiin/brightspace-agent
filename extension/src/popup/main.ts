// Popup UI. Plain TS + inline CSS (in popup.html) — no framework, no extra
// bundle weight. All actual orchestration lives in background.ts / the pure
// sync-engine; this file only reads/writes chrome.storage.local for simple
// field persistence and talks to background.ts via chrome.runtime messages.

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
  } catch {
    backendStatusDot.className = "status-dot bad";
    backendStatusText.textContent = "unreachable";
  }
});

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
