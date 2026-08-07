import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BookOpen,
  ClipboardList,
  FileText,
  Link as LinkIcon,
  MonitorPlay,
  Presentation,
  Video,
} from "lucide-react";
import { useState } from "react";
import type { ComponentType } from "react";

import { enrichDryRun, enrichTopic, getTopicEnrichment, setEnrichmentStatus } from "../api/client";
import type { EnrichDryRunResponse, EnrichmentResource, EnrichmentStatus } from "../api/types";

/** `resourceType` -> icon (M3.2's finder emits a free-form string, see
 * agents/schemas.py's `Candidate.resource_type` docstring: "e.g. 'video',
 * 'article', 'notes', 'problem_set', 'interactive'"). Falls back to a plain
 * link icon for anything else. */
const RESOURCE_TYPE_ICON: Record<string, ComponentType<{ size?: number; className?: string }>> = {
  video: Video,
  article: FileText,
  notes: BookOpen,
  problem_set: ClipboardList,
  interactive: MonitorPlay,
  slides: Presentation,
};

/** `intent` -> short human label (agents/schemas.py's `IntentType`, a
 * closed 6-value literal). Any future value still renders (underscores ->
 * spaces) rather than disappearing. */
const INTENT_LABEL: Record<string, string> = {
  alternative_explanation: "alternative explanation",
  video_lecture: "video lecture",
  worked_examples: "worked examples",
  interactive_visualization: "interactive viz",
  university_notes: "university notes",
  past_exams: "past exams",
};

function intentLabel(intent: string | null): string | null {
  if (!intent) return null;
  return INTENT_LABEL[intent] ?? intent.replace(/_/g, " ");
}

/** A rough "is this a weaker match" signal for the low-confidence dot
 * (nice-to-have per the brief): the judge's rubric scores
 * (agents/schemas.py's `JudgedResource.scores`) average below this reads
 * as worth a second look, not necessarily wrong. */
const LOW_CONFIDENCE_THRESHOLD = 0.5;

function isLowConfidence(scores: Record<string, number>): boolean {
  const values = Object.values(scores);
  if (values.length === 0) return false;
  return values.reduce((sum, v) => sum + v, 0) / values.length < LOW_CONFIDENCE_THRESHOLD;
}

interface TopicSupplementaryProps {
  topicId: number;
  /** Needed for the course-level dry-run estimate (api/enrichment.py has no
   * per-topic dry-run endpoint) even though the confirmed action itself
   * (`enrichTopic`) is scoped to just this topic. */
  courseId: number;
  mockLlm: boolean;
  /** Mirrors `pipelineActive` (CourseWorkspacePage's `active`, which
   * already reflects an enrichment run too -- runner.py's `start()` and
   * `start_enrichment()` share one `_active` guard per course). Disables
   * the Find/Refresh button while any course run is in flight, matching
   * the backend's 409 guard instead of letting the student hit it. */
  runActive: boolean;
}

/** Task M3.3: the topic detail panel's "Supplementary" section -- M3's
 * AI-found web resources for one topic, with keep/dismiss feedback and a
 * per-topic Find/Refresh trigger. Read model is `["topic-enrichment",
 * topicId]` (invalidated by CourseWorkspacePage's SSE handler on the
 * matching `enrichment` event); the Find/Refresh button reuses the exact
 * dry-run -> confirm shape as CourseWorkspacePage's "Run pipeline" button,
 * just against the enrichment dry-run's own (differently-shaped) estimate. */
export function TopicSupplementary({ topicId, courseId, mockLlm, runActive }: TopicSupplementaryProps) {
  const queryClient = useQueryClient();
  const [showDismissed, setShowDismissed] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const enrichmentQuery = useQuery({
    queryKey: ["topic-enrichment", topicId],
    queryFn: () => getTopicEnrichment(topicId),
  });

  const dryRunMutation = useMutation({
    mutationFn: () => enrichDryRun(courseId),
    onSuccess: () => setConfirmOpen(true),
  });

  const enrichMutation = useMutation({
    mutationFn: () => enrichTopic(topicId),
    onSuccess: () => {
      setConfirmOpen(false);
      // The run itself takes a while (LLM + web calls); this just makes the
      // Find/Refresh button disable promptly (mirroring runPipelineDisabled
      // in CourseWorkspacePage) instead of waiting on the SSE round-trip.
      queryClient.invalidateQueries({ queryKey: ["pipeline-status", courseId] });
    },
  });

  const statusMutation = useMutation({
    mutationFn: ({ id, status }: { id: number; status: EnrichmentStatus }) => setEnrichmentStatus(id, status),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["topic-enrichment", topicId] }),
  });

  const findDisabled = runActive || dryRunMutation.isPending || enrichMutation.isPending;

  if (enrichmentQuery.isLoading) {
    return (
      <div>
        <SectionHeading />
        <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading supplementary materials…</p>
      </div>
    );
  }

  if (enrichmentQuery.isError || !enrichmentQuery.data) {
    return (
      <div>
        <SectionHeading />
        <p className="text-sm text-red-600 dark:text-red-400">Couldn't load supplementary materials.</p>
      </div>
    );
  }

  const { resources, meta } = enrichmentQuery.data;
  const hasAny = resources.length > 0;
  const visibleResources = showDismissed ? resources : resources.filter((r) => r.status !== "dismissed");
  const findLabel = hasAny ? "Refresh supplementary materials" : "Find supplementary materials";

  return (
    <div>
      <SectionHeading meta={hasAny ? meta : undefined} />

      {!hasAny && (
        <p className="mb-2 text-sm text-neutral-500 dark:text-neutral-400">
          No supplementary materials yet. BrightSpace Agent can search the web for resources that fit this topic.
        </p>
      )}

      {hasAny && (
        <ul className="mb-2 space-y-1.5">
          {visibleResources.map((resource) => (
            <ResourceRow
              key={resource.id}
              resource={resource}
              onSetStatus={(status) => statusMutation.mutate({ id: resource.id, status })}
            />
          ))}
        </ul>
      )}

      {meta.dismissed > 0 && (
        <button
          type="button"
          onClick={() => setShowDismissed((current) => !current)}
          className="mb-2 text-xs text-neutral-500 underline underline-offset-2 hover:text-neutral-700 dark:text-neutral-400 dark:hover:text-neutral-200"
        >
          {showDismissed ? "Hide dismissed" : `Show dismissed (${meta.dismissed})`}
        </button>
      )}

      {dryRunMutation.isError && (
        <p className="mb-1 text-xs text-red-600 dark:text-red-400">Couldn't estimate cost.</p>
      )}

      <button
        type="button"
        disabled={findDisabled}
        onClick={() => dryRunMutation.mutate()}
        className="rounded-md border border-neutral-300 px-2.5 py-1 text-xs font-medium text-neutral-700 transition hover:bg-neutral-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-neutral-700 dark:text-neutral-200 dark:hover:bg-neutral-800"
      >
        {runActive ? "Running…" : dryRunMutation.isPending ? "Estimating…" : findLabel}
      </button>

      {confirmOpen && dryRunMutation.data && (
        <EnrichDryRunConfirmDialog
          dryRun={dryRunMutation.data}
          mockLlm={mockLlm}
          confirming={enrichMutation.isPending}
          onCancel={() => setConfirmOpen(false)}
          onConfirm={() => enrichMutation.mutate()}
        />
      )}
    </div>
  );
}

function SectionHeading({ meta }: { meta?: { suggested: number; kept: number; dismissed: number } }) {
  return (
    <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
      Supplementary
      {meta && (
        <span className="ml-1.5 normal-case font-normal text-neutral-400 dark:text-neutral-500">
          ({meta.kept} kept · {meta.suggested} suggested)
        </span>
      )}
    </h3>
  );
}

interface ResourceRowProps {
  resource: EnrichmentResource;
  onSetStatus: (status: EnrichmentStatus) => void;
}

function ResourceRow({ resource, onSetStatus }: ResourceRowProps) {
  const Icon = (resource.resourceType && RESOURCE_TYPE_ICON[resource.resourceType]) || LinkIcon;
  const isKept = resource.status === "kept";
  const isDismissed = resource.status === "dismissed";
  const lowConfidence = isLowConfidence(resource.scores);

  return (
    <li className={isDismissed ? "opacity-50" : undefined}>
      <div className="flex items-start gap-2 rounded-md px-1.5 py-1">
        <Icon size={14} className="mt-0.5 shrink-0 text-neutral-400 dark:text-neutral-500" aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <a
              href={resource.url}
              target="_blank"
              rel="noopener noreferrer"
              className="min-w-0 truncate text-sm font-medium text-blue-600 hover:underline dark:text-blue-400"
            >
              {resource.title ?? resource.url}
            </a>
            {lowConfidence && (
              <span
                title="Lower-confidence match"
                className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500"
              />
            )}
          </div>
          {resource.rationale && (
            <p className="text-xs text-neutral-500 dark:text-neutral-400">{resource.rationale}</p>
          )}
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            {resource.intent && (
              <span className="rounded-full bg-neutral-100 px-2 py-0.5 text-[10px] text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
                {intentLabel(resource.intent)}
              </span>
            )}
            {isKept && (
              <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">
                kept
              </span>
            )}
          </div>
        </div>
        <div className="flex shrink-0 gap-1">
          <button
            type="button"
            onClick={() => onSetStatus(isKept ? "suggested" : "kept")}
            className={[
              "rounded-md border px-1.5 py-0.5 text-[11px] font-medium transition",
              isKept
                ? "border-emerald-300 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-300"
                : "border-neutral-300 text-neutral-600 hover:bg-neutral-100 dark:border-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-800",
            ].join(" ")}
          >
            {isKept ? "Kept" : "Keep"}
          </button>
          <button
            type="button"
            onClick={() => onSetStatus(isDismissed ? "suggested" : "dismissed")}
            className={[
              "rounded-md border px-1.5 py-0.5 text-[11px] font-medium transition",
              isDismissed
                ? "border-neutral-300 bg-neutral-100 text-neutral-500 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-400"
                : "border-neutral-300 text-neutral-600 hover:bg-neutral-100 dark:border-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-800",
            ].join(" ")}
          >
            {isDismissed ? "Dismissed" : "Dismiss"}
          </button>
        </div>
      </div>
    </li>
  );
}

interface EnrichDryRunConfirmDialogProps {
  dryRun: EnrichDryRunResponse;
  mockLlm: boolean;
  confirming: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

function formatUsd(amount: number): string {
  return `$${amount.toFixed(4)}`;
}

/** Same shape as CourseWorkspacePage's `DryRunConfirmDialog` (same
 * dry-run -> confirm pattern, same styling) but for the enrichment
 * dry-run's own response shape, which has no `byStage` breakdown -- just a
 * calls/cost-per-topic estimate (api/enrichment.py's `EnrichDryRunResponse`). */
function EnrichDryRunConfirmDialog({ dryRun, mockLlm, confirming, onCancel, onConfirm }: EnrichDryRunConfirmDialogProps) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="enrich-dry-run-confirm-title"
        className="w-full max-w-sm rounded-lg bg-white p-5 shadow-xl dark:bg-neutral-900"
      >
        <h2
          id="enrich-dry-run-confirm-title"
          className="text-base font-semibold text-neutral-900 dark:text-neutral-100"
        >
          Find supplementary materials?
        </h2>

        <ul className="mt-3 space-y-1 text-sm text-neutral-700 dark:text-neutral-300">
          <li className="flex items-center justify-between">
            <span>Estimated calls</span>
            <span className="tabular-nums text-neutral-500 dark:text-neutral-400">{dryRun.callsPerTopic}</span>
          </li>
          <li className="flex items-center justify-between">
            <span>Web searches (max)</span>
            <span className="tabular-nums text-neutral-500 dark:text-neutral-400">
              {dryRun.webSearchesPerTopic}
            </span>
          </li>
          <li className="flex items-center justify-between font-medium text-neutral-900 dark:text-neutral-100">
            <span>Estimated cost</span>
            <span className="tabular-nums">{formatUsd(dryRun.estCostPerTopicUsd)}</span>
          </li>
        </ul>

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
