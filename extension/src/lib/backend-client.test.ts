import { afterEach, describe, expect, it, vi } from "vitest";

import { BackendClient, BackendError } from "./backend-client";

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// uploadFile
// ---------------------------------------------------------------------------

describe("BackendClient.uploadFile", () => {
  it("posts to /api/ingest/file with query params, headers, and the source response's bytes", async () => {
    const sourceResponse = new Response("filebytes", { headers: { "Content-Type": "application/pdf" } });
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ materialId: 5, sha256: "abc123", deduped: false }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new BackendClient("http://127.0.0.1:8730", async () => "tok123");

    const result = await client.uploadFile(42, 7, sourceResponse, {
      sourceUrl: "https://tenant.example/file/7",
      title: "Café Notes",
      d2lUpdated: "2026-01-01T00:00:00Z",
    });

    expect(result).toEqual({ materialId: 5, sha256: "abc123", deduped: false });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit & { duplex?: string; headers: Record<string, string> }];
    expect(url).toBe("http://127.0.0.1:8730/api/ingest/file?syncRunId=42&d2lTopicId=7");
    expect(init.method).toBe("POST");
    expect(init.headers.Authorization).toBe("Bearer tok123");
    expect(init.headers["X-Source-Url"]).toBe("https://tenant.example/file/7");
    expect(init.headers["X-Title"]).toBe(encodeURIComponent("Café Notes"));
    expect(init.headers["X-D2L-Updated"]).toBe("2026-01-01T00:00:00Z");
    expect(init.headers["Content-Type"]).toBe("application/pdf");
    // Buffered, never streamed: Chrome rejects a ReadableStream request body
    // over HTTP/1.1 (which the local backend speaks) with "Failed to fetch"
    // before the request leaves the browser.
    expect(init.duplex).toBeUndefined();
    expect(init.body).toBeInstanceOf(Blob);
    expect(await (init.body as Blob).text()).toBe("filebytes");
  });

  it("omits X-D2L-Updated when not provided", async () => {
    const sourceResponse = new Response("bytes");
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ materialId: 1, sha256: "x", deduped: true }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new BackendClient("http://127.0.0.1:8730", async () => "tok");

    await client.uploadFile(1, 2, sourceResponse, { sourceUrl: "u", title: "t" });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit & { headers: Record<string, string> }];
    expect(init.headers["X-D2L-Updated"]).toBeUndefined();
  });

  it("sends an empty body when the source response has none", async () => {
    const sourceResponse = new Response(null, { status: 204 });
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ materialId: 1, sha256: "x", deduped: true }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new BackendClient("http://127.0.0.1:8730", async () => "tok");

    await client.uploadFile(1, 2, sourceResponse, { sourceUrl: "u", title: "t" });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit & { duplex?: string; body?: unknown }];
    expect(init.duplex).toBeUndefined();
    expect(init.body).toBeInstanceOf(Blob);
  });
});

// ---------------------------------------------------------------------------
// Auth + error handling across all methods
// ---------------------------------------------------------------------------

describe("BackendClient auth + error handling", () => {
  it("attaches the bearer token on every method", async () => {
    // A fresh Response per call -- a Response body can only be read once,
    // and each BackendClient method call reads its own.
    const fetchMock = vi
      .fn()
      .mockImplementation(() => jsonResponse({ ok: true, knownCourses: [], syncRunId: 1, needed: [] }));
    vi.stubGlobal("fetch", fetchMock);
    const getToken = vi.fn().mockResolvedValue("secret-token");
    const client = new BackendClient("http://127.0.0.1:8730", getToken);

    await client.health();
    await client.handshake({ tenantOrigin: "o", apiVersions: {}, whoami: {}, enrollments: [] });
    await client.toc({ orgUnitId: 1, toc: {}, extras: null });
    await client.complete({ syncRunId: 1, errors: [] });

    expect(fetchMock).toHaveBeenCalledTimes(4);
    for (const call of fetchMock.mock.calls) {
      const init = call[1] as RequestInit & { headers: Record<string, string> };
      expect(init.headers.Authorization).toBe("Bearer secret-token");
    }
    expect(getToken).toHaveBeenCalledTimes(4);
  });

  it("throws BackendError with the response status and parsed detail on non-2xx", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "invalid pairing token" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new BackendClient("http://127.0.0.1:8730", async () => "bad-token");

    const error = await client.health().catch((e: unknown) => e);

    expect(error).toBeInstanceOf(BackendError);
    expect((error as BackendError).status).toBe(401);
    expect((error as BackendError).detail).toBe("invalid pairing token");
  });

  it("throws BackendError even when the error body isn't JSON", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("plain text failure", { status: 500 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new BackendClient("http://127.0.0.1:8730", async () => "tok");

    const error = await client.health().catch((e: unknown) => e);

    expect(error).toBeInstanceOf(BackendError);
    expect((error as BackendError).status).toBe(500);
  });
});

// ---------------------------------------------------------------------------
// health
// ---------------------------------------------------------------------------

describe("BackendClient.health", () => {
  it("returns the parsed {status, paired} body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "ok", paired: true }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new BackendClient("http://127.0.0.1:8730", async () => "tok");

    const result = await client.health();

    expect(result).toEqual({ status: "ok", paired: true });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8730/api/health",
      expect.objectContaining({ method: "GET" }),
    );
  });
});

// ---------------------------------------------------------------------------
// ltiCandidates / reportLtiResolution (M2.7)
// ---------------------------------------------------------------------------

describe("BackendClient.ltiCandidates", () => {
  it("GETs /api/ingest/lti-candidates with orgUnitId as a query param and the bearer token", async () => {
    const body = {
      courseId: 5,
      candidates: [{ materialId: 1, title: "Lecture 1", launchUrl: "/d2l/lti/launch/1" }],
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(body));
    vi.stubGlobal("fetch", fetchMock);
    const client = new BackendClient("http://127.0.0.1:8730", async () => "tok123");

    const result = await client.ltiCandidates(42);

    expect(result).toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8730/api/ingest/lti-candidates?orgUnitId=42",
      expect.objectContaining({ method: "GET" }),
    );
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit & { headers: Record<string, string> }];
    expect(init.headers.Authorization).toBe("Bearer tok123");
  });

  it("throws BackendError on a non-2xx response", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "unknown course; run handshake first" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new BackendClient("http://127.0.0.1:8730", async () => "tok");

    const error = await client.ltiCandidates(999).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(BackendError);
    expect((error as BackendError).status).toBe(404);
  });
});

describe("BackendClient.reportLtiResolution", () => {
  it("POSTs the payload to /api/ingest/lti-resolution with the bearer token", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "resolved", platform: "mediasite", added: 3, total: 3 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new BackendClient("http://127.0.0.1:8730", async () => "tok123");

    const result = await client.reportLtiResolution({
      orgUnitId: 42,
      materialId: 7,
      finalUrl: "https://mediasite.example.edu/watch/abc",
      error: null,
    });

    expect(result).toEqual({ status: "resolved", platform: "mediasite", added: 3, total: 3 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit & { headers: Record<string, string>; body: string }];
    expect(url).toBe("http://127.0.0.1:8730/api/ingest/lti-resolution");
    expect(init.method).toBe("POST");
    expect(init.headers.Authorization).toBe("Bearer tok123");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(init.body)).toEqual({
      orgUnitId: 42,
      materialId: 7,
      finalUrl: "https://mediasite.example.edu/watch/abc",
      error: null,
    });
  });

  it("resolves the parsed body for the 'unrecognized' outcome, with platform/added/total absent (not null)", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "unrecognized" }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new BackendClient("http://127.0.0.1:8730", async () => "tok");

    const result = await client.reportLtiResolution({
      orgUnitId: 1,
      materialId: 2,
      finalUrl: "https://tenant.example/some/landing",
      error: null,
    });

    expect(result).toEqual({ status: "unrecognized" });
    expect("platform" in result).toBe(false);
  });

  it("propagates a non-2xx expand failure as BackendError rather than a JSON outcome", async () => {
    // Per Task 1: an expand failure (e.g. yt-dlp missing) returns a plain
    // non-2xx, not a {status: ...} body -- the resolver's per-candidate
    // error isolation must treat this exactly like any other network error.
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "yt-dlp is not installed" }), {
        status: 503,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new BackendClient("http://127.0.0.1:8730", async () => "tok");

    const error = await client
      .reportLtiResolution({ orgUnitId: 1, materialId: 2, finalUrl: "https://zoom.us/rec/x", error: null })
      .catch((e: unknown) => e);

    expect(error).toBeInstanceOf(BackendError);
    expect((error as BackendError).status).toBe(503);
  });
});

// ---------------------------------------------------------------------------
// pairRequest / pairClaim (M2.7 one-click pairing)
//
// Both are called before the extension has a pairing token -- that's the
// whole point of this flow -- so unlike every method above, neither may
// attach an Authorization header.
// ---------------------------------------------------------------------------

describe("BackendClient.pairRequest", () => {
  it("POSTs to /api/pair/request with the CSRF header and no Authorization header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ requestId: "abc123" }));
    vi.stubGlobal("fetch", fetchMock);
    const getToken = vi.fn().mockResolvedValue("should-not-be-used");
    const client = new BackendClient("http://127.0.0.1:8730", getToken);

    const result = await client.pairRequest();

    expect(result).toEqual({ requestId: "abc123" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit & { headers?: Record<string, string> }];
    expect(url).toBe("http://127.0.0.1:8730/api/pair/request");
    expect(init.method).toBe("POST");
    expect(init.headers?.["X-BSA-Request"]).toBe("1");
    expect(init.headers?.Authorization).toBeUndefined();
    expect(getToken).not.toHaveBeenCalled();
  });

  it("throws BackendError on a non-2xx response", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "missing X-BSA-Request header" }), {
        status: 403,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new BackendClient("http://127.0.0.1:8730", async () => "tok");

    const error = await client.pairRequest().catch((e: unknown) => e);

    expect(error).toBeInstanceOf(BackendError);
    expect((error as BackendError).status).toBe(403);
  });
});

describe("BackendClient.pairClaim", () => {
  it("GETs /api/pair/claim with requestId as a query param and no Authorization header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "pending" }));
    vi.stubGlobal("fetch", fetchMock);
    const getToken = vi.fn().mockResolvedValue("should-not-be-used");
    const client = new BackendClient("http://127.0.0.1:8730", getToken);

    const result = await client.pairClaim("req-id-1");

    expect(result).toEqual({ status: "pending" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit & { headers?: Record<string, string> }];
    expect(url).toBe("http://127.0.0.1:8730/api/pair/claim?requestId=req-id-1");
    expect(init.method).toBe("GET");
    expect(init.headers?.Authorization).toBeUndefined();
    expect(getToken).not.toHaveBeenCalled();
  });

  it("URL-encodes the requestId", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "pending" }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new BackendClient("http://127.0.0.1:8730", async () => "tok");

    await client.pairClaim("a b+c");

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("http://127.0.0.1:8730/api/pair/claim?requestId=a%20b%2Bc");
  });

  it("resolves {status: 'approved', pairingToken} once approved", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "approved", pairingToken: "secret-tok" }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new BackendClient("http://127.0.0.1:8730", async () => "tok");

    const result = await client.pairClaim("req-id-1");

    expect(result).toEqual({ status: "approved", pairingToken: "secret-tok" });
  });

  it("throws BackendError on a non-2xx response (e.g. unknown/expired requestId)", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "unknown or expired pairing request" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new BackendClient("http://127.0.0.1:8730", async () => "tok");

    const error = await client.pairClaim("req-id-1").catch((e: unknown) => e);

    expect(error).toBeInstanceOf(BackendError);
    expect((error as BackendError).status).toBe(404);
  });
});
