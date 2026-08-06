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

export interface TocPayload {
  orgUnitId: number;
  toc: unknown;
  extras: { news?: unknown[]; dropbox?: unknown[] } | null;
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
