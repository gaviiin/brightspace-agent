// Wire types for the extension's two neighbors:
//   - the backend ingest API (Task 3, camelCase JSON) — these interfaces are
//     a field-for-field mirror of backend/src/brightspace_agent/api/ingest.py.
//     The backend is the source of truth for these names; do not rename
//     fields here without updating the backend contract first.
//   - the Brightspace (D2L Valence) REST API — the subset of response shapes
//     d2l-client.ts actually reads.

// ---------------------------------------------------------------------------
// Backend contract (camelCase)
// ---------------------------------------------------------------------------

export interface EnrollmentIn {
  orgUnitId: number;
  name: string;
  code: string | null;
}

export interface HandshakePayload {
  tenantOrigin: string;
  apiVersions: Record<string, string>;
  whoami: unknown;
  enrollments: EnrollmentIn[];
}

export interface KnownCourse {
  orgUnitId: number;
  name: string;
  courseId: number;
}

export interface HandshakeResponse {
  knownCourses: KnownCourse[];
}

/** One announcement, already reshaped from D2L's PascalCase news item into
 * the backend's `NewsExtra` contract (see api/ingest.py). */
export interface NewsExtra {
  id: number;
  title: string;
  html: string;
}

/** One assignment folder, already reshaped from D2L's PascalCase dropbox
 * folder into the backend's `DropboxExtra` contract. */
export interface DropboxExtra {
  id: number;
  name: string;
  instructionsText: string | null;
}

export interface TocPayload {
  orgUnitId: number;
  toc: unknown;
  extras: { news?: NewsExtra[]; dropbox?: DropboxExtra[] } | null;
}

export interface NeededItem {
  d2lTopicId: number;
  url: string;
  title: string;
  sizeHint: number | null;
  lastModified: string | null;
}

export interface TocResponse {
  syncRunId: number;
  needed: NeededItem[];
}

export interface CompletePayload {
  syncRunId: number;
  errors: { d2lTopicId: number | null; message: string }[];
}

/** One still-unresolved LTI quicklink material — mirrors `LtiCandidateOut`
 * in api/ingest.py. `launchUrl` may be relative to the tenant origin. */
export interface LtiCandidate {
  materialId: number;
  title: string;
  launchUrl: string;
}

/** `GET /api/ingest/lti-candidates` — mirrors `LtiCandidatesResponse`. */
export interface LtiCandidatesResponse {
  courseId: number;
  candidates: LtiCandidate[];
}

/** `POST /api/ingest/lti-resolution` request body — mirrors
 * `LtiResolutionRequest`. */
export interface LtiResolutionPayload {
  orgUnitId: number;
  materialId: number;
  finalUrl: string | null;
  error: string | null;
}

export type LtiResolutionStatus = "resolved" | "unrecognized" | "failed";

/** `POST /api/ingest/lti-resolution` response body — mirrors
 * `LtiResolutionResponse`. The backend route uses
 * `response_model_exclude_none`, so `platform`/`added`/`total` are ABSENT
 * (not null) on the 'unrecognized'/'failed' outcomes -- optional here,
 * never assume they're present without checking `status` first. */
export interface LtiResolutionResponse {
  status: LtiResolutionStatus;
  platform?: string;
  added?: number;
  total?: number;
}

// ---------------------------------------------------------------------------
// D2L shapes (subset we rely on)
// ---------------------------------------------------------------------------

export interface D2LVersionInfo {
  ProductCode: string;
  LatestVersion: string;
  SupportedVersions: string[];
}

export interface D2LEnrollmentItem {
  OrgUnit: {
    Id: number;
    Name: string;
    Code: string | null;
    Type: { Id: number; Code: string; Name: string };
  };
}

export interface D2LPagedResultSet<T> {
  PagingInfo: { Bookmark: string; HasMoreItems: boolean };
  Items: T[];
}

/** A rich-text field as Valence returns it (news bodies, dropbox custom
 * instructions): both a plain-text and an HTML rendering, either of which
 * a tenant may omit. */
export interface D2LRichText {
  Text?: string | null;
  Html?: string | null;
}

/** `GET /d2l/api/le/{le}/{orgUnitId}/news/` — PascalCase, with the body
 * nested under `Body`. Every field is optional here on purpose: this is
 * the untrusted wire shape, and d2l-client.ts reshapes it defensively into
 * the backend's `NewsExtra`. */
export interface D2LNewsItem {
  Id?: number | null;
  Title?: string | null;
  Body?: D2LRichText | null;
}

/** `GET /d2l/api/le/{le}/{orgUnitId}/dropbox/folders/` — PascalCase, with
 * the instructions nested under `CustomInstructions`. */
export interface D2LDropboxFolder {
  Id?: number | null;
  Name?: string | null;
  CustomInstructions?: D2LRichText | null;
}
