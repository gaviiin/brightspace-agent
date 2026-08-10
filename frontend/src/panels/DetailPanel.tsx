import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { getMaterial } from "../api/client";
import type { GraphAttachment, GraphPayload, GraphTopic } from "../api/types";
import { ADMIN_TOPIC_ID, UNSORTED_TOPIC_ID } from "../api/types";
import { KIND_ICON } from "../graph/nodes/MaterialNode";
import { isSafeHttpUrl } from "../lib/url";
import { useUiStore } from "../state/uiStore";
import { MaterialReader } from "./MaterialReader";
import { TopicSupplementary } from "./TopicSupplementary";

interface DetailPanelProps {
  payload: GraphPayload;
  /** Threaded down to TopicSupplementary (M3.3) -- GraphPayload has no
   * courseId of its own, and the enrichment dry-run/course-enrich
   * endpoints are course-scoped. */
  courseId: number;
  mockLlm: boolean;
  /** Mirrors TaxonomyEditor's `pipelineActive`: CourseWorkspacePage's
   * `active` already reflects an enrichment run too (runner.py shares one
   * `_active` guard per course between pipeline and enrichment runs). */
  runActive: boolean;
}

/** The right-hand detail/reader panel: shows whatever is currently
 * selected in `uiStore` -- a topic (description, related-topic edges,
 * attached materials) or a material (metadata, summary, key terms, and the
 * `MaterialReader` preview). Reads topic/material/attachment data straight
 * off the shared graph payload (the same react-query cache the graph and
 * outline use) -- the only extra network call is `getMaterial`, which
 * carries fields the graph payload doesn't (summary, key terms, sourceUrl,
 * mime), not a duplicate of it. */
export function DetailPanel({ payload, courseId, mockLlm, runActive }: DetailPanelProps) {
  const selection = useUiStore((state) => state.selection);
  const selectTopic = useUiStore((state) => state.selectTopic);
  const selectMaterial = useUiStore((state) => state.selectMaterial);
  const setSelection = useUiStore((state) => state.setSelection);

  if (selection === null) {
    return (
      <p className="text-sm text-neutral-500 dark:text-neutral-400">
        Select a topic or material to see its details here.
      </p>
    );
  }

  if (selection.type === "topic") {
    return (
      <TopicDetail
        payload={payload}
        topicId={selection.id}
        courseId={courseId}
        mockLlm={mockLlm}
        runActive={runActive}
        onSelectTopic={selectTopic}
        onSelectMaterial={selectMaterial}
      />
    );
  }

  return (
    <MaterialDetail
      payload={payload}
      materialId={selection.id}
      onSelectTopic={selectTopic}
      onJumpToMaterial={(materialId) => setSelection({ type: "material", id: materialId })}
    />
  );
}

// ---------------------------------------------------------------------------
// Topic selected
// ---------------------------------------------------------------------------

interface EdgeChip {
  key: string;
  label: "requires" | "required by" | "related";
  otherTopicId: number;
  otherTopicName: string;
}

/** Edge chips for a topic, in both directions. `prerequisite` edges are
 * directional (see agents/schemas.py: "from_slug must be understood before
 * to_slug"), so a topic that is the EDGE'S TARGET requires the other one
 * ("requires: X"), while a topic that is the edge's SOURCE is a
 * prerequisite for the other one ("required by: X") -- `related` edges are
 * shown the same way regardless of direction, since they're symmetric. */
function edgeChipsForTopic(payload: GraphPayload, topicId: number): EdgeChip[] {
  const topicsById = new Map(payload.topics.map((topic) => [topic.id, topic]));
  const chips: EdgeChip[] = [];
  for (const edge of payload.topicEdges) {
    if (edge.fromTopicId !== topicId && edge.toTopicId !== topicId) continue;
    const otherId = edge.fromTopicId === topicId ? edge.toTopicId : edge.fromTopicId;
    const other = topicsById.get(otherId);
    if (!other) continue;
    const label: EdgeChip["label"] =
      edge.relation === "related" ? "related" : edge.toTopicId === topicId ? "requires" : "required by";
    chips.push({
      key: `${edge.fromTopicId}-${edge.toTopicId}-${edge.relation}`,
      label,
      otherTopicId: otherId,
      otherTopicName: other.name,
    });
  }
  return chips;
}

interface TopicDetailProps {
  payload: GraphPayload;
  topicId: number;
  courseId: number;
  mockLlm: boolean;
  runActive: boolean;
  onSelectTopic: (topicId: number) => void;
  onSelectMaterial: (materialId: number) => void;
}

function TopicDetail({ payload, topicId, courseId, mockLlm, runActive, onSelectTopic, onSelectMaterial }: TopicDetailProps) {
  const topic = payload.topics.find((t) => t.id === topicId);
  const edgeChips = useMemo(() => edgeChipsForTopic(payload, topicId), [payload, topicId]);
  const attachments = useMemo(
    () => payload.attachments.filter((attachment) => attachment.topicId === topicId),
    [payload, topicId],
  );
  const materialsById = useMemo(
    () => new Map(payload.materials.map((material) => [material.id, material])),
    [payload.materials],
  );

  if (!topic) {
    return (
      <p className="text-sm text-neutral-500 dark:text-neutral-400">
        This topic is no longer in the current taxonomy.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-base font-semibold text-neutral-900 dark:text-neutral-100">{topic.name}</h2>
        {topic.description && (
          <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-300">{topic.description}</p>
        )}
      </div>

      {edgeChips.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {edgeChips.map((chip) => (
            <button
              key={chip.key}
              type="button"
              onClick={() => onSelectTopic(chip.otherTopicId)}
              className="rounded-full bg-neutral-100 px-2 py-0.5 text-xs text-neutral-700 transition hover:bg-neutral-200 dark:bg-neutral-800 dark:text-neutral-300 dark:hover:bg-neutral-700"
            >
              {chip.label}: {chip.otherTopicName}
            </button>
          ))}
        </div>
      )}

      <div>
        <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
          Materials
        </h3>
        {attachments.length === 0 ? (
          <p className="text-sm text-neutral-500 dark:text-neutral-400">No materials attached.</p>
        ) : (
          <ul className="space-y-1">
            {attachments.map((attachment) => {
              const material = materialsById.get(attachment.materialId);
              if (!material) return null;
              const Icon = KIND_ICON[material.kind];
              return (
                <li key={material.id}>
                  <button
                    type="button"
                    onClick={() => onSelectMaterial(material.id)}
                    title={attachment.rationale ?? undefined}
                    className="flex w-full min-w-0 items-center gap-2 rounded-md px-1.5 py-1 text-left text-sm text-neutral-700 transition hover:bg-neutral-100 dark:text-neutral-200 dark:hover:bg-neutral-900"
                  >
                    <Icon size={14} className="shrink-0 text-neutral-400 dark:text-neutral-500" aria-hidden />
                    <span className="min-w-0 flex-1 truncate">{material.title}</span>
                    <span className="shrink-0 text-xs tabular-nums text-neutral-400 dark:text-neutral-500">
                      {formatConfidence(attachment)}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {/* M3.5c: neither synthetic bucket is a real topic to search the web
       * for -- Unsorted already carried this rule nowhere (no guard
       * existed at all before this task), so it's added here for BOTH
       * Unsorted and the Logistics & admin bucket together. */}
      {topicId !== UNSORTED_TOPIC_ID && topicId !== ADMIN_TOPIC_ID && (
        <TopicSupplementary topicId={topicId} courseId={courseId} mockLlm={mockLlm} runActive={runActive} />
      )}
    </div>
  );
}

function formatConfidence(attachment: GraphAttachment): string {
  return attachment.confidence === null ? "—" : `${Math.round(attachment.confidence * 100)}%`;
}

// ---------------------------------------------------------------------------
// Material selected
// ---------------------------------------------------------------------------

interface MaterialDetailProps {
  payload: GraphPayload;
  materialId: number;
  onSelectTopic: (topicId: number) => void;
  /** In-app selection jump (M3.5c) -- backs both the transcript's "from
   * recording" link and the source material's "View transcript" link.
   * Wired to `setSelection` (idempotent, non-toggling) rather than
   * `selectMaterial`, matching how RecordingsDrawer's own "Transcript
   * ready" jump behaves: a jump should always land on its target, never
   * toggle it off. */
  onJumpToMaterial: (materialId: number) => void;
}

function MaterialDetail({ payload, materialId, onSelectTopic, onJumpToMaterial }: MaterialDetailProps) {
  const materialQuery = useQuery({
    queryKey: ["material", materialId],
    queryFn: () => getMaterial(materialId),
  });

  const topicsById = useMemo(() => new Map(payload.topics.map((t) => [t.id, t])), [payload.topics]);
  const topicChips = useMemo(
    () =>
      payload.attachments
        .filter((attachment) => attachment.materialId === materialId)
        .map((attachment) => ({ attachment, topic: topicsById.get(attachment.topicId) }))
        .filter(
          (entry): entry is { attachment: GraphAttachment; topic: GraphTopic } => entry.topic !== undefined,
        ),
    [payload.attachments, materialId, topicsById],
  );

  if (materialQuery.isLoading) {
    return <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading material…</p>;
  }
  if (materialQuery.isError || !materialQuery.data) {
    return <p className="text-sm text-red-600 dark:text-red-400">Couldn't load this material.</p>;
  }

  const material = materialQuery.data;
  const openInBrightspace = material.sourceUrl?.startsWith("http") ? material.sourceUrl : null;
  const recording = material.recording;
  // The two `recording` shapes (source vs. transcript, api/materials.py's
  // `_recording_info`) share only `url`/`status` -- narrow on which id
  // field is present rather than trusting `material.kind`, since that's a
  // separate classification the recording linkage doesn't depend on.
  const transcriptMaterialId =
    recording && "transcriptMaterialId" in recording ? (recording.transcriptMaterialId ?? null) : null;
  const sourceMaterialId =
    recording && "sourceMaterialId" in recording ? (recording.sourceMaterialId ?? null) : null;

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-base font-semibold text-neutral-900 dark:text-neutral-100">{material.title}</h2>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <span className="rounded-full bg-neutral-100 px-2 py-0.5 text-xs text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
            {material.kind}
          </span>
          <span className="rounded-full bg-neutral-100 px-2 py-0.5 text-xs text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
            {material.status}
          </span>
        </div>
      </div>

      {topicChips.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {topicChips.map(({ attachment, topic }) => (
            <button
              key={topic.id}
              type="button"
              onClick={() => onSelectTopic(topic.id)}
              className="rounded-full bg-blue-50 px-2 py-0.5 text-xs text-blue-700 transition hover:bg-blue-100 dark:bg-blue-950 dark:text-blue-300 dark:hover:bg-blue-900"
            >
              {topic.name} · {formatConfidence(attachment)}
            </button>
          ))}
        </div>
      )}

      <div>
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
          Summary
        </h3>
        <p className="text-sm text-neutral-700 dark:text-neutral-300">
          {material.summary ?? "No summary yet."}
        </p>
      </div>

      {material.keyTerms.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {material.keyTerms.map((term) => (
            <span
              key={term}
              className="rounded-full bg-neutral-100 px-2 py-0.5 text-xs text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300"
            >
              {term}
            </span>
          ))}
        </div>
      )}

      {recording && (
        <div className="space-y-1">
          {/* Review fix: `recording.url` is backend-supplied and rendered
           * straight into `href` -- gated on `isSafeHttpUrl` so a
           * non-http(s) scheme never reaches an anchor (same reasoning as
           * RecordingsDrawer's Open link). The jump links below don't touch
           * `href` at all (in-app selection only), so they're unaffected
           * and still render. */}
          {isSafeHttpUrl(recording.url) && (
            <a
              href={recording.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-block text-sm text-blue-600 underline underline-offset-2 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
            >
              Open recording ↗
            </a>
          )}
          {sourceMaterialId !== null && (
            <button
              type="button"
              onClick={() => onJumpToMaterial(sourceMaterialId)}
              className="block text-xs text-neutral-500 underline underline-offset-2 hover:text-neutral-700 dark:text-neutral-400 dark:hover:text-neutral-200"
            >
              from recording
            </button>
          )}
          {transcriptMaterialId !== null && (
            <button
              type="button"
              onClick={() => onJumpToMaterial(transcriptMaterialId)}
              className="block text-xs text-neutral-500 underline underline-offset-2 hover:text-neutral-700 dark:text-neutral-400 dark:hover:text-neutral-200"
            >
              View transcript
            </button>
          )}
        </div>
      )}

      {openInBrightspace && (
        <a
          href={openInBrightspace}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-block text-sm text-blue-600 underline underline-offset-2 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
        >
          Open in Brightspace ↗
        </a>
      )}

      <MaterialReader material={material} />
    </div>
  );
}
