import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import {
  dryRun,
  getCourse,
  getGraph,
  getSettings,
  openEvents,
  pipelineStatus,
  runPipeline,
} from "../api/client";
import type { BsaEvent, DryRunResponse, TaxonomyApplyResponse } from "../api/types";
import { GraphView } from "../graph/GraphView";
import { DetailPanel } from "../panels/DetailPanel";
import { OutlinePanel } from "../panels/OutlinePanel";
import { TaxonomyEditor } from "../panels/TaxonomyEditor";
import { useUiStore } from "../state/uiStore";

/** Every status an enrichment run can END on (runner.py's
 * `_execute_enrichment` / `_EnrichmentRunHooks.on_finish`). A run that
 * aborted on the cost cap or failed still wrote rows for whatever it got
 * through, so the read models must be refreshed for all of these -- not just
 * the happy one. */
const ENRICHMENT_TERMINAL_STATUSES = new Set(["complete", "aborted", "failed"]);

export function CourseWorkspacePage() {
  const { courseId: courseIdParam } = useParams<{ courseId: string }>();
  const courseId = Number(courseIdParam);
  const courseIdValid = Number.isFinite(courseId);

  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();

  const selection = useUiStore((state) => state.selection);
  const setSelection = useUiStore((state) => state.setSelection);

  const [confirmDryRun, setConfirmDryRun] = useState(false);
  const [taxonomyEditorOpen, setTaxonomyEditorOpen] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  const courseQuery = useQuery({
    queryKey: ["course", courseId],
    queryFn: () => getCourse(courseId),
    enabled: courseIdValid,
  });
  const graphQuery = useQuery({
    queryKey: ["graph", courseId],
    queryFn: () => getGraph(courseId),
    enabled: courseIdValid,
  });
  const statusQuery = useQuery({
    queryKey: ["pipeline-status", courseId],
    queryFn: () => pipelineStatus(courseId),
    enabled: courseIdValid,
  });
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: getSettings });

  // Run Pipeline is a two-step flow: dry-run first (an estimate, no spend),
  // then -- once the user confirms the modal it opens -- the real run.
  const dryRunMutation = useMutation({
    mutationFn: () => dryRun(courseId),
    onSuccess: () => setConfirmDryRun(true),
  });
  const runMutation = useMutation({
    mutationFn: () => runPipeline(courseId),
    onSuccess: () => {
      setConfirmDryRun(false);
      queryClient.invalidateQueries({ queryKey: ["pipeline-status", courseId] });
    },
  });

  // Load -> store: hydrate the selection from the URL once per course
  // (covers a fresh page load / a shared deep link). Deliberately depends
  // only on courseId, not on searchParams, so it doesn't fight with the
  // store -> URL effect below. Uses `setSelection` (an unconditional set),
  // NOT the toggling `selectTopic`/`selectMaterial` -- those deselect on a
  // second call with the same id, and React StrictMode's dev-mode
  // double-invoke of effects would otherwise select-then-immediately-
  // deselect on every mount, silently dropping every deep link.
  useEffect(() => {
    if (!courseIdValid) return;
    const materialParam = searchParams.get("material");
    const topicParam = searchParams.get("topic");
    const materialId = materialParam !== null ? Number(materialParam) : NaN;
    const topicId = topicParam !== null ? Number(topicParam) : NaN;
    if (Number.isFinite(materialId)) {
      setSelection({ type: "material", id: materialId });
    } else if (Number.isFinite(topicId)) {
      setSelection({ type: "topic", id: topicId });
    } else {
      setSelection(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId, courseIdValid]);

  // Store -> URL: mirror the current selection out via replaceState (no
  // history spam) whenever it changes.
  useEffect(() => {
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        next.delete("topic");
        next.delete("material");
        if (selection?.type === "topic") next.set("topic", String(selection.id));
        if (selection?.type === "material") next.set("material", String(selection.id));
        return next;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selection]);

  // SSE: any pipeline event for this course refreshes its status; a
  // finished run also refreshes the graph (and course, for material
  // counts) since that's when the data actually changed. An `enrichment`
  // event (M3.3) refreshes the same pipeline-status query -- runner.py's
  // `start()`/`start_enrichment()` share one `_active` guard per course, so
  // `pipelineStatus`'s `active` already doubles as "is any course run
  // (pipeline OR enrichment) active", which is what disables the
  // Find/Refresh button in TopicSupplementary -- plus the affected
  // topic-enrichment read model(s): just this topic's when the event
  // carries one, every mounted topic's on a finished course-wide batch
  // (no topicId). "Finished" means ANY terminal status, not just
  // 'complete': a batch that aborted on the cost cap or failed partway
  // still wrote rows for the topics it got through, and treating those
  // runs as non-events left the panels showing stale data with nothing to
  // trigger a refetch. Enrichment never changes the graph payload (it only
  // writes enrichment_resources), so no graph/course refetch here.
  useEffect(() => {
    if (!courseIdValid) return;
    const source = openEvents((event: BsaEvent) => {
      if (event.type === "pipeline" && event.courseId === courseId) {
        queryClient.invalidateQueries({ queryKey: ["pipeline-status", courseId] });
        if (event.status === "run-finished") {
          queryClient.invalidateQueries({ queryKey: ["graph", courseId] });
          queryClient.invalidateQueries({ queryKey: ["course", courseId] });
        }
      }
      if (event.type === "enrichment" && event.courseId === courseId) {
        queryClient.invalidateQueries({ queryKey: ["pipeline-status", courseId] });
        if (event.topicId !== undefined) {
          queryClient.invalidateQueries({ queryKey: ["topic-enrichment", event.topicId] });
        } else if (ENRICHMENT_TERMINAL_STATUSES.has(event.status)) {
          queryClient.invalidateQueries({ queryKey: ["topic-enrichment"] });
        }
      }
    });
    return () => source.close();
  }, [courseId, courseIdValid, queryClient]);

  // Auto-dismiss the taxonomy-save toast after a few seconds.
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(timer);
  }, [toast]);

  // Task 12: a patch is applied immediately (its version doesn't change),
  // so the graph is stale until refetched here; a structural save started a
  // pipeline run (`reclassify: true`) whose progress/completion the
  // existing SSE effect above already handles -- refetching again here
  // would just race it.
  function handleTaxonomySaved(result: TaxonomyApplyResponse) {
    setTaxonomyEditorOpen(false);
    if (result.reclassify) {
      setToast("Re-classifying…");
    } else {
      setToast("Taxonomy updated.");
      queryClient.invalidateQueries({ queryKey: ["graph", courseId] });
    }
  }

  const course = courseQuery.data;
  const active = statusQuery.data?.active ?? false;
  const runPipelineDisabled =
    !courseIdValid || active || dryRunMutation.isPending || runMutation.isPending;

  return (
    <div className="flex h-screen flex-col bg-neutral-50 dark:bg-neutral-950">
      <header className="flex items-center gap-3 border-b border-neutral-200 px-4 py-2 dark:border-neutral-800">
        <Link
          to="/"
          className="text-sm text-neutral-500 hover:text-neutral-700 dark:text-neutral-400 dark:hover:text-neutral-200"
        >
          ← Courses
        </Link>
        <h1 className="truncate font-medium text-neutral-900 dark:text-neutral-100">
          {course?.name ?? (courseIdValid ? `Course ${courseId}` : "Unknown course")}
        </h1>
        {course && (
          <span className="shrink-0 rounded-full bg-neutral-100 px-2 py-0.5 text-xs text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
            taxonomy v{course.taxonomyVersion}
          </span>
        )}
        <div className="flex-1" />
        {dryRunMutation.isError && (
          <span className="text-xs text-red-600 dark:text-red-400">Couldn't estimate cost.</span>
        )}
        <Link
          to="/settings"
          className="text-sm text-neutral-500 hover:text-neutral-700 dark:text-neutral-400 dark:hover:text-neutral-200"
        >
          Settings
        </Link>
        <button
          type="button"
          disabled={!graphQuery.data}
          onClick={() => setTaxonomyEditorOpen(true)}
          className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium text-neutral-700 transition hover:bg-neutral-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-neutral-700 dark:text-neutral-200 dark:hover:bg-neutral-800"
        >
          Edit topics
        </button>
        <button
          type="button"
          disabled={runPipelineDisabled}
          onClick={() => dryRunMutation.mutate()}
          className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {active ? "Running…" : dryRunMutation.isPending ? "Estimating…" : "Run pipeline"}
        </button>
      </header>

      <div className="flex flex-1 overflow-hidden">
        <aside className="w-[260px] shrink-0 overflow-y-auto border-r border-neutral-200 p-3 dark:border-neutral-800">
          {graphQuery.isLoading && (
            <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading outline…</p>
          )}
          {graphQuery.isError && (
            <p className="text-sm text-red-600 dark:text-red-400">Couldn't load the outline.</p>
          )}
          {graphQuery.data && <OutlinePanel payload={graphQuery.data} />}
        </aside>

        <main className="min-w-0 flex-1 overflow-hidden">
          {graphQuery.isLoading && (
            <p className="p-4 text-sm text-neutral-500 dark:text-neutral-400">Loading graph…</p>
          )}
          {graphQuery.isError && (
            <p className="p-4 text-sm text-red-600 dark:text-red-400">Couldn't load the graph.</p>
          )}
          {graphQuery.data && <GraphView payload={graphQuery.data} />}
        </main>

        <aside className="w-[360px] shrink-0 overflow-y-auto border-l border-neutral-200 p-3 dark:border-neutral-800">
          {graphQuery.isLoading && (
            <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading details…</p>
          )}
          {graphQuery.isError && (
            <p className="text-sm text-red-600 dark:text-red-400">Couldn't load details.</p>
          )}
          {graphQuery.data && (
            <DetailPanel
              payload={graphQuery.data}
              courseId={courseId}
              mockLlm={settingsQuery.data?.mockLlm ?? false}
              runActive={active}
            />
          )}
        </aside>
      </div>

      {confirmDryRun && dryRunMutation.data && (
        <DryRunConfirmDialog
          dryRun={dryRunMutation.data}
          mockLlm={settingsQuery.data?.mockLlm ?? false}
          confirming={runMutation.isPending}
          onCancel={() => setConfirmDryRun(false)}
          onConfirm={() => runMutation.mutate()}
        />
      )}

      {taxonomyEditorOpen && graphQuery.data && (
        <TaxonomyEditor
          courseId={courseId}
          payload={graphQuery.data}
          pipelineActive={active}
          onClose={() => setTaxonomyEditorOpen(false)}
          onSaved={handleTaxonomySaved}
        />
      )}

      {toast && (
        <div className="fixed bottom-4 right-4 z-50 rounded-md bg-neutral-900 px-3 py-2 text-sm text-white shadow-lg dark:bg-neutral-100 dark:text-neutral-900">
          {toast}
        </div>
      )}
    </div>
  );
}

const STAGE_LABELS: Record<string, string> = {
  summarize: "Summarize",
  taxonomy: "Build taxonomy",
  classify: "Classify",
};

function formatUsd(amount: number): string {
  return `$${amount.toFixed(4)}`;
}

interface DryRunConfirmDialogProps {
  dryRun: DryRunResponse;
  mockLlm: boolean;
  confirming: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

function DryRunConfirmDialog({ dryRun, mockLlm, confirming, onCancel, onConfirm }: DryRunConfirmDialogProps) {
  const stages = Object.entries(dryRun.byStage);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="dry-run-confirm-title"
        className="w-full max-w-sm rounded-lg bg-white p-5 shadow-xl dark:bg-neutral-900"
      >
        <h2 id="dry-run-confirm-title" className="text-base font-semibold text-neutral-900 dark:text-neutral-100">
          Run the pipeline?
        </h2>

        <ul className="mt-3 space-y-1 text-sm">
          {stages.map(([stage, stats]) => (
            <li key={stage} className="flex items-center justify-between text-neutral-700 dark:text-neutral-300">
              <span>
                {STAGE_LABELS[stage] ?? stage} ({stats.calls} {stats.calls === 1 ? "call" : "calls"})
              </span>
              <span className="tabular-nums text-neutral-500 dark:text-neutral-400">
                {formatUsd(stats.estCostUsd)}
              </span>
            </li>
          ))}
        </ul>

        <div className="mt-2 flex items-center justify-between border-t border-neutral-200 pt-2 text-sm font-medium text-neutral-900 dark:border-neutral-800 dark:text-neutral-100">
          <span>Total estimated cost</span>
          <span className="tabular-nums">{formatUsd(dryRun.totalEstCostUsd)}</span>
        </div>

        {mockLlm && (
          <p className="mt-2 rounded-md bg-amber-50 px-2 py-1.5 text-xs text-amber-700 dark:bg-amber-950 dark:text-amber-300">
            Mock mode — free. No real LLM calls will be made or billed.
          </p>
        )}

        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium text-neutral-700 transition hover:bg-neutral-100 dark:border-neutral-700 dark:text-neutral-200 dark:hover:bg-neutral-800"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={confirming}
            onClick={onConfirm}
            className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {confirming ? "Starting…" : "Confirm"}
          </button>
        </div>
      </div>
    </div>
  );
}
