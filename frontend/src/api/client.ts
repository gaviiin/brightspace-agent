// Typed fetch wrapper for the backend's frontend-facing APIs. Relative
// paths only -- the vite dev proxy forwards /api to the backend in dev, and
// in production the backend serves the built frontend itself (same
// origin), so no base URL is ever needed.
import type {
  BsaEvent,
  CourseSummary,
  DryRunResponse,
  GraphPayload,
  PipelineRunResponse,
  PipelineStatusResponse,
} from "./types";

// Mutating endpoints under /api/ (besides /api/ingest/*, which the
// extension authenticates via the pairing token instead) require this
// header -- see main.py's CSRF guard.
const CSRF_HEADERS = { "X-BSA-Request": "1" };

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // Response body wasn't JSON (or was empty) -- fall back to statusText.
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function getCourses(): Promise<CourseSummary[]> {
  return request<CourseSummary[]>("/api/courses");
}

export function getCourse(courseId: number): Promise<CourseSummary> {
  return request<CourseSummary>(`/api/courses/${courseId}`);
}

export function getGraph(courseId: number): Promise<GraphPayload> {
  return request<GraphPayload>(`/api/courses/${courseId}/graph`);
}

export function runPipeline(courseId: number, stages?: string[]): Promise<PipelineRunResponse> {
  return request<PipelineRunResponse>(`/api/courses/${courseId}/pipeline/run`, {
    method: "POST",
    headers: { ...CSRF_HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify(stages ? { stages } : {}),
  });
}

export function dryRun(courseId: number): Promise<DryRunResponse> {
  return request<DryRunResponse>(`/api/courses/${courseId}/pipeline/dry-run`, {
    method: "POST",
    headers: CSRF_HEADERS,
  });
}

export function pipelineStatus(courseId: number): Promise<PipelineStatusResponse> {
  return request<PipelineStatusResponse>(`/api/courses/${courseId}/pipeline/status`);
}

/** Subscribe to the backend's SSE event stream. Returns the EventSource so
 * the caller can `.close()` it (e.g. on unmount). Malformed events are
 * dropped silently rather than throwing, so one bad frame can't kill the
 * subscription. */
export function openEvents(onEvent: (event: BsaEvent) => void): EventSource {
  const source = new EventSource("/api/events");
  source.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data) as BsaEvent);
    } catch {
      // Ignore malformed events (e.g. SSE heartbeat comments never reach
      // onmessage, but be defensive anyway).
    }
  };
  return source;
}
