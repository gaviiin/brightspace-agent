import { useQuery } from "@tanstack/react-query";

import { getRuns } from "../api/client";
import type { PipelineRunSummary, SyncRunSummary } from "../api/types";

const STAGE_LABELS: Record<string, string> = {
  summarize: "Summarize",
  taxonomy: "Build taxonomy",
  classify: "Classify",
  assemble: "Assemble",
  enrich: "Enrich",
};

function formatUsd(amount: number): string {
  return `$${amount.toFixed(4)}`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatWhen(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function StatusBadge({ status }: { status: string }) {
  const tone =
    status === "complete"
      ? "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300"
      : status === "running"
        ? "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
        : "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300";
  return <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs ${tone}`}>{status}</span>;
}

function SyncRunRow({ run }: { run: SyncRunSummary }) {
  return (
    <li className="rounded-md border border-neutral-200 p-2 dark:border-neutral-800">
      <div className="flex items-center gap-2">
        <span className="text-sm text-neutral-900 dark:text-neutral-100">
          {run.files} files · {formatBytes(run.bytes)}
        </span>
        <div className="flex-1" />
        <StatusBadge status={run.status} />
      </div>
      <p className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">
        {formatWhen(run.startedAt)} · {run.source}
        {run.notNeeded > 0 && ` · ${run.notNeeded} unchanged`}
      </p>
      {run.errorCount > 0 && (
        <div className="mt-1.5 rounded-md bg-red-50 p-2 dark:bg-red-950/40">
          <p className="text-xs font-medium text-red-700 dark:text-red-300">
            {run.errorCount === 1 ? "1 error" : `${run.errorCount} errors`}
          </p>
          <ul className="mt-1 space-y-0.5">
            {run.errors.map((error, index) => (
              <li key={index} className="truncate text-xs text-red-600 dark:text-red-400">
                {error.d2lTopicId !== null ? `#${error.d2lTopicId}: ` : ""}
                {error.message}
              </li>
            ))}
          </ul>
          {run.errorCount > run.errors.length && (
            <p className="mt-1 text-xs text-red-500 dark:text-red-400">
              …and {run.errorCount - run.errors.length} more
            </p>
          )}
        </div>
      )}
    </li>
  );
}

function PipelineRunRow({ run }: { run: PipelineRunSummary }) {
  return (
    <li className="rounded-md border border-neutral-200 p-2 dark:border-neutral-800">
      <div className="flex items-center gap-2">
        <span className="text-sm text-neutral-900 dark:text-neutral-100">
          {STAGE_LABELS[run.stage] ?? run.stage}
        </span>
        <div className="flex-1" />
        <span className="tabular-nums text-xs text-neutral-500 dark:text-neutral-400">
          {formatUsd(run.estCostUsd)}
        </span>
        <StatusBadge status={run.status} />
      </div>
      <p className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">
        {formatWhen(run.startedAt)} ·{" "}
        <span className="tabular-nums">
          {`${run.inputTokens.toLocaleString("en-US")} in / ${run.outputTokens.toLocaleString("en-US")} out`}
        </span>
      </p>
      {run.error && <p className="mt-1 text-xs text-red-600 dark:text-red-400">{run.error}</p>}
    </li>
  );
}

interface RunsDrawerProps {
  courseId: number;
  onClose: () => void;
}

/** Right-side drawer listing the course's recent sync and pipeline runs --
 * what synced (and what failed), and what each pipeline stage cost. */
export function RunsDrawer({ courseId, onClose }: RunsDrawerProps) {
  const runsQuery = useQuery({ queryKey: ["runs", courseId], queryFn: () => getRuns(courseId) });
  const runs = runsQuery.data;
  const totalShownCost = runs?.pipelineRuns.reduce((sum, run) => sum + run.estCostUsd, 0) ?? 0;

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/40" onClick={onClose}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Run history"
        onClick={(event) => event.stopPropagation()}
        className="flex h-full w-full max-w-[420px] flex-col bg-white shadow-xl dark:bg-neutral-900"
      >
        <header className="flex shrink-0 items-center justify-between border-b border-neutral-200 px-4 py-3 dark:border-neutral-800">
          <h2 className="text-base font-semibold text-neutral-900 dark:text-neutral-100">Runs</h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-2 py-1 text-sm text-neutral-500 hover:bg-neutral-100 hover:text-neutral-700 dark:text-neutral-400 dark:hover:bg-neutral-800 dark:hover:text-neutral-200"
          >
            Close
          </button>
        </header>

        <div className="flex-1 space-y-5 overflow-y-auto p-4">
          {runsQuery.isLoading && (
            <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading run history…</p>
          )}
          {runsQuery.isError && (
            <p className="text-sm text-red-600 dark:text-red-400">Couldn't load run history.</p>
          )}

          {runs && (
            <>
              <section>
                <h3 className="text-sm font-medium text-neutral-900 dark:text-neutral-100">Syncs</h3>
                {runs.syncRuns.length === 0 ? (
                  <p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">No syncs yet.</p>
                ) : (
                  <ul className="mt-2 space-y-2">
                    {runs.syncRuns.map((run) => (
                      <SyncRunRow key={run.id} run={run} />
                    ))}
                  </ul>
                )}
              </section>

              <section>
                <h3 className="text-sm font-medium text-neutral-900 dark:text-neutral-100">
                  Pipeline runs
                </h3>
                {runs.pipelineRuns.length === 0 ? (
                  <p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">
                    No pipeline runs yet.
                  </p>
                ) : (
                  <>
                    <ul className="mt-2 space-y-2">
                      {runs.pipelineRuns.map((run) => (
                        <PipelineRunRow key={run.id} run={run} />
                      ))}
                    </ul>
                    <div className="mt-2 flex items-center justify-between border-t border-neutral-200 pt-2 text-sm text-neutral-900 dark:border-neutral-800 dark:text-neutral-100">
                      <span>Total shown</span>
                      <span className="tabular-nums font-medium">{formatUsd(totalShownCost)}</span>
                    </div>
                  </>
                )}
              </section>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}
