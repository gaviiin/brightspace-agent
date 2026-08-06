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
