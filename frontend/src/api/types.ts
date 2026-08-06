// Wire types for the backend's frontend-facing JSON APIs. Field names and
// shapes mirror the backend exactly (see backend/src/brightspace_agent/api/
// courses.py, graph.py, pipeline.py, events.py) — this file has no logic of
// its own, just the contract.

// ---------------------------------------------------------------------------
// GET /api/courses/{id}/graph  (backend/src/brightspace_agent/graph/build.py)
// ---------------------------------------------------------------------------

/** The synthetic "everything unfiled" topic id (see graph/build.py). */
export const UNSORTED_TOPIC_ID = 0;

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

export interface GraphEdge {
  fromTopicId: number;
  toTopicId: number;
  relation: "prerequisite" | "related";
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
  meta: { taxonomyVersion: number; orphanCount: number };
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

export type BsaEvent = PipelineBsaEvent | SyncBsaEvent;
