// Wire types for the backend's frontend-facing JSON APIs. Field names and
// shapes mirror the backend exactly (see backend/src/brightspace_agent/api/
// courses.py, graph.py, pipeline.py, events.py) — this file has no logic of
// its own, just the contract.

// ---------------------------------------------------------------------------
// GET /api/courses/{id}/graph  (backend/src/brightspace_agent/graph/build.py)
// ---------------------------------------------------------------------------

/** The synthetic "everything unfiled" topic id (see graph/build.py). */
export const UNSORTED_TOPIC_ID = 0;

/** The synthetic "Logistics & admin" topic id (M3.5a; see graph/build.py). */
export const ADMIN_TOPIC_ID = -1;

export type MaterialKind =
  | "syllabus"
  | "slides"
  | "document"
  | "assignment"
  | "announcement"
  | "video"
  | "transcript"
  | "link"
  | "other";

export interface GraphTopic {
  id: number;
  slug: string;
  name: string;
  description: string;
  orderIndex: number;
  materialCount: number;
}

export interface GraphMaterial {
  id: number;
  title: string;
  kind: MaterialKind;
  status: string;
  maxConfidence: number | null;
}

export type TopicEdgeRelation = "prerequisite" | "related";

export interface GraphEdge {
  fromTopicId: number;
  toTopicId: number;
  relation: TopicEdgeRelation;
}

export interface GraphAttachment {
  topicId: number;
  materialId: number;
  confidence: number | null;
  rationale: string | null;
}

export interface GraphPayload {
  topics: GraphTopic[];
  materials: GraphMaterial[];
  topicEdges: GraphEdge[];
  attachments: GraphAttachment[];
  // `adminCount` (M3.5a) is optional here so existing fixtures/tests that
  // predate it keep type-checking; the backend always sends it. UI wiring
  // for the admin bucket itself is left to a later task.
  meta: { taxonomyVersion: number; orphanCount: number; adminCount?: number };
}

// ---------------------------------------------------------------------------
// GET /api/materials/{id}  (api/materials.py)
// ---------------------------------------------------------------------------

export interface MaterialDetail {
  id: number;
  courseId: number;
  title: string;
  kind: MaterialKind;
  status: string;
  mime: string | null;
  sizeBytes: number | null;
  sourceUrl: string | null;
  summary: string | null;
  keyTerms: string[];
  topicIds: number[];
}

// ---------------------------------------------------------------------------
// GET /api/courses, GET /api/courses/{id}  (api/courses.py)
// ---------------------------------------------------------------------------

export interface MaterialCounts {
  total: number;
  summarized: number;
  failed: number;
}

export interface PipelineSummary {
  status: string;
  stage: string | null;
}

export interface CourseSummary {
  id: number;
  orgUnitId: number;
  name: string;
  code: string | null;
  term: string | null;
  taxonomyVersion: number;
  lastSyncedAt: string | null;
  materialCounts: MaterialCounts;
  pipeline: PipelineSummary | null;
}

// ---------------------------------------------------------------------------
// PUT /api/courses/{id}/taxonomy  (api/taxonomy.py, pipeline/taxonomy_apply.py)
// ---------------------------------------------------------------------------

export interface TaxonomyEditTopic {
  id: number | null;
  name: string;
  description: string;
  mergedFromTopicIds: number[];
}

export interface TaxonomyEditEdge {
  fromIndex: number;
  toIndex: number;
  relation: TopicEdgeRelation;
}

export interface TaxonomyEditRequest {
  topics: TaxonomyEditTopic[];
  edges: TaxonomyEditEdge[];
}

export interface TaxonomyApplyResponse {
  taxonomyVersion: number;
  reclassify: boolean;
  runToken: number | null;
}

// ---------------------------------------------------------------------------
// Pipeline control (api/pipeline.py)
// ---------------------------------------------------------------------------

export interface PipelineRunResponse {
  runToken: number;
}

export interface StageStatus {
  stage: string;
  status: string;
  startedAt: string;
  finishedAt: string | null;
  usage: Record<string, unknown> | null;
}

export interface PipelineStatusResponse {
  active: boolean;
  stages: StageStatus[];
}

export interface StageDryRun {
  calls: number;
  estCostUsd: number;
}

export interface DryRunResponse {
  byStage: Record<string, StageDryRun>;
  totalEstCostUsd: number;
}

// ---------------------------------------------------------------------------
// GET /api/courses/{id}/runs  (api/courses.py)
// ---------------------------------------------------------------------------

export interface SyncRunError {
  d2lTopicId: number | null;
  message: string;
}

export interface SyncRunSummary {
  id: number;
  source: string;
  status: string;
  startedAt: string;
  finishedAt: string | null;
  files: number;
  bytes: number;
  notNeeded: number;
  /** Full error total; `errors` itself is capped server-side at five. */
  errorCount: number;
  errors: SyncRunError[];
}

export interface PipelineRunSummary {
  id: number;
  stage: string;
  status: string;
  startedAt: string;
  finishedAt: string | null;
  inputTokens: number;
  outputTokens: number;
  estCostUsd: number;
  error: string | null;
}

export interface RunsResponse {
  syncRuns: SyncRunSummary[];
  pipelineRuns: PipelineRunSummary[];
}

// ---------------------------------------------------------------------------
// GET /api/events (SSE)  (api/events.py, pipeline/runner.py, api/ingest.py)
// ---------------------------------------------------------------------------

export interface PipelineBsaEvent {
  type: "pipeline";
  courseId: number;
  runToken: number;
  stage: string | null;
  status: string;
  stats?: Record<string, unknown>;
}

export interface SyncBsaEvent {
  type: "sync";
  courseId: number;
  syncRunId: number;
  status: string;
}

/** `topicId` is present for a single-topic enrichment run, absent for a
 * course-wide batch (see runner.py's `_EnrichmentRunHooks` /
 * api/enrichment.py's module docstring). `status` is
 * "run-started" | "complete" | "aborted" | "failed" (typed as `string`
 * here, matching PipelineBsaEvent's own looseness). */
export interface EnrichmentBsaEvent {
  type: "enrichment";
  courseId: number;
  runToken: number;
  topicId?: number;
  status: string;
  stats?: Record<string, unknown>;
}

/** `sourceId` is present for a per-source status change (fetching/
 * transcribing/done/failed -- the `media_sources.status` values), absent
 * for a run-level event (`status` then one of run-started/complete/failed
 * -- see runner.py's `_MediaRunHooks`). */
export interface MediaBsaEvent {
  type: "media";
  courseId: number;
  runToken: number;
  sourceId?: number;
  status: string;
}

export type BsaEvent = PipelineBsaEvent | SyncBsaEvent | EnrichmentBsaEvent | MediaBsaEvent;

// ---------------------------------------------------------------------------
// Enrichment (api/enrichment.py) -- M3.3
// ---------------------------------------------------------------------------

export type EnrichmentStatus = "suggested" | "kept" | "dismissed";

export interface EnrichmentResource {
  id: number;
  url: string;
  title: string | null;
  resourceType: string | null;
  intent: string | null;
  rationale: string | null;
  scores: Record<string, number>;
  verification: Record<string, unknown>;
  rank: number | null;
  shared: boolean;
  status: EnrichmentStatus;
}

export interface EnrichmentMeta {
  suggested: number;
  kept: number;
  dismissed: number;
  /** True once an enrichment run has COMPLETED for this topic's current
   * content (api/enrichment.py derives it from the enrich stage's cache
   * row). Lets the empty state say "not searched yet" or "searched, found
   * nothing" instead of one line that could mean either. */
  searched: boolean;
  /** The completed run found fewer good resources than it aimed for. Only
   * meaningful when `searched` is true. */
  thin: boolean;
}

export interface TopicEnrichment {
  topicId: number;
  resources: EnrichmentResource[];
  meta: EnrichmentMeta;
}

export interface EnrichRunResponse {
  runToken: number;
}

export interface EnrichDryRunResponse {
  topicsNeedingEnrichment: number;
  callsPerTopic: number;
  estCostPerTopicUsd: number;
  totalEstCostUsd: number;
  /** Upper bound on billable web searches per topic. `web_search` is billed
   * per search (~$0.01) on top of tokens, and at that rate it dominates
   * `estCostPerTopicUsd` -- so the confirm dialog shows it rather than
   * leaving the number unexplained. */
  webSearchesPerTopic: number;
}

// ---------------------------------------------------------------------------
// Media (api/media.py) -- M2.5
// ---------------------------------------------------------------------------

export type MediaPlatform = "mediasite" | "zoom" | "gdrive";

export type MediaSourceStatus = "detected" | "fetching" | "transcribing" | "done" | "failed" | "skipped";

export interface MediaSourceSummary {
  id: number;
  /** Both null for a manually-added row (POST .../media/add -- M2.6a): it
   * has no backing `materials` row at all (see api/media.py's
   * `MediaSourceOut`). */
  materialId: number | null;
  materialTitle: string | null;
  platform: MediaPlatform;
  url: string;
  /** Local single-user app, no masking (see api/media.py's `MediaSourceOut`). */
  passcode: string | null;
  status: MediaSourceStatus;
  error: string | null;
  transcriptMaterialId: number | null;
  updatedAt: string;
}

/** A link material that looks like an LTI-embedded recording channel the
 * sync structurally can't read into (api/media.py's `_compute_lti_hints`) --
 * points the user at the manual-add workaround instead. */
export interface MediaHint {
  materialId: number;
  title: string;
}

export interface MediaListResponse {
  sources: MediaSourceSummary[];
  /** Any run (pipeline/enrichment/media) active for the course -- runner.py
   * shares one `_active` guard across all three, same meaning as
   * `PipelineStatusResponse.active`. */
  active: boolean;
  hints: MediaHint[];
}

export interface MediaDetectResponse {
  scannedMaterials: number;
  found: number;
  added: number;
}

export interface MediaRunResponse {
  runToken: number;
}

/** `POST /api/courses/{id}/media/add` body (M2.6a) -- a pasted recording or
 * channel/catalog URL, with an optional passcode applied to any
 * zoom-classified row(s) it expands into. */
export interface MediaAddRequest {
  url: string;
  passcode?: string | null;
}

export interface MediaAddResponse {
  added: number;
  skipped: number;
  total: number;
  sources: MediaSourceSummary[];
}

/** `PUT /api/media/{id}` body. Both fields are optional-and-distinct-from-
 * `null` on the wire (an absent field means "leave alone"; the backend's
 * `MediaSourceUpdate` rejects an explicit `status: null`) -- `passcode:
 * null` is the one meaningful null, clearing a stored passcode. */
export interface MediaSourceUpdateRequest {
  passcode?: string | null;
  status?: "skipped" | "detected";
}

// ---------------------------------------------------------------------------
// GET /api/settings  (api/settings.py)
// ---------------------------------------------------------------------------

export interface SettingsModels {
  fast: string;
  smart: string;
}

export interface SettingsResponse {
  pairingToken: string;
  dataDir: string;
  models: SettingsModels;
  mockLlm: boolean;
  maxCostUsdPerRun: number;
  apiKeyConfigured: boolean;
}
