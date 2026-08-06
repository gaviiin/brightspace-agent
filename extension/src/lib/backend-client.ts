// The extension-facing client for the local BrightSpace Agent backend's
// ingest API (Task 3: backend/src/brightspace_agent/api/ingest.py). Plain
// fetch-based -- no chrome.* APIs -- so it runs both inside the MV3 service
// worker and under vitest in Node. Wire format is camelCase JSON, matching
// types.ts field-for-field.

import type {
  CompletePayload,
  HandshakePayload,
  HandshakeResponse,
  TocPayload,
  TocResponse,
} from "./types";

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/** A non-2xx response from the backend. `detail` is FastAPI's error body's
 * `detail` field when the body is JSON shaped that way, else the response's
 * status text. */
export class BackendError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(`Backend request failed (HTTP ${status}): ${detail}`);
    this.name = "BackendError";
  }
}

// ---------------------------------------------------------------------------
// BackendClient
// ---------------------------------------------------------------------------

export interface UploadFileMeta {
  sourceUrl: string;
  title: string;
  d2lUpdated?: string;
}

export interface UploadFileResult {
  materialId: number;
  sha256: string;
  deduped: boolean;
}

export interface HealthResponse {
  status: string;
  paired: boolean;
}

export class BackendClient {
  constructor(
    private readonly baseUrl: string = "http://127.0.0.1:8730",
    private readonly getToken: () => Promise<string>,
  ) {}

  health(): Promise<HealthResponse> {
    return this.request<HealthResponse>("GET", "/api/health");
  }

  handshake(payload: HandshakePayload): Promise<HandshakeResponse> {
    return this.request<HandshakeResponse>("POST", "/api/ingest/handshake", payload);
  }

  toc(payload: TocPayload): Promise<TocResponse> {
    return this.request<TocResponse>("POST", "/api/ingest/toc", payload);
  }

  /** Streams `res`'s body straight through to POST /api/ingest/file — the
   * caller (sync-engine.ts) never buffers a D2L file in memory. */
  async uploadFile(
    syncRunId: number,
    d2lTopicId: number,
    res: Response,
    meta: UploadFileMeta,
  ): Promise<UploadFileResult> {
    const token = await this.getToken();
    const url = `${this.baseUrl}/api/ingest/file?syncRunId=${syncRunId}&d2lTopicId=${d2lTopicId}`;

    const headers: Record<string, string> = {
      Authorization: `Bearer ${token}`,
      "X-Source-Url": meta.sourceUrl,
      // HTTP header values must stay ASCII; the backend decodes this
      // defensively (RFC 2047 or percent-encoding) so percent-encoding a
      // possibly-Unicode title here is always safe to send.
      "X-Title": encodeURIComponent(meta.title),
    };
    const contentType = res.headers.get("Content-Type");
    if (contentType) headers["Content-Type"] = contentType;
    if (meta.d2lUpdated) headers["X-D2L-Updated"] = meta.d2lUpdated;

    const init: RequestInit & { duplex?: "half" } = { method: "POST", headers };
    if (res.body) {
      // Chrome requires `duplex: 'half'` on any fetch() carrying a
      // streamed (ReadableStream) request body.
      init.body = res.body;
      init.duplex = "half";
    } else {
      // No stream to pipe (e.g. an empty/204 source response) -- fall back
      // to buffering whatever's left via blob.
      init.body = await res.blob();
    }

    const response = await fetch(url, init);
    return this.parseJsonOrThrow<UploadFileResult>(response);
  }

  complete(payload: CompletePayload): Promise<{ status: string }> {
    return this.request<{ status: string }>("POST", "/api/ingest/complete", payload);
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const token = await this.getToken();
    const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
    let requestBody: string | undefined;
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      requestBody = JSON.stringify(body);
    }
    const response = await fetch(`${this.baseUrl}${path}`, { method, headers, body: requestBody });
    return this.parseJsonOrThrow<T>(response);
  }

  private async parseJsonOrThrow<T>(response: Response): Promise<T> {
    if (!response.ok) {
      let detail = response.statusText || `HTTP ${response.status}`;
      try {
        const data: unknown = await response.json();
        if (data && typeof data === "object" && typeof (data as { detail?: unknown }).detail === "string") {
          detail = (data as { detail: string }).detail;
        }
      } catch {
        // Non-JSON (or empty) error body -- keep the statusText fallback.
      }
      throw new BackendError(response.status, detail);
    }
    return response.json() as Promise<T>;
  }
}
