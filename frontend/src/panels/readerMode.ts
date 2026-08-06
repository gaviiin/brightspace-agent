// Pure dispatch: given a material's kind/status/mime, which preview should
// MaterialReader render? No React imports (only a type-only import from
// api/types, erased at build time) -- unit-testable without a DOM.
import type { MaterialKind } from "../api/types";

export type ReaderMode = "pdf-iframe" | "text" | "link-out" | "video-link" | "download" | "none";

export interface ReaderModeInput {
  kind: MaterialKind;
  status: string;
  mime: string | null;
}

// Kinds with no browser-native preview whose "preview" is their extracted
// text sidecar (see materials.py's /text endpoint) once the pipeline has
// gotten far enough to have extracted it. Includes slides/pptx and docx --
// neither renders in a browser -- alongside kinds that are just plain text
// to begin with.
const TEXT_KINDS: ReadonlySet<MaterialKind> = new Set([
  "document",
  "transcript",
  "announcement",
  "assignment",
  "syllabus",
  "slides",
]);

const TEXT_STATUSES: ReadonlySet<string> = new Set(["extracted", "summarized"]);

/**
 * Chooses what MaterialReader renders. Order matters -- earlier branches
 * take precedence over later ones (see the Task 11 brief):
 *   1. `mime === "application/pdf"` -> pdf-iframe, regardless of kind or
 *      status. A PDF slide deck still gets the native PDF viewer, not the
 *      extracted-text fallback in (2) -- and even a `link`-kind material
 *      that somehow resolved to a PDF blob gets the PDF viewer, not (3).
 *   2. A text-bearing kind (see TEXT_KINDS) that's been extracted or
 *      summarized -> text.
 *   3. `kind === "link"` -> link-out, even if a blob happens to exist for
 *      it (beats the download fallback in (5)).
 *   4. `kind === "video"` -> video-link (an anchor to the source URL, not
 *      the blob -- video files aren't proxied through the backend).
 *   5. Anything else with a blob on disk (`mime !== null`, since the
 *      ingest layer only ever sets mime alongside sha256/size when a blob
 *      was actually stored -- see ingest/repo.py) -> download.
 *   6. Otherwise -> none ("no preview").
 */
export function chooseReaderMode(material: ReaderModeInput): ReaderMode {
  if (material.mime === "application/pdf") return "pdf-iframe";
  if (TEXT_KINDS.has(material.kind) && TEXT_STATUSES.has(material.status)) return "text";
  if (material.kind === "link") return "link-out";
  if (material.kind === "video") return "video-link";
  if (material.mime !== null) return "download";
  return "none";
}
