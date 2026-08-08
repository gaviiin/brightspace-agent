// Typed fetch wrapper for the backend's frontend-facing APIs. Relative
// paths only -- the vite dev proxy forwards /api to the backend in dev, and
// in production the backend serves the built frontend itself (same
// origin), so no base URL is ever needed.
import type {
  BsaEvent,
  CourseSummary,
  DryRunResponse,
  EnrichDryRunResponse,
  EnrichRunResponse,
  EnrichmentResource,
  EnrichmentStatus,
  GraphPayload,
  MaterialDetail,
  PipelineRunResponse,
  PipelineStatusResponse,
  RunsResponse,
  SettingsResponse,
  TaxonomyApplyResponse,
  TaxonomyEditRequest,
  TopicEnrichment,
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

export function getMaterial(materialId: number): Promise<MaterialDetail> {
  return request<MaterialDetail>(`/api/materials/${materialId}`);
}

/** Saves a taxonomy edit (Task 12). The response says whether the backend
 * took the patch path (`reclassify: false`, applied immediately) or the
 * structural path (`reclassify: true`, `runToken` set -- the existing SSE/
 * refetch machinery picks up the resulting graph change). */
export function putTaxonomy(courseId: number, body: TaxonomyEditRequest): Promise<TaxonomyApplyResponse> {
  return request<TaxonomyApplyResponse>(`/api/courses/${courseId}/taxonomy`, {
    method: "PUT",
    headers: { ...CSRF_HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** The extracted-text sidecar's URL (plain text, not JSON -- MaterialReader
 * fetches it directly rather than through `request()`). */
export function getMaterialTextUrl(materialId: number): string {
  return `/api/materials/${materialId}/text`;
}

/** The raw blob's URL -- used as an `<iframe>` src (PDFs) or a download
 * anchor's `href`, never fetched as JSON. */
export function getMaterialFileUrl(materialId: number): string {
  return `/api/materials/${materialId}/file`;
}

export function getSettings(): Promise<SettingsResponse> {
  return request<SettingsResponse>("/api/settings");
}

export function getRuns(courseId: number): Promise<RunsResponse> {
  return request<RunsResponse>(`/api/courses/${courseId}/runs`);
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

// ---------------------------------------------------------------------------
// Enrichment (api/enrichment.py) -- M3.3
// ---------------------------------------------------------------------------

export function getTopicEnrichment(topicId: number): Promise<TopicEnrichment> {
  return request<TopicEnrichment>(`/api/topics/${topicId}/enrichment`);
}

export function enrichTopic(topicId: number): Promise<EnrichRunResponse> {
  return request<EnrichRunResponse>(`/api/topics/${topicId}/enrich`, {
    method: "POST",
    headers: CSRF_HEADERS,
  });
}

export function enrichCourse(courseId: number): Promise<EnrichRunResponse> {
  return request<EnrichRunResponse>(`/api/courses/${courseId}/enrich`, {
    method: "POST",
    headers: CSRF_HEADERS,
  });
}

export function setEnrichmentStatus(resourceId: number, status: EnrichmentStatus): Promise<EnrichmentResource> {
  return request<EnrichmentResource>(`/api/enrichment/${resourceId}`, {
    method: "PUT",
    headers: { ...CSRF_HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
}

/** A GET, unlike the pipeline's own dry-run (a POST) -- api/enrichment.py's
 * module docstring: it only reads the DB, so it stays open to the same
 * browser+CORS rules as other GETs and doesn't need the CSRF header. */
export function enrichDryRun(courseId: number): Promise<EnrichDryRunResponse> {
  return request<EnrichDryRunResponse>(`/api/courses/${courseId}/enrich/dry-run`);
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
