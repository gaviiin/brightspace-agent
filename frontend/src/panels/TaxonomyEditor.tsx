import { useMutation } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { putTaxonomy } from "../api/client";
import type { GraphPayload, TaxonomyApplyResponse, TopicEdgeRelation } from "../api/types";
import {
  addEdge,
  addTopic,
  deleteTopic,
  type Draft,
  estimateReclassifyCount,
  initDraft,
  isStructural,
  mergeSelectedInto,
  removeEdge,
  rename,
  setDescription,
  toggleMergeSelect,
  toRequest,
} from "./taxonomyDraft";

interface TaxonomyEditorProps {
  courseId: number;
  payload: GraphPayload;
  /** Mirrors the workspace's "Run pipeline" button's own active-run guard
   * (see CourseWorkspacePage's `statusQuery`): a structural save starts a
   * pipeline run, and the backend's `PipelineRunner` only ever allows one
   * per course, so a structural Save is disabled while one is already
   * active -- matching the server-side check-before-write guard in
   * taxonomy_apply.py, rather than letting the student hit a 409. A patch
   * never touches the runner, so it stays enabled regardless. */
  pipelineActive: boolean;
  onClose: () => void;
  /** Called once the PUT succeeds. The caller decides what happens next
   * (patch -> refetch the graph immediately; structural -> a toast, with
   * the existing SSE/refetch machinery picking up the run) -- this
   * component's job ends at a successful save. */
  onSaved: (result: TaxonomyApplyResponse) => void;
}

const RELATIONS: TopicEdgeRelation[] = ["prerequisite", "related"];

/** Right-side drawer over the workspace for editing a course's taxonomy
 * (Task 12): rename/re-describe, add, delete, and merge topics, and edit
 * edges. All the actual decision logic (draft mutation, patch-vs-structural,
 * index resolution) lives in taxonomyDraft.ts -- this component is the
 * form around it. Draft state is initialized once from `payload` (a copy,
 * per taxonomyDraft's own contract) and never writes back into the
 * react-query cache directly; only a successful PUT does that, via
 * `onSaved`. */
export function TaxonomyEditor({ courseId, payload, pipelineActive, onClose, onSaved }: TaxonomyEditorProps) {
  const [draft, setDraft] = useState<Draft>(() => initDraft(payload));
  const [newEdgeFrom, setNewEdgeFrom] = useState("");
  const [newEdgeTo, setNewEdgeTo] = useState("");
  const [newEdgeRelation, setNewEdgeRelation] = useState<TopicEdgeRelation>("related");

  const structural = useMemo(() => isStructural(draft), [draft]);
  const affectedCount = useMemo(
    () => (structural ? estimateReclassifyCount(draft, payload) : 0),
    [structural, draft, payload],
  );
  const blockedByActiveRun = structural && pipelineActive;

  const saveMutation = useMutation({
    mutationFn: () => putTaxonomy(courseId, toRequest(draft)),
    onSuccess: (result) => onSaved(result),
  });

  function topicName(key: string): string {
    return draft.topics.find((topic) => topic.key === key)?.name || "(untitled)";
  }

  function handleAddEdge() {
    if (!newEdgeFrom || !newEdgeTo || newEdgeFrom === newEdgeTo) return;
    setDraft((current) => addEdge(current, newEdgeFrom, newEdgeTo, newEdgeRelation));
    setNewEdgeFrom("");
    setNewEdgeTo("");
  }

  function handleDelete(key: string, materialCount: number, name: string) {
    if (materialCount > 0) {
      const label = `${materialCount} material${materialCount === 1 ? "" : "s"}`;
      const confirmed = window.confirm(`${label} will be re-classified. Delete "${name || "this topic"}"?`);
      if (!confirmed) return;
    }
    setDraft((current) => deleteTopic(current, key));
  }

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/40" onClick={onClose}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Edit taxonomy"
        onClick={(event) => event.stopPropagation()}
        className="flex h-full w-full max-w-[480px] flex-col bg-white shadow-xl dark:bg-neutral-900"
      >
        <header className="flex shrink-0 items-center justify-between border-b border-neutral-200 px-4 py-3 dark:border-neutral-800">
          <h2 className="text-base font-semibold text-neutral-900 dark:text-neutral-100">Edit topics</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="text-sm text-neutral-500 hover:text-neutral-700 dark:text-neutral-400 dark:hover:text-neutral-200"
          >
            Close
          </button>
        </header>

        <div className="flex-1 space-y-4 overflow-y-auto p-4">
          {draft.mergeSelection.size >= 2 && (
            <div className="flex flex-wrap items-center gap-2 rounded-md bg-blue-50 px-3 py-2 text-sm dark:bg-blue-950">
              <span className="text-blue-800 dark:text-blue-200">{draft.mergeSelection.size} selected —</span>
              {[...draft.mergeSelection].map((key) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => setDraft((current) => mergeSelectedInto(current, key))}
                  className="rounded-full bg-blue-600 px-2 py-0.5 text-xs font-medium text-white transition hover:bg-blue-700"
                >
                  Merge into &ldquo;{topicName(key)}&rdquo;
                </button>
              ))}
            </div>
          )}

          <ul className="space-y-3">
            {draft.topics.map((topic) => (
              <li key={topic.key} className="rounded-md border border-neutral-200 p-3 dark:border-neutral-800">
                <div className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    checked={draft.mergeSelection.has(topic.key)}
                    onChange={() => setDraft((current) => toggleMergeSelect(current, topic.key))}
                    aria-label={`Select "${topic.name || "untitled topic"}" to merge`}
                    className="mt-2"
                  />
                  <div className="min-w-0 flex-1 space-y-1.5">
                    <input
                      type="text"
                      value={topic.name}
                      onChange={(event) => setDraft((current) => rename(current, topic.key, event.target.value))}
                      placeholder="Topic name"
                      aria-label="Topic name"
                      className="w-full rounded-md border border-neutral-300 px-2 py-1 text-sm font-medium text-neutral-900 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-100"
                    />
                    <textarea
                      value={topic.description}
                      onChange={(event) =>
                        setDraft((current) => setDescription(current, topic.key, event.target.value))
                      }
                      placeholder="Description"
                      aria-label="Topic description"
                      rows={2}
                      className="w-full rounded-md border border-neutral-300 px-2 py-1 text-xs text-neutral-700 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-300"
                    />
                    <div className="flex items-center justify-between text-xs text-neutral-500 dark:text-neutral-400">
                      <span>
                        {topic.materialCount} material{topic.materialCount === 1 ? "" : "s"}
                      </span>
                      <button
                        type="button"
                        onClick={() => handleDelete(topic.key, topic.materialCount, topic.name)}
                        className="font-medium text-red-600 hover:underline dark:text-red-400"
                      >
                        Delete
                      </button>
                    </div>
                  </div>
                </div>
              </li>
            ))}
          </ul>

          <button
            type="button"
            onClick={() => setDraft((current) => addTopic(current))}
            className="w-full rounded-md border border-dashed border-neutral-300 py-1.5 text-sm text-neutral-600 transition hover:bg-neutral-50 dark:border-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-800"
          >
            + Add topic
          </button>

          <div>
            <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
              Edges
            </h3>
            {draft.edges.length > 0 && (
              <ul className="mb-2 space-y-1">
                {draft.edges.map((edge) => (
                  <li
                    key={edge.key}
                    className="flex items-center justify-between gap-2 rounded-md bg-neutral-50 px-2 py-1 text-xs text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300"
                  >
                    <span className="min-w-0 truncate">
                      {topicName(edge.fromKey)} {edge.relation === "prerequisite" ? "requires" : "↔ related to"}{" "}
                      {topicName(edge.toKey)}
                    </span>
                    <button
                      type="button"
                      onClick={() => setDraft((current) => removeEdge(current, edge.key))}
                      className="shrink-0 font-medium text-red-600 hover:underline dark:text-red-400"
                    >
                      Remove
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <div className="flex items-center gap-1">
              <select
                value={newEdgeFrom}
                onChange={(event) => setNewEdgeFrom(event.target.value)}
                aria-label="Edge source topic"
                className="min-w-0 flex-1 rounded-md border border-neutral-300 px-1.5 py-1 text-xs dark:border-neutral-700 dark:bg-neutral-800"
              >
                <option value="">From…</option>
                {draft.topics.map((topic) => (
                  <option key={topic.key} value={topic.key}>
                    {topic.name || "(untitled)"}
                  </option>
                ))}
              </select>
              <select
                value={newEdgeRelation}
                onChange={(event) => setNewEdgeRelation(event.target.value as TopicEdgeRelation)}
                aria-label="Edge relation"
                className="shrink-0 rounded-md border border-neutral-300 px-1.5 py-1 text-xs dark:border-neutral-700 dark:bg-neutral-800"
              >
                {RELATIONS.map((relation) => (
                  <option key={relation} value={relation}>
                    {relation}
                  </option>
                ))}
              </select>
              <select
                value={newEdgeTo}
                onChange={(event) => setNewEdgeTo(event.target.value)}
                aria-label="Edge target topic"
                className="min-w-0 flex-1 rounded-md border border-neutral-300 px-1.5 py-1 text-xs dark:border-neutral-700 dark:bg-neutral-800"
              >
                <option value="">To…</option>
                {draft.topics.map((topic) => (
                  <option key={topic.key} value={topic.key}>
                    {topic.name || "(untitled)"}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={handleAddEdge}
                disabled={!newEdgeFrom || !newEdgeTo || newEdgeFrom === newEdgeTo}
                className="shrink-0 rounded-md bg-neutral-200 px-2 py-1 text-xs font-medium text-neutral-700 transition hover:bg-neutral-300 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-neutral-700 dark:text-neutral-200 dark:hover:bg-neutral-600"
              >
                Add
              </button>
            </div>
          </div>

          {saveMutation.isError && (
            <p className="text-sm text-red-600 dark:text-red-400">
              Couldn't save: {(saveMutation.error as Error).message}
            </p>
          )}
          {blockedByActiveRun && !saveMutation.isError && (
            <p className="text-sm text-amber-600 dark:text-amber-400">
              A pipeline run is already active for this course — wait for it to finish before saving
              structural changes.
            </p>
          )}
        </div>

        <footer className="flex shrink-0 items-center justify-end gap-2 border-t border-neutral-200 px-4 py-3 dark:border-neutral-800">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium text-neutral-700 transition hover:bg-neutral-100 dark:border-neutral-700 dark:text-neutral-200 dark:hover:bg-neutral-800"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={saveMutation.isPending || draft.topics.length === 0 || blockedByActiveRun}
            onClick={() => saveMutation.mutate()}
            className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {saveMutation.isPending
              ? "Saving…"
              : structural
                ? `Save & re-classify (${affectedCount})`
                : "Save"}
          </button>
        </footer>
      </aside>
    </div>
  );
}
