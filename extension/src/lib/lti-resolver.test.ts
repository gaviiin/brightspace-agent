import { afterEach, describe, expect, it, vi } from "vitest";

import {
  LTI_MAX_CANDIDATES_PER_RUN,
  LTI_SETTLE_QUIET_MS,
  LTI_SETTLE_TIMEOUT_MS,
  canonicalizeOrigin,
  resolveLtiCandidates,
} from "./lti-resolver";
import type { LtiResolverBackendLike, LtiResolverDeps, TabDriver } from "./lti-resolver";
import type { LtiCandidate, LtiResolutionPayload, LtiResolutionResponse } from "./types";

// ---------------------------------------------------------------------------
// Test helpers — plain fakes, no chrome.* (the whole point of the
// injected-interface architecture rule; see lti-resolver.ts's header).
// ---------------------------------------------------------------------------

const ORIGIN = "https://tenant.example";

function candidate(materialId: number, launchUrl = `/d2l/lti/launch/${materialId}`): LtiCandidate {
  return { materialId, title: `Lecture ${materialId}`, launchUrl };
}

function makeFakeBackend(overrides: Partial<LtiResolverBackendLike> = {}): LtiResolverBackendLike {
  return {
    ltiCandidates: vi.fn().mockResolvedValue({ courseId: 1, candidates: [] }),
    reportLtiResolution: vi.fn().mockResolvedValue({ status: "resolved" } as LtiResolutionResponse),
    ...overrides,
  };
}

interface FakeTab {
  openedUrl: string;
  currentUrl: string | null;
  closed: boolean;
}

interface FakeTabDriverOptions {
  /** Maps (tabId, openedUrl) -> the URL currentUrl() should report once
   * settled. Defaults to bouncing back to the opened URL unchanged. */
  finalUrlFor?: (tabId: number, openedUrl: string) => string | null;
  onNavigationSettled?: TabDriver["onNavigationSettled"];
}

function makeFakeTabs(options: FakeTabDriverOptions = {}): TabDriver & { tabsById: Map<number, FakeTab> } {
  const tabsById = new Map<number, FakeTab>();
  let nextId = 1;

  const open = vi.fn(async (url: string): Promise<number> => {
    const id = nextId++;
    const finalUrl = options.finalUrlFor ? options.finalUrlFor(id, url) : url;
    tabsById.set(id, { openedUrl: url, currentUrl: finalUrl, closed: false });
    return id;
  });

  const currentUrl = vi.fn(async (tabId: number): Promise<string | null> => {
    const tab = tabsById.get(tabId);
    if (!tab || tab.closed) return null;
    return tab.currentUrl;
  });

  const close = vi.fn(async (tabId: number): Promise<void> => {
    const tab = tabsById.get(tabId);
    if (tab) tab.closed = true;
  });

  const onNavigationSettled = options.onNavigationSettled ?? vi.fn().mockResolvedValue(undefined);

  return { open, currentUrl, close, onNavigationSettled, tabsById };
}

function makeDeps(
  tabs: TabDriver,
  backend: LtiResolverBackendLike,
): { deps: LtiResolverDeps; onProgress: ReturnType<typeof vi.fn> } {
  const onProgress = vi.fn();
  return { deps: { backend, tabs, onProgress }, onProgress };
}

afterEach(() => {
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// Happy path
// ---------------------------------------------------------------------------

describe("resolveLtiCandidates — happy path", () => {
  it("opens, settles, reads the final URL, closes, then POSTs the resolution", async () => {
    const tabs = makeFakeTabs({
      finalUrlFor: () => "https://mediasite.example.edu/watch/abc",
    });
    const backend = makeFakeBackend({
      reportLtiResolution: vi.fn().mockResolvedValue({ status: "resolved", platform: "mediasite", added: 1, total: 1 }),
      ltiCandidates: vi.fn().mockResolvedValue({ courseId: 5, candidates: [candidate(1)] }),
    });
    const { deps } = makeDeps(tabs, backend);

    const summary = await resolveLtiCandidates(deps, ORIGIN, 42);

    expect(tabs.open).toHaveBeenCalledWith("https://tenant.example/d2l/lti/launch/1");
    expect(tabs.onNavigationSettled).toHaveBeenCalledWith(1, LTI_SETTLE_QUIET_MS, LTI_SETTLE_TIMEOUT_MS);
    expect(tabs.currentUrl).toHaveBeenCalledWith(1);
    expect(tabs.close).toHaveBeenCalledWith(1);
    expect(backend.reportLtiResolution).toHaveBeenCalledWith({
      orgUnitId: 42,
      materialId: 1,
      finalUrl: "https://mediasite.example.edu/watch/abc",
      error: null,
    } satisfies LtiResolutionPayload);
    expect(summary).toEqual({ resolved: 1, unrecognized: 0, failed: 0, total: 1 });
  });

  it("resolves a relative launchUrl against the tenant origin before opening it", async () => {
    const tabs = makeFakeTabs();
    const backend = makeFakeBackend({
      ltiCandidates: vi.fn().mockResolvedValue({ courseId: 1, candidates: [candidate(9, "/d2l/lti/launch/9?x=1")] }),
    });
    const { deps } = makeDeps(tabs, backend);

    await resolveLtiCandidates(deps, ORIGIN, 1);

    expect(tabs.open).toHaveBeenCalledWith("https://tenant.example/d2l/lti/launch/9?x=1");
  });

  it("closes the tab even though a bounced-back final URL still sits on the tenant origin, and reports it as-is", async () => {
    // Per the brief: the backend is the one that classifies a same-origin
    // bounce-back as 'unrecognized' -- the resolver just reports finalUrl
    // as-is, verbatim.
    const tabs = makeFakeTabs({ finalUrlFor: () => "https://tenant.example/d2l/home/1" });
    const backend = makeFakeBackend({
      ltiCandidates: vi.fn().mockResolvedValue({ courseId: 1, candidates: [candidate(1)] }),
      reportLtiResolution: vi.fn().mockResolvedValue({ status: "unrecognized" }),
    });
    const { deps } = makeDeps(tabs, backend);

    const summary = await resolveLtiCandidates(deps, ORIGIN, 1);

    expect(backend.reportLtiResolution).toHaveBeenCalledWith({
      orgUnitId: 1,
      materialId: 1,
      finalUrl: "https://tenant.example/d2l/home/1",
      error: null,
    });
    expect(tabs.close).toHaveBeenCalledWith(1);
    expect(summary).toEqual({ resolved: 0, unrecognized: 1, failed: 0, total: 1 });
  });
});

// ---------------------------------------------------------------------------
// Timeout path / settle policy
// ---------------------------------------------------------------------------

describe("resolveLtiCandidates — settle policy", () => {
  it("passes the fixed quietMs=2000/timeoutMs=25000 policy to the TabDriver", async () => {
    const tabs = makeFakeTabs();
    const backend = makeFakeBackend({
      ltiCandidates: vi.fn().mockResolvedValue({ courseId: 1, candidates: [candidate(1)] }),
    });
    const { deps } = makeDeps(tabs, backend);

    await resolveLtiCandidates(deps, ORIGIN, 1);

    expect(tabs.onNavigationSettled).toHaveBeenCalledWith(expect.any(Number), 2000, 25000);
  });

  it("awaits the full settle duration (hard timeout) before reading currentUrl, then takes whatever URL is current", async () => {
    vi.useFakeTimers();
    let settledAt: number | null = null;
    const onNavigationSettled = vi.fn(
      (_tabId: number, _quietMs: number, timeoutMs: number) =>
        new Promise<void>((resolve) => {
          setTimeout(() => {
            settledAt = Date.now();
            resolve();
          }, timeoutMs);
        }),
    );
    const tabs = makeFakeTabs({ onNavigationSettled, finalUrlFor: () => "https://mediasite.example.edu/watch/late" });
    const backend = makeFakeBackend({
      ltiCandidates: vi.fn().mockResolvedValue({ courseId: 1, candidates: [candidate(1)] }),
    });
    const { deps } = makeDeps(tabs, backend);

    const summaryPromise = resolveLtiCandidates(deps, ORIGIN, 1);

    // Not settled yet just before the hard cap.
    await vi.advanceTimersByTimeAsync(LTI_SETTLE_TIMEOUT_MS - 1);
    expect(settledAt).toBeNull();
    expect(tabs.currentUrl).not.toHaveBeenCalled();

    // The hard cap fires; currentUrl is read only after that.
    await vi.advanceTimersByTimeAsync(1);
    await summaryPromise;

    expect(settledAt).not.toBeNull();
    expect(tabs.currentUrl).toHaveBeenCalledWith(1);
    expect(backend.reportLtiResolution).toHaveBeenCalledWith(
      expect.objectContaining({ finalUrl: "https://mediasite.example.edu/watch/late", error: null }),
    );
  });
});

// ---------------------------------------------------------------------------
// User-closed-tab path
// ---------------------------------------------------------------------------

describe("resolveLtiCandidates — tab closed before settling", () => {
  it("reports finalUrl: null, error: 'tab closed' when currentUrl comes back null after settling", async () => {
    const tabs = makeFakeTabs({ finalUrlFor: () => null });
    const backend = makeFakeBackend({
      ltiCandidates: vi.fn().mockResolvedValue({ courseId: 1, candidates: [candidate(3)] }),
      reportLtiResolution: vi.fn().mockResolvedValue({ status: "failed" }),
    });
    const { deps } = makeDeps(tabs, backend);

    const summary = await resolveLtiCandidates(deps, ORIGIN, 1);

    expect(backend.reportLtiResolution).toHaveBeenCalledWith({
      orgUnitId: 1,
      materialId: 3,
      finalUrl: null,
      error: "tab closed",
    });
    // close() is still attempted even though the tab is already gone --
    // never assume, always try (best-effort, see the finally block).
    expect(tabs.close).toHaveBeenCalledWith(1);
    expect(summary).toEqual({ resolved: 0, unrecognized: 0, failed: 1, total: 1 });
  });
});

// ---------------------------------------------------------------------------
// Off-origin launchUrl
// ---------------------------------------------------------------------------

describe("resolveLtiCandidates — off-origin launchUrl", () => {
  it("never opens a tab and reports the failure client-side", async () => {
    const tabs = makeFakeTabs();
    const backend = makeFakeBackend({
      ltiCandidates: vi
        .fn()
        .mockResolvedValue({ courseId: 1, candidates: [candidate(1, "https://evil.example/lti/launch")] }),
      reportLtiResolution: vi.fn().mockResolvedValue({ status: "failed" }),
    });
    const { deps } = makeDeps(tabs, backend);

    const summary = await resolveLtiCandidates(deps, ORIGIN, 1);

    expect(tabs.open).not.toHaveBeenCalled();
    expect(tabs.close).not.toHaveBeenCalled();
    expect(backend.reportLtiResolution).toHaveBeenCalledWith({
      orgUnitId: 1,
      materialId: 1,
      finalUrl: null,
      error: "launch URL not on tenant origin",
    });
    expect(summary).toEqual({ resolved: 0, unrecognized: 0, failed: 1, total: 1 });
  });

  it("rejects a lookalike host (prefix-string match would wrongly accept this)", async () => {
    const tabs = makeFakeTabs();
    const backend = makeFakeBackend({
      ltiCandidates: vi
        .fn()
        .mockResolvedValue({ courseId: 1, candidates: [candidate(1, "https://tenant.example.evil.com/lti")] }),
    });
    const { deps } = makeDeps(tabs, backend);

    await resolveLtiCandidates(deps, ORIGIN, 1);

    expect(tabs.open).not.toHaveBeenCalled();
  });

  it("treats a launchUrl that fails to parse the same way (never opens a tab, never throws)", async () => {
    const tabs = makeFakeTabs();
    const backend = makeFakeBackend({
      // "http://" has a scheme but no host -- the WHATWG URL constructor
      // throws for this rather than treating it as relative.
      ltiCandidates: vi.fn().mockResolvedValue({ courseId: 1, candidates: [candidate(1, "http://")] }),
      reportLtiResolution: vi.fn().mockResolvedValue({ status: "failed" }),
    });
    const { deps } = makeDeps(tabs, backend);

    const summary = await resolveLtiCandidates(deps, ORIGIN, 1);

    expect(tabs.open).not.toHaveBeenCalled();
    expect(backend.reportLtiResolution).toHaveBeenCalledWith({
      orgUnitId: 1,
      materialId: 1,
      finalUrl: null,
      error: "launch URL not on tenant origin",
    });
    expect(summary).toEqual({ resolved: 0, unrecognized: 0, failed: 1, total: 1 });
  });
});

// ---------------------------------------------------------------------------
// canonicalizeOrigin (fix-wave item 3)
//
// `resolveOnOriginUrl` compares against `URL.origin`, which is always
// canonical (lowercase host, no trailing slash, default port stripped). The
// `origin` string `resolveLtiCandidates` receives comes from background.ts,
// which reads it back out of the popup message or chrome.storage -- not
// guaranteed to already be canonical. This small pure helper normalizes it;
// it's exported (and lives here, in the pure module) purely because it
// touches zero chrome.* itself, same house rule as everything else in this
// file -- the actual call happens at the adapter seam, in background.ts,
// before `origin` ever reaches `resolveLtiCandidates`.
// ---------------------------------------------------------------------------

describe("canonicalizeOrigin", () => {
  it("lowercases the host", () => {
    expect(canonicalizeOrigin("https://Tenant.example")).toBe("https://tenant.example");
  });

  it("strips a trailing slash", () => {
    expect(canonicalizeOrigin("https://tenant.example/")).toBe("https://tenant.example");
  });

  it("strips the default port for the scheme", () => {
    expect(canonicalizeOrigin("https://tenant.example:443")).toBe("https://tenant.example");
  });

  it("passes an unparseable origin through unchanged", () => {
    // resolveOnOriginUrl's own try/catch + origin-mismatch check then
    // rejects every candidate exactly as it does today for a badly-stored
    // origin -- this helper doesn't hide that, it just doesn't make it worse.
    expect(canonicalizeOrigin("not-a-url")).toBe("not-a-url");
  });
});

describe("resolveLtiCandidates — canonicalized origin (regression, fix-wave item 3)", () => {
  it("a non-canonical origin fails every candidate closed; the canonicalized form resolves normally", async () => {
    // Uppercase host + trailing slash, exactly as chrome.storage might hold
    // it (the popup writes whatever the tenant URL bar showed).
    const rawOrigin = "https://Tenant.example/";
    const tabs = makeFakeTabs({ finalUrlFor: () => "https://mediasite.example.edu/watch/abc" });
    const backend = makeFakeBackend({
      ltiCandidates: vi.fn().mockResolvedValue({ courseId: 1, candidates: [candidate(1, "/d2l/lti/launch/1")] }),
      // Mirrors the real backend's contract closely enough for this test:
      // an error means a client-side rejection (never reached the tab), no
      // error means the launch actually happened and resolved.
      reportLtiResolution: vi.fn((payload: LtiResolutionPayload) =>
        Promise.resolve({ status: payload.error === null ? "resolved" : "failed" } as LtiResolutionResponse),
      ),
    });
    const { deps } = makeDeps(tabs, backend);

    const withRawOrigin = await resolveLtiCandidates(deps, rawOrigin, 1);
    expect(withRawOrigin).toEqual({ resolved: 0, unrecognized: 0, failed: 1, total: 1 });
    expect(tabs.open).not.toHaveBeenCalled();

    const withCanonicalOrigin = await resolveLtiCandidates(deps, canonicalizeOrigin(rawOrigin), 1);
    expect(withCanonicalOrigin).toEqual({ resolved: 1, unrecognized: 0, failed: 0, total: 1 });
    expect(tabs.open).toHaveBeenCalledWith("https://tenant.example/d2l/lti/launch/1");
  });
});

// ---------------------------------------------------------------------------
// Per-candidate error isolation
// ---------------------------------------------------------------------------

describe("resolveLtiCandidates — per-candidate error isolation", () => {
  it("a backend/network error reporting one candidate's resolution does not abort the loop", async () => {
    const tabs = makeFakeTabs();
    const backend = makeFakeBackend({
      ltiCandidates: vi.fn().mockResolvedValue({ courseId: 1, candidates: [candidate(1), candidate(2)] }),
      reportLtiResolution: vi
        .fn()
        .mockRejectedValueOnce(new Error("network down"))
        .mockResolvedValueOnce({ status: "resolved", platform: "zoom", added: 1, total: 1 }),
    });
    const { deps } = makeDeps(tabs, backend);

    const summary = await resolveLtiCandidates(deps, ORIGIN, 1);

    expect(backend.reportLtiResolution).toHaveBeenCalledTimes(2);
    expect(summary).toEqual({ resolved: 1, unrecognized: 0, failed: 1, total: 2 });
  });

  it("a TabDriver error (e.g. tabs.open rejects) is caught, reported as a failure, and the loop continues", async () => {
    const tabs = makeFakeTabs();
    tabs.open = vi
      .fn()
      .mockRejectedValueOnce(new Error("tab creation blocked"))
      .mockImplementationOnce(async (url: string) => {
        tabs.tabsById.set(2, { openedUrl: url, currentUrl: "https://zoom.us/rec/share/xyz", closed: false });
        return 2;
      });
    const backend = makeFakeBackend({
      ltiCandidates: vi.fn().mockResolvedValue({ courseId: 1, candidates: [candidate(1), candidate(2)] }),
      reportLtiResolution: vi
        .fn()
        .mockResolvedValueOnce({ status: "failed" })
        .mockResolvedValueOnce({ status: "resolved", platform: "zoom", added: 1, total: 1 }),
    });
    const { deps } = makeDeps(tabs, backend);

    const summary = await resolveLtiCandidates(deps, ORIGIN, 1);

    expect(backend.reportLtiResolution).toHaveBeenNthCalledWith(1, {
      orgUnitId: 1,
      materialId: 1,
      finalUrl: null,
      error: "tab creation blocked",
    });
    // No tabId was ever obtained for candidate 1 -- close() must not be
    // called for it (nothing to leak, nothing to close).
    expect(tabs.close).toHaveBeenCalledTimes(1);
    expect(tabs.close).toHaveBeenCalledWith(2);
    expect(summary).toEqual({ resolved: 1, unrecognized: 0, failed: 1, total: 2 });
  });

  it("still closes the tab (best-effort) even if close() itself throws", async () => {
    const tabs = makeFakeTabs({ finalUrlFor: () => "https://mediasite.example.edu/watch/z" });
    tabs.close = vi.fn().mockRejectedValue(new Error("tab already gone"));
    const backend = makeFakeBackend({
      ltiCandidates: vi.fn().mockResolvedValue({ courseId: 1, candidates: [candidate(1)] }),
    });
    const { deps } = makeDeps(tabs, backend);

    const summary = await resolveLtiCandidates(deps, ORIGIN, 1);

    expect(tabs.close).toHaveBeenCalledWith(1);
    expect(backend.reportLtiResolution).toHaveBeenCalledWith(
      expect.objectContaining({ finalUrl: "https://mediasite.example.edu/watch/z", error: null }),
    );
    expect(summary.total).toBe(1);
  });

  it("a failure fetching the candidates list itself propagates (not this module's job to swallow — the caller wraps the whole call)", async () => {
    const tabs = makeFakeTabs();
    const backend = makeFakeBackend({ ltiCandidates: vi.fn().mockRejectedValue(new Error("backend unreachable")) });
    const { deps } = makeDeps(tabs, backend);

    await expect(resolveLtiCandidates(deps, ORIGIN, 1)).rejects.toThrow("backend unreachable");
    expect(tabs.open).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Cap at 8
// ---------------------------------------------------------------------------

describe("resolveLtiCandidates — cap", () => {
  it("processes at most 8 candidates per run and leaves the rest for next sync", async () => {
    const tabs = makeFakeTabs();
    const tenCandidates = Array.from({ length: 10 }, (_, i) => candidate(i + 1));
    const backend = makeFakeBackend({
      ltiCandidates: vi.fn().mockResolvedValue({ courseId: 1, candidates: tenCandidates }),
    });
    const { deps } = makeDeps(tabs, backend);

    const summary = await resolveLtiCandidates(deps, ORIGIN, 1);

    expect(LTI_MAX_CANDIDATES_PER_RUN).toBe(8);
    expect(tabs.open).toHaveBeenCalledTimes(8);
    expect(backend.reportLtiResolution).toHaveBeenCalledTimes(8);
    expect(summary.total).toBe(8);
    // The first 8 (in order), not an arbitrary subset.
    expect((tabs.open as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0])).toEqual(
      tenCandidates.slice(0, 8).map((c) => `https://tenant.example${c.launchUrl}`),
    );
  });
});

// ---------------------------------------------------------------------------
// Progress callback sequence
// ---------------------------------------------------------------------------

describe("resolveLtiCandidates — progress callback", () => {
  it("calls onProgress once per candidate, in order, with the running done/total", async () => {
    const tabs = makeFakeTabs();
    const backend = makeFakeBackend({
      ltiCandidates: vi.fn().mockResolvedValue({ courseId: 1, candidates: [candidate(1), candidate(2), candidate(3)] }),
    });
    const { deps, onProgress } = makeDeps(tabs, backend);

    await resolveLtiCandidates(deps, ORIGIN, 1);

    expect(onProgress).toHaveBeenCalledTimes(3);
    expect(onProgress.mock.calls.map((c) => c[0])).toEqual([
      { done: 1, total: 3 },
      { done: 2, total: 3 },
      { done: 3, total: 3 },
    ]);
  });

  it("never calls onProgress when there are no candidates", async () => {
    const tabs = makeFakeTabs();
    const backend = makeFakeBackend();
    const { deps, onProgress } = makeDeps(tabs, backend);

    const summary = await resolveLtiCandidates(deps, ORIGIN, 1);

    expect(onProgress).not.toHaveBeenCalled();
    expect(summary).toEqual({ resolved: 0, unrecognized: 0, failed: 0, total: 0 });
  });
});
