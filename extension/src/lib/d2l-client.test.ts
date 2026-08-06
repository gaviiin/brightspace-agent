import { describe, expect, it, vi } from "vitest";

import {
  D2LClient,
  D2LError,
  RateLimitedFetcher,
  RateLimitError,
  SessionExpiredError,
} from "./d2l-client";
import type { D2LEnrollmentItem, D2LPagedResultSet, D2LVersionInfo } from "./types";

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

/** sleepImpl stand-in that resolves instantly — no real waiting in tests. */
const instantSleep = vi.fn().mockResolvedValue(undefined);

function jsonResponse(
  body: unknown,
  init: { status?: number; headers?: Record<string, string> } = {},
): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
}

/** Flush microtasks until `predicate` holds, without any real timers. */
async function waitUntil(predicate: () => boolean, maxTicks = 200): Promise<void> {
  for (let i = 0; i < maxTicks && !predicate(); i++) {
    await Promise.resolve();
  }
  if (!predicate()) {
    throw new Error("condition not met within microtask budget");
  }
}

/** Flush a fixed number of microtask ticks — no real timers — to give a
 * (possibly broken) gate every reasonable chance to advance before we
 * assert it didn't. */
async function flushMicrotasks(ticks = 20): Promise<void> {
  for (let i = 0; i < ticks; i++) {
    await Promise.resolve();
  }
}

function courseEnrollment(id: number, typeCode = "Course Offering"): D2LEnrollmentItem {
  return {
    OrgUnit: {
      Id: id,
      Name: `Course ${id}`,
      Code: `C${id}`,
      Type: { Id: 3, Code: typeCode, Name: typeCode },
    },
  };
}

// ---------------------------------------------------------------------------
// D2LClient
// ---------------------------------------------------------------------------

describe("D2LClient.discoverVersions", () => {
  it("picks LatestVersion for lp and le", async () => {
    const versions: D2LVersionInfo[] = [
      { ProductCode: "lp", LatestVersion: "1.43", SupportedVersions: ["1.0", "1.43"] },
      { ProductCode: "le", LatestVersion: "1.79", SupportedVersions: ["1.0", "1.79"] },
      { ProductCode: "other", LatestVersion: "9.9", SupportedVersions: ["9.9"] },
    ];
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(versions));
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl: instantSleep });
    const client = new D2LClient("https://tenant.example", fetcher);

    const result = await client.discoverVersions();

    expect(result).toEqual({ lp: "1.43", le: "1.79" });
    expect(fetchImpl).toHaveBeenCalledWith(
      "https://tenant.example/d2l/api/versions/",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("throws D2LError when a required product is missing", async () => {
    const versions: D2LVersionInfo[] = [
      { ProductCode: "lp", LatestVersion: "1.43", SupportedVersions: ["1.43"] },
    ];
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(versions));
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl: instantSleep });
    const client = new D2LClient("https://tenant.example", fetcher);

    await expect(client.discoverVersions()).rejects.toBeInstanceOf(D2LError);
  });
});

describe("D2LClient.myEnrollments", () => {
  it("follows bookmark paging across two pages and concatenates course items", async () => {
    const page1: D2LPagedResultSet<D2LEnrollmentItem> = {
      PagingInfo: { Bookmark: "b1", HasMoreItems: true },
      Items: [courseEnrollment(1), courseEnrollment(2, "Semester")],
    };
    const page2: D2LPagedResultSet<D2LEnrollmentItem> = {
      PagingInfo: { Bookmark: "", HasMoreItems: false },
      Items: [courseEnrollment(3)],
    };
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(page1))
      .mockResolvedValueOnce(jsonResponse(page2));
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl: instantSleep });
    const client = new D2LClient("https://tenant.example", fetcher);

    const result = await client.myEnrollments("1.43");

    expect(result.map((item) => item.OrgUnit.Id)).toEqual([1, 3]);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    const secondCallUrl = fetchImpl.mock.calls[1][0] as string;
    expect(secondCallUrl).toContain("bookmark=b1");
  });

  it("accepts Type.Code 'Course Offering' and rejects Type.Code 'Semester'", async () => {
    const page: D2LPagedResultSet<D2LEnrollmentItem> = {
      PagingInfo: { Bookmark: "", HasMoreItems: false },
      Items: [courseEnrollment(1, "Course Offering"), courseEnrollment(2, "Semester")],
    };
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(page));
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl: instantSleep });
    const client = new D2LClient("https://tenant.example", fetcher);

    const result = await client.myEnrollments("1.43");

    expect(result).toHaveLength(1);
    expect(result[0].OrgUnit.Id).toBe(1);
  });
});

describe("D2LClient.fetchTopicFile", () => {
  it("hits the exact URL and passes credentials: 'include'", async () => {
    const response = new Response("filebytes", { status: 200 });
    const fetchImpl = vi.fn().mockResolvedValue(response);
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl: instantSleep });
    const client = new D2LClient("https://tenant.example", fetcher);

    const result = await client.fetchTopicFile("1.79", 12345, 987);

    expect(result).toBe(response);
    expect(fetchImpl).toHaveBeenCalledWith(
      "https://tenant.example/d2l/api/le/1.79/12345/content/topics/987/file",
      expect.objectContaining({ credentials: "include" }),
    );
  });
});

describe("D2LClient.news / D2LClient.dropboxFolders", () => {
  it("news() reshapes D2L's PascalCase items into the backend's extras contract", async () => {
    // The real Valence shape: PascalCase, body nested under Body as a
    // {Text, Html} pair. The backend's /toc contract is {id, title, html},
    // so this mapping is what stands between a real tenant and a 422 that
    // fails the whole course sync.
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse([
        {
          Id: 42,
          Title: "Midterm date announced",
          Body: { Text: "Week 6.", Html: "<p>Week 6.</p>" },
          IsGlobal: false,
        },
      ]),
    );
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl: instantSleep });
    const client = new D2LClient("https://tenant.example", fetcher);

    await expect(client.news("1.79", 111)).resolves.toEqual([
      { id: 42, title: "Midterm date announced", html: "<p>Week 6.</p>" },
    ]);
  });

  it("news() defends against missing Body/Title and drops items with no numeric Id", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse([
        { Id: 1 }, // no Body, no Title at all
        { Id: 2, Title: "Text only", Body: { Text: "plain" } }, // no Html rendering
        { Title: "No id" }, // unusable: nothing to key d2l:news:{id} on
        null, // not even an object
      ]),
    );
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl: instantSleep });
    const client = new D2LClient("https://tenant.example", fetcher);

    await expect(client.news("1.79", 111)).resolves.toEqual([
      { id: 1, title: "", html: "" },
      { id: 2, title: "Text only", html: "" },
    ]);
  });

  it("dropboxFolders() reshapes CustomInstructions.Text into instructionsText", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse([
        {
          Id: 7,
          Name: "Homework 1",
          CustomInstructions: { Text: "Submit a PDF.", Html: "<p>Submit a PDF.</p>" },
          IsHidden: false,
        },
        { Id: 8, Name: "Homework 2" }, // no instructions at all
      ]),
    );
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl: instantSleep });
    const client = new D2LClient("https://tenant.example", fetcher);

    await expect(client.dropboxFolders("1.79", 111)).resolves.toEqual([
      { id: 7, name: "Homework 1", instructionsText: "Submit a PDF." },
      { id: 8, name: "Homework 2", instructionsText: null },
    ]);
  });

  it("returns [] when the tenant answers with something that isn't an array", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({ Errors: ["nope"] }));
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl: instantSleep });
    const client = new D2LClient("https://tenant.example", fetcher);

    await expect(client.news("1.79", 111)).resolves.toEqual([]);
    await expect(client.dropboxFolders("1.79", 111)).resolves.toEqual([]);
  });

  it("news() returns [] on a 500 response instead of throwing", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response("boom", { status: 500 }));
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl: instantSleep });
    const client = new D2LClient("https://tenant.example", fetcher);

    await expect(client.news("1.79", 111)).resolves.toEqual([]);
  });

  it("dropboxFolders() returns [] on a 500 response instead of throwing", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response("boom", { status: 500 }));
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl: instantSleep });
    const client = new D2LClient("https://tenant.example", fetcher);

    await expect(client.dropboxFolders("1.79", 111)).resolves.toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// RateLimitedFetcher
// ---------------------------------------------------------------------------

describe("RateLimitedFetcher retry/backoff", () => {
  it("waits Retry-After seconds on 429 then retries and succeeds", async () => {
    const sleepImpl = vi.fn().mockResolvedValue(undefined);
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 429, headers: { "Retry-After": "2" } }))
      .mockResolvedValueOnce(new Response("ok", { status: 200 }));
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl });

    const response = await fetcher.fetch("https://x.example/y");

    expect(response.status).toBe(200);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(sleepImpl).toHaveBeenCalledWith(2000);
  });

  it("uses jittered exponential backoff without Retry-After, throwing RateLimitError once maxAttempts is exhausted", async () => {
    const sleepImpl = vi.fn().mockResolvedValue(undefined);
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 429 }));
    const randomSpy = vi.spyOn(Math, "random").mockReturnValue(0.5);
    const fetcher = new RateLimitedFetcher({
      fetchImpl,
      sleepImpl,
      maxAttempts: 3,
      baseBackoffMs: 1000,
    });

    await expect(fetcher.fetch("https://x.example/y")).rejects.toBeInstanceOf(RateLimitError);

    // 3 HTTP attempts total (maxAttempts, including the first try), 2 backoff sleeps between them.
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    expect(sleepImpl).toHaveBeenCalledTimes(2);
    expect(sleepImpl).toHaveBeenNthCalledWith(1, 500); // 0.5 * 1000 * 2^0
    expect(sleepImpl).toHaveBeenNthCalledWith(2, 1000); // 0.5 * 1000 * 2^1

    randomSpy.mockRestore();
  });

  it("falls back to the backoff delay when Retry-After isn't a usable number", async () => {
    // An HTTP-date is a legal Retry-After, and a proxy can put anything
    // there. Number()-ing either gives NaN, and sleep(NaN) resolves on the
    // next tick -- turning the backoff into a hot retry loop against a
    // server that just asked us to slow down.
    for (const header of ["Wed, 21 Oct 2015 07:28:00 GMT", "soon", "", "-5"]) {
      const sleepImpl = vi.fn().mockResolvedValue(undefined);
      const fetchImpl = vi
        .fn()
        .mockResolvedValueOnce(new Response(null, { status: 429, headers: { "Retry-After": header } }))
        .mockResolvedValueOnce(new Response("ok", { status: 200 }));
      const randomSpy = vi.spyOn(Math, "random").mockReturnValue(0.5);
      const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl, baseBackoffMs: 1000 });

      const response = await fetcher.fetch("https://x.example/y");

      expect(response.status).toBe(200);
      expect(sleepImpl).toHaveBeenCalledTimes(1);
      expect(sleepImpl).toHaveBeenCalledWith(500); // 0.5 * 1000 * 2^0, not NaN
      randomSpy.mockRestore();
    }
  });

  it("clamps an absurd Retry-After to the 120s ceiling", async () => {
    const sleepImpl = vi.fn().mockResolvedValue(undefined);
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 429, headers: { "Retry-After": "86400" } }))
      .mockResolvedValueOnce(new Response("ok", { status: 200 }));
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl });

    await fetcher.fetch("https://x.example/y");

    expect(sleepImpl).toHaveBeenCalledWith(120_000);
  });

  it("throws SessionExpiredError on 401 without retrying", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 401 }));
    const sleepImpl = vi.fn().mockResolvedValue(undefined);
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl });

    await expect(fetcher.fetch("https://x.example/y")).rejects.toBeInstanceOf(SessionExpiredError);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(sleepImpl).not.toHaveBeenCalled();
  });

  it("throws SessionExpiredError on 403 without retrying", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 403 }));
    const sleepImpl = vi.fn().mockResolvedValue(undefined);
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl });

    await expect(fetcher.fetch("https://x.example/y")).rejects.toBeInstanceOf(SessionExpiredError);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});

describe("RateLimitedFetcher concurrency cap", () => {
  it("never runs more than maxConcurrent requests in flight", async () => {
    let inFlight = 0;
    let maxObserved = 0;
    const releasers: Array<() => void> = [];
    const fetchImpl = vi.fn().mockImplementation(() => {
      inFlight++;
      maxObserved = Math.max(maxObserved, inFlight);
      return new Promise<Response>((resolve) => {
        releasers.push(() => {
          inFlight--;
          resolve(new Response("ok", { status: 200 }));
        });
      });
    });
    const fetcher = new RateLimitedFetcher({ fetchImpl, sleepImpl: instantSleep, maxConcurrent: 2 });

    const results = Array.from({ length: 5 }, (_, i) => fetcher.fetch(`https://x.example/${i}`));

    await waitUntil(() => fetchImpl.mock.calls.length === 2);
    expect(maxObserved).toBeLessThanOrEqual(2);
    expect(releasers).toHaveLength(2);

    releasers[0]();
    await waitUntil(() => fetchImpl.mock.calls.length === 3);
    expect(maxObserved).toBeLessThanOrEqual(2);

    releasers[1]();
    await waitUntil(() => fetchImpl.mock.calls.length === 4);
    expect(maxObserved).toBeLessThanOrEqual(2);

    releasers[2]();
    await waitUntil(() => fetchImpl.mock.calls.length === 5);
    expect(maxObserved).toBeLessThanOrEqual(2);

    releasers[3]();
    releasers[4]();
    await Promise.all(results);

    expect(maxObserved).toBeLessThanOrEqual(2);
  });
});

describe("RateLimitedFetcher rate-limit cooldown", () => {
  it("blocks the next request until the cooldown resolves, sharing one cooldown across requests", async () => {
    // A controllable (non-auto-resolving) sleepImpl: fetchImpl for a queued
    // request must NOT be called while this promise is still pending, which
    // is the actual safety property the cooldown provides. An
    // auto-resolving sleepImpl can't prove that — it can't distinguish
    // "waited for the cooldown" from "the gate was never there".
    let resolveCooldown!: () => void;
    const cooldownPromise = new Promise<void>((resolve) => {
      resolveCooldown = resolve;
    });
    const sleepImpl = vi.fn().mockReturnValue(cooldownPromise);
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(
        new Response("ok", { status: 200, headers: { "X-Rate-Limit-Remaining": "5" } }),
      )
      .mockResolvedValueOnce(new Response("ok", { status: 200 }))
      .mockResolvedValueOnce(new Response("ok", { status: 200 }));
    const fetcher = new RateLimitedFetcher({
      fetchImpl,
      sleepImpl,
      minRemainingThreshold: 20,
      baseBackoffMs: 1000,
    });

    // Request 1 reports low remaining and completes normally — the cooldown
    // delays *subsequent* requests, not the one that reported it.
    await fetcher.fetch("https://x.example/1");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(sleepImpl).toHaveBeenCalledWith(1000);

    // Requests 2 and 3 start while the cooldown is still pending.
    const request2 = fetcher.fetch("https://x.example/2");
    const request3 = fetcher.fetch("https://x.example/3");
    await flushMicrotasks(50);

    // The cooldown must still be gating them: if `await this.cooldown` were
    // missing (or a no-op), fetchImpl would already have been called here.
    expect(fetchImpl).toHaveBeenCalledTimes(1);

    resolveCooldown();
    await Promise.all([request2, request3]);

    expect(fetchImpl).toHaveBeenCalledTimes(3);
    // Shared cooldown, not per-request: only one cooldown sleep for both
    // requests that came after the low-remaining response.
    expect(sleepImpl).toHaveBeenCalledTimes(1);
  });

  it("does not delay when X-Rate-Limit-Remaining stays above the threshold", async () => {
    const sleepImpl = vi.fn().mockResolvedValue(undefined);
    const fetchImpl = vi.fn().mockResolvedValue(
      new Response("ok", { status: 200, headers: { "X-Rate-Limit-Remaining": "50" } }),
    );
    const fetcher = new RateLimitedFetcher({
      fetchImpl,
      sleepImpl,
      minRemainingThreshold: 20,
      baseBackoffMs: 1000,
    });

    await fetcher.fetch("https://x.example/1");
    await fetcher.fetch("https://x.example/2");

    expect(sleepImpl).not.toHaveBeenCalled();
  });

  // Regression: the default fetchImpl is invoked as `this.fetchImpl(...)`, so
  // an unbound global fetch receives the fetcher instance as `this` and the
  // browser's native implementation throws "Illegal invocation". Every other
  // test injects a plain mock, which is indifferent to `this` — only a
  // this-sensitive stub reproduces what Chrome does.
  it("calls the default global fetch bound to the global scope", async () => {
    const original = globalThis.fetch;
    const calls: string[] = [];
    globalThis.fetch = function (this: unknown, input: string | URL | Request) {
      if (this !== globalThis && this !== undefined) {
        throw new TypeError("Failed to execute 'fetch': Illegal invocation");
      }
      calls.push(String(input));
      return Promise.resolve(new Response("ok", { status: 200 }));
    } as unknown as typeof fetch;

    try {
      const fetcher = new RateLimitedFetcher();
      const response = await fetcher.fetch("https://x.example/whoami");

      expect(response.status).toBe(200);
      expect(calls).toEqual(["https://x.example/whoami"]);
    } finally {
      globalThis.fetch = original;
    }
  });
});
