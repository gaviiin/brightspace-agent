import { describe, expect, it, vi } from "vitest";

import { BackendError } from "./backend-client";
import { SessionExpiredError } from "./d2l-client";
import { discover, resume, syncCourse } from "./sync-engine";
import type { BackendClientLike, D2LClientLike, SyncDeps, SyncState } from "./sync-engine";
import type { D2LEnrollmentItem, HandshakePayload, KnownCourse, NeededItem } from "./types";

// ---------------------------------------------------------------------------
// Test helpers — plain fakes, no chrome.*, no real D2LClient/BackendClient
// instances (the whole point of the injected-interface architecture rule).
// ---------------------------------------------------------------------------

function fakeFileResponse(body = "filebytes"): Response {
  return new Response(body, { status: 200 });
}

function makeItem(id: number, lastModified: string | null = null): NeededItem {
  return {
    d2lTopicId: id,
    url: `https://tenant.example/file/${id}`,
    title: `Item ${id}`,
    sizeHint: null,
    lastModified,
  };
}

function courseEnrollment(id: number): D2LEnrollmentItem {
  return {
    OrgUnit: {
      Id: id,
      Name: `Course ${id}`,
      Code: `C${id}`,
      Type: { Id: 3, Code: "Course Offering", Name: "Course Offering" },
    },
  };
}

function makeFakeD2L(overrides: Partial<D2LClientLike> = {}): D2LClientLike {
  return {
    discoverVersions: vi.fn().mockResolvedValue({ lp: "1.43", le: "1.79" }),
    whoami: vi.fn().mockResolvedValue({ Identifier: "u1" }),
    myEnrollments: vi.fn().mockResolvedValue([]),
    courseToc: vi.fn().mockResolvedValue({ Modules: [] }),
    news: vi.fn().mockResolvedValue([]),
    dropboxFolders: vi.fn().mockResolvedValue([]),
    fetchTopicFile: vi.fn().mockResolvedValue(fakeFileResponse()),
    ...overrides,
  };
}

function makeFakeBackend(overrides: Partial<BackendClientLike> = {}): BackendClientLike {
  return {
    handshake: vi.fn().mockResolvedValue({ knownCourses: [] }),
    toc: vi.fn().mockResolvedValue({ syncRunId: 1, needed: [] }),
    uploadFile: vi.fn().mockResolvedValue({ materialId: 1, sha256: "abc", deduped: false }),
    complete: vi.fn().mockResolvedValue({ status: "complete" }),
    ...overrides,
  };
}

function makeDeps(
  d2l: D2LClientLike,
  backend: BackendClientLike,
): { deps: SyncDeps; saveState: ReturnType<typeof vi.fn>; onProgress: ReturnType<typeof vi.fn> } {
  const saveState = vi.fn().mockResolvedValue(undefined);
  const onProgress = vi.fn();
  return { deps: { d2l, backend, saveState, onProgress }, saveState, onProgress };
}

// ---------------------------------------------------------------------------
// discover
// ---------------------------------------------------------------------------

describe("discover", () => {
  it("calls versions -> whoami -> enrollments -> handshake in order, maps EnrollmentIn correctly, and returns knownCourses", async () => {
    const callOrder: string[] = [];
    const d2l = makeFakeD2L({
      discoverVersions: vi.fn().mockImplementation(async () => {
        callOrder.push("versions");
        return { lp: "1.43", le: "1.79" };
      }),
      whoami: vi.fn().mockImplementation(async () => {
        callOrder.push("whoami");
        return { Identifier: "u1" };
      }),
      myEnrollments: vi.fn().mockImplementation(async () => {
        callOrder.push("enrollments");
        return [courseEnrollment(10), courseEnrollment(20)];
      }),
    });
    const knownCourses: KnownCourse[] = [{ orgUnitId: 10, name: "Course 10", courseId: 100 }];
    const backend = makeFakeBackend({
      handshake: vi.fn().mockImplementation(async (_payload: HandshakePayload) => {
        callOrder.push("handshake");
        return { knownCourses };
      }),
    });
    const { deps } = makeDeps(d2l, backend);

    const result = await discover(deps, "https://tenant.example");

    expect(callOrder).toEqual(["versions", "whoami", "enrollments", "handshake"]);
    expect(result).toEqual(knownCourses);
    expect(backend.handshake).toHaveBeenCalledWith({
      tenantOrigin: "https://tenant.example",
      apiVersions: { lp: "1.43", le: "1.79" },
      whoami: { Identifier: "u1" },
      enrollments: [
        { orgUnitId: 10, name: "Course 10", code: "C10" },
        { orgUnitId: 20, name: "Course 20", code: "C20" },
      ],
    });
    expect(d2l.whoami).toHaveBeenCalledWith("1.43");
    expect(d2l.myEnrollments).toHaveBeenCalledWith("1.43");
  });

  it("propagates SessionExpiredError from whoami without calling myEnrollments/handshake", async () => {
    const d2l = makeFakeD2L({ whoami: vi.fn().mockRejectedValue(new SessionExpiredError("expired")) });
    const backend = makeFakeBackend();
    const { deps } = makeDeps(d2l, backend);

    await expect(discover(deps, "https://tenant.example")).rejects.toBeInstanceOf(SessionExpiredError);
    expect(d2l.myEnrollments).not.toHaveBeenCalled();
    expect(backend.handshake).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// syncCourse
// ---------------------------------------------------------------------------

describe("syncCourse", () => {
  it("happy path: uploads every needed item in order, updates state per item, completes with no errors", async () => {
    // item 2 has no lastModified (null) -- checks that the d2lUpdated
    // pass-through omits the header rather than sending "null"/undefined
    // literally when the backend didn't supply one (e.g. a Link-adjacent
    // File topic D2L never stamped).
    const items = [makeItem(1, "2026-01-05T12:00:00.000Z"), makeItem(2, null), makeItem(3, "2026-01-10T09:00:00.000Z")];
    const fetchOrder: number[] = [];
    const uploadOrder: number[] = [];
    const d2l = makeFakeD2L({
      fetchTopicFile: vi.fn().mockImplementation(async (_le: string, _orgUnitId: number, topicId: number) => {
        fetchOrder.push(topicId);
        return fakeFileResponse(`body-${topicId}`);
      }),
    });
    const backend = makeFakeBackend({
      toc: vi.fn().mockResolvedValue({ syncRunId: 42, needed: items }),
      uploadFile: vi.fn().mockImplementation(async (_syncRunId: number, topicId: number) => {
        uploadOrder.push(topicId);
        return { materialId: topicId, sha256: `sha-${topicId}`, deduped: false };
      }),
      complete: vi.fn().mockResolvedValue({ status: "complete" }),
    });
    const { deps, saveState, onProgress } = makeDeps(d2l, backend);

    const state = await syncCourse(deps, "https://tenant.example", 555);

    expect(fetchOrder).toEqual([1, 2, 3]);
    expect(uploadOrder).toEqual([1, 2, 3]);
    // uploadFile's meta carries each needed item's lastModified through as
    // d2lUpdated -- this is the fix under test: without it, d2l_updated_at
    // never gets set server-side and incremental sync is defeated.
    expect(backend.uploadFile).toHaveBeenNthCalledWith(1, 42, 1, expect.anything(), {
      sourceUrl: items[0].url,
      title: items[0].title,
      d2lUpdated: "2026-01-05T12:00:00.000Z",
    });
    expect(backend.uploadFile).toHaveBeenNthCalledWith(2, 42, 2, expect.anything(), {
      sourceUrl: items[1].url,
      title: items[1].title,
      d2lUpdated: undefined,
    });
    expect(backend.uploadFile).toHaveBeenNthCalledWith(3, 42, 3, expect.anything(), {
      sourceUrl: items[2].url,
      title: items[2].title,
      d2lUpdated: "2026-01-10T09:00:00.000Z",
    });
    expect(backend.complete).toHaveBeenCalledTimes(1);
    expect(backend.complete).toHaveBeenCalledWith({ syncRunId: 42, errors: [] });
    expect(state).toEqual({
      syncRunId: 42,
      orgUnitId: 555,
      queue: [],
      done: 3,
      total: 3,
      errors: [],
      phase: "complete",
    });

    // saveState/onProgress: once per item (3) plus once for the final
    // running -> complete transition.
    expect(saveState).toHaveBeenCalledTimes(4);
    expect(saveState.mock.calls.map((call) => (call[0] as SyncState).done)).toEqual([1, 2, 3, 3]);
    expect(saveState.mock.calls.map((call) => (call[0] as SyncState).phase)).toEqual([
      "running",
      "running",
      "running",
      "complete",
    ]);
    expect(onProgress).toHaveBeenCalledTimes(4);
    expect(onProgress).toHaveBeenLastCalledWith(
      expect.objectContaining({ done: 3, total: 3, errorCount: 0, phase: "complete" }),
    );
  });

  it("per-file failure: records the error against that d2lTopicId, keeps looping, and completes with 1 error", async () => {
    const items = [makeItem(1), makeItem(2), makeItem(3)];
    const d2l = makeFakeD2L();
    const backend = makeFakeBackend({
      toc: vi.fn().mockResolvedValue({ syncRunId: 7, needed: items }),
      uploadFile: vi
        .fn()
        .mockResolvedValueOnce({ materialId: 1, sha256: "a", deduped: false })
        .mockRejectedValueOnce(new BackendError(500, "boom"))
        .mockResolvedValueOnce({ materialId: 3, sha256: "c", deduped: false }),
    });
    const { deps } = makeDeps(d2l, backend);

    const state = await syncCourse(deps, "https://tenant.example", 1);

    expect(backend.uploadFile).toHaveBeenCalledTimes(3);
    expect(state.errors).toEqual([{ d2lTopicId: 2, message: expect.stringContaining("boom") }]);
    expect(state.done).toBe(3);
    expect(state.phase).toBe("complete");
    expect(backend.complete).toHaveBeenCalledWith({
      syncRunId: 7,
      errors: [{ d2lTopicId: 2, message: expect.stringContaining("boom") }],
    });
  });

  it("SessionExpiredError mid-queue: saves phase 'needs-login' with the remaining queue intact and does not call complete", async () => {
    const items = [makeItem(1), makeItem(2), makeItem(3)];
    const d2l = makeFakeD2L({
      fetchTopicFile: vi
        .fn()
        .mockResolvedValueOnce(fakeFileResponse())
        .mockRejectedValueOnce(new SessionExpiredError("session gone")),
    });
    const backend = makeFakeBackend({ toc: vi.fn().mockResolvedValue({ syncRunId: 9, needed: items }) });
    const { deps, saveState } = makeDeps(d2l, backend);

    const state = await syncCourse(deps, "https://tenant.example", 1);

    expect(state.phase).toBe("needs-login");
    // Item 1 succeeded and was removed; item 2 (the one that hit
    // SessionExpiredError) and item 3 remain untouched in the queue.
    expect(state.queue).toEqual([items[1], items[2]]);
    expect(state.done).toBe(1);
    expect(backend.complete).not.toHaveBeenCalled();
    expect(saveState).toHaveBeenLastCalledWith(expect.objectContaining({ phase: "needs-login" }));
    // Only 1 upload attempted (item 1) -- item 2 died in fetchTopicFile
    // before ever reaching backend.uploadFile.
    expect(backend.uploadFile).toHaveBeenCalledTimes(1);
  });

  it("includes news/dropbox extras from the d2l client in the toc payload", async () => {
    const newsItems = [{ id: 1, title: "n", html: "<p>hi</p>" }];
    const dropboxItems = [{ id: 2, name: "d1" }];
    const d2l = makeFakeD2L({
      courseToc: vi.fn().mockResolvedValue({ Modules: [] }),
      news: vi.fn().mockResolvedValue(newsItems),
      dropboxFolders: vi.fn().mockResolvedValue(dropboxItems),
    });
    const backend = makeFakeBackend({ toc: vi.fn().mockResolvedValue({ syncRunId: 1, needed: [] }) });
    const { deps } = makeDeps(d2l, backend);

    await syncCourse(deps, "https://tenant.example", 1);

    expect(backend.toc).toHaveBeenCalledWith({
      orgUnitId: 1,
      toc: { Modules: [] },
      extras: { news: newsItems, dropbox: dropboxItems },
    });
  });

  it("SessionExpiredError from discoverVersions (session already dead before /toc) pauses instead of throwing", async () => {
    const d2l = makeFakeD2L({
      discoverVersions: vi.fn().mockRejectedValue(new SessionExpiredError("dead on arrival")),
    });
    const backend = makeFakeBackend();
    const { deps, saveState } = makeDeps(d2l, backend);

    const state = await syncCourse(deps, "https://tenant.example", 555);

    expect(state.phase).toBe("needs-login");
    expect(state.syncRunId).toBeNull();
    expect(state.orgUnitId).toBe(555);
    expect(backend.toc).not.toHaveBeenCalled();
    expect(backend.complete).not.toHaveBeenCalled();
    expect(saveState).toHaveBeenLastCalledWith(
      expect.objectContaining({ phase: "needs-login", syncRunId: null, orgUnitId: 555 }),
    );
  });

  it("SessionExpiredError from courseToc (session dies after discoverVersions but before /toc) pauses instead of throwing", async () => {
    const d2l = makeFakeD2L({
      courseToc: vi.fn().mockRejectedValue(new SessionExpiredError("expired mid-toc")),
    });
    const backend = makeFakeBackend();
    const { deps } = makeDeps(d2l, backend);

    const state = await syncCourse(deps, "https://tenant.example", 42);

    expect(state.phase).toBe("needs-login");
    expect(state.syncRunId).toBeNull();
    expect(backend.toc).not.toHaveBeenCalled();
    expect(backend.complete).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// resume
// ---------------------------------------------------------------------------

describe("resume", () => {
  it("continues from the remaining queue only (already-done items not re-fetched) and completes with the same syncRunId", async () => {
    const remaining = [makeItem(2), makeItem(3)];
    const priorState: SyncState = {
      syncRunId: 9,
      orgUnitId: 1,
      queue: remaining,
      done: 1,
      total: 3,
      errors: [],
      phase: "needs-login",
    };
    const fetchedIds: number[] = [];
    const d2l = makeFakeD2L({
      fetchTopicFile: vi.fn().mockImplementation(async (_le: string, _orgUnitId: number, topicId: number) => {
        fetchedIds.push(topicId);
        return fakeFileResponse();
      }),
    });
    const backend = makeFakeBackend({ complete: vi.fn().mockResolvedValue({ status: "complete" }) });
    const { deps } = makeDeps(d2l, backend);

    const state = await resume(deps, "https://tenant.example", priorState);

    expect(fetchedIds).toEqual([2, 3]);
    expect(backend.toc).not.toHaveBeenCalled();
    expect(backend.complete).toHaveBeenCalledWith({ syncRunId: 9, errors: [] });
    expect(state.phase).toBe("complete");
    expect(state.done).toBe(3);
    expect(state.total).toBe(3);
    expect(state.queue).toEqual([]);
  });

  it("SessionExpiredError from discoverVersions pauses again without throwing: queue intact, no uploads attempted", async () => {
    const remaining = [makeItem(2), makeItem(3)];
    const priorState: SyncState = {
      syncRunId: 9,
      orgUnitId: 1,
      queue: remaining,
      done: 1,
      total: 3,
      errors: [],
      phase: "needs-login",
    };
    const d2l = makeFakeD2L({
      discoverVersions: vi.fn().mockRejectedValue(new SessionExpiredError("still expired")),
    });
    const backend = makeFakeBackend();
    const { deps, saveState } = makeDeps(d2l, backend);

    const state = await resume(deps, "https://tenant.example", priorState);

    expect(state.phase).toBe("needs-login");
    expect(state.syncRunId).toBe(9);
    expect(state.queue).toEqual(remaining);
    expect(d2l.fetchTopicFile).not.toHaveBeenCalled();
    expect(backend.uploadFile).not.toHaveBeenCalled();
    expect(backend.toc).not.toHaveBeenCalled();
    expect(backend.complete).not.toHaveBeenCalled();
    expect(saveState).toHaveBeenLastCalledWith(expect.objectContaining({ phase: "needs-login", syncRunId: 9 }));
  });

  it("with syncRunId null (never established), retries syncCourse from scratch instead of draining an empty queue", async () => {
    const priorState: SyncState = {
      syncRunId: null,
      orgUnitId: 7,
      queue: [],
      done: 0,
      total: 0,
      errors: [],
      phase: "needs-login",
    };
    const items = [makeItem(1)];
    const d2l = makeFakeD2L();
    const backend = makeFakeBackend({ toc: vi.fn().mockResolvedValue({ syncRunId: 100, needed: items }) });
    const { deps } = makeDeps(d2l, backend);

    const state = await resume(deps, "https://tenant.example", priorState);

    // A full syncCourse pipeline ran (courseToc/toc, not just a drain of
    // the empty persisted queue) and got a real syncRunId this time.
    expect(backend.toc).toHaveBeenCalledWith(expect.objectContaining({ orgUnitId: 7 }));
    expect(state.syncRunId).toBe(100);
    expect(state.phase).toBe("complete");
    expect(backend.complete).toHaveBeenCalledWith({ syncRunId: 100, errors: [] });
  });
});
