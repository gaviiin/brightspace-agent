import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { detectMedia, getMedia, processMedia, processMediaSource, updateMediaSource } from "../api/client";
import type { MediaSourceStatus, MediaSourceSummary, MediaSourceUpdateRequest } from "../api/types";
import { useUiStore } from "../state/uiStore";
import { StatusBadge } from "./RunsDrawer";

const PLATFORM_LABELS: Record<string, string> = {
  mediasite: "Mediasite",
  zoom: "Zoom",
  gdrive: "Drive",
};

/** Color-coding for `MediaSourceSummary.status` -- layered on RunsDrawer's
 * shared `StatusBadge` shell rather than duplicated: its own complete/
 * running/error 3-way default doesn't fit media's six-value status set, so
 * this supplies a tone per value via the badge's `tone` override. */
const MEDIA_STATUS_TONE: Record<MediaSourceStatus, string> = {
  detected: "bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300",
  fetching: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
  transcribing: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
  done: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
  failed: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  skipped: "bg-neutral-100 text-neutral-500 dark:bg-neutral-800 dark:text-neutral-400",
};

const ACTION_BUTTON_CLASS =
  "rounded-md border border-neutral-300 px-1.5 py-0.5 text-[11px] font-medium text-neutral-600 transition hover:bg-neutral-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-800";

/** api/client.ts throws an Error carrying the backend's status/detail (same
 * helper as TopicSupplementary's) -- keeps a 409's detail readable instead
 * of rendering "[object Object]". */
function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return "unexpected error";
}

interface RecordingsDrawerProps {
  courseId: number;
  onClose: () => void;
}

/** Right-side drawer listing the course's detected lecture recordings --
 * platform/status per source, passcode entry for Zoom, and the
 * detect/process triggers. Modeled on RunsDrawer's overlay/aside/dialog
 * markup and close semantics. Reads stay enabled while a run is active
 * (`mediaQuery` is never gated on it) -- CourseWorkspacePage's SSE effect
 * invalidates `["media", courseId]` on every matching event, so progress
 * shows up via refetch rather than this component polling on its own. */
export function RecordingsDrawer({ courseId, onClose }: RecordingsDrawerProps) {
  const queryClient = useQueryClient();
  const setSelection = useUiStore((state) => state.setSelection);

  const mediaQuery = useQuery({ queryKey: ["media", courseId], queryFn: () => getMedia(courseId) });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["media", courseId] });

  const detectMutation = useMutation({
    mutationFn: () => detectMedia(courseId),
    onSuccess: invalidate,
  });

  const processAllMutation = useMutation({
    mutationFn: () => processMedia(courseId),
    onSuccess: invalidate,
  });

  const processSourceMutation = useMutation({
    mutationFn: (sourceId: number) => processMediaSource(sourceId),
    onSuccess: invalidate,
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, body }: { id: number; body: MediaSourceUpdateRequest }) => updateMediaSource(id, body),
    onSuccess: invalidate,
  });

  const sources = mediaQuery.data?.sources ?? [];
  const active = mediaQuery.data?.active ?? false;
  const hasProcessable = sources.some((s) => s.status === "detected" || s.status === "failed");
  const detectDisabled = active || detectMutation.isPending;
  const processAllDisabled = active || !hasProcessable || processAllMutation.isPending;

  function selectTranscript(materialId: number) {
    setSelection({ type: "material", id: materialId });
    onClose();
  }

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/40" onClick={onClose}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Recordings"
        onClick={(event) => event.stopPropagation()}
        className="flex h-full w-full max-w-[420px] flex-col bg-white shadow-xl dark:bg-neutral-900"
      >
        <header className="flex shrink-0 items-center gap-2 border-b border-neutral-200 px-4 py-3 dark:border-neutral-800">
          <h2 className="text-base font-semibold text-neutral-900 dark:text-neutral-100">Recordings</h2>
          <div className="flex-1" />
          <button
            type="button"
            title="Scan course for recordings"
            disabled={detectDisabled}
            onClick={() => detectMutation.mutate()}
            className="rounded-md border border-neutral-300 px-2.5 py-1 text-xs font-medium text-neutral-700 transition hover:bg-neutral-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-neutral-700 dark:text-neutral-200 dark:hover:bg-neutral-800"
          >
            {detectMutation.isPending ? "Scanning…" : "Detect"}
          </button>
          <button
            type="button"
            disabled={processAllDisabled}
            onClick={() => processAllMutation.mutate()}
            className="rounded-md bg-blue-600 px-2.5 py-1 text-xs font-medium text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {processAllMutation.isPending ? "Starting…" : "Process all"}
          </button>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-2 py-1 text-sm text-neutral-500 hover:bg-neutral-100 hover:text-neutral-700 dark:text-neutral-400 dark:hover:bg-neutral-800 dark:hover:text-neutral-200"
          >
            Close
          </button>
        </header>

        <div className="flex-1 space-y-2 overflow-y-auto p-4">
          {mediaQuery.isLoading && (
            <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading recordings…</p>
          )}
          {mediaQuery.isError && (
            <p className="text-sm text-red-600 dark:text-red-400">Couldn't load recordings.</p>
          )}

          {detectMutation.isError && (
            <p className="text-xs text-red-600 dark:text-red-400">{errorMessage(detectMutation.error)}</p>
          )}
          {processAllMutation.isError && (
            <p className="text-xs text-red-600 dark:text-red-400">{errorMessage(processAllMutation.error)}</p>
          )}

          {mediaQuery.data && sources.length === 0 && (
            <div>
              <p className="text-sm text-neutral-500 dark:text-neutral-400">No recordings detected yet.</p>
              <p className="mt-1 text-xs text-neutral-400 dark:text-neutral-500">Sync the course, then Scan.</p>
            </div>
          )}

          {sources.length > 0 && (
            <ul className="space-y-2">
              {sources.map((source) => (
                <SourceRow
                  key={source.id}
                  source={source}
                  active={active}
                  onProcess={() => processSourceMutation.mutate(source.id)}
                  processPending={
                    processSourceMutation.isPending && processSourceMutation.variables === source.id
                  }
                  processError={
                    processSourceMutation.isError && processSourceMutation.variables === source.id
                      ? errorMessage(processSourceMutation.error)
                      : null
                  }
                  onSkip={() => updateMutation.mutate({ id: source.id, body: { status: "skipped" } })}
                  onUnskip={() => updateMutation.mutate({ id: source.id, body: { status: "detected" } })}
                  onSavePasscode={(passcode) => updateMutation.mutate({ id: source.id, body: { passcode } })}
                  updatePending={updateMutation.isPending && updateMutation.variables?.id === source.id}
                  updateError={
                    updateMutation.isError && updateMutation.variables?.id === source.id
                      ? errorMessage(updateMutation.error)
                      : null
                  }
                  onSelectTranscript={selectTranscript}
                />
              ))}
            </ul>
          )}
        </div>
      </aside>
    </div>
  );
}

interface SourceRowProps {
  source: MediaSourceSummary;
  /** `mediaQuery.data.active` -- any course run (pipeline/enrichment/media),
   * not just a media run, since the backend's PUT/process guards on the
   * same shared `_active` flag. Disables this row's Process/Skip/Unskip and
   * the Zoom passcode Save so they don't sit clickable-but-deterministically-
   * 409 for the whole time another run is in flight. */
  active: boolean;
  onProcess: () => void;
  processPending: boolean;
  processError: string | null;
  onSkip: () => void;
  onUnskip: () => void;
  onSavePasscode: (passcode: string | null) => void;
  updatePending: boolean;
  updateError: string | null;
  onSelectTranscript: (materialId: number) => void;
}

function SourceRow({
  source,
  active,
  onProcess,
  processPending,
  processError,
  onSkip,
  onUnskip,
  onSavePasscode,
  updatePending,
  updateError,
  onSelectTranscript,
}: SourceRowProps) {
  const busy = processPending || updatePending || active;

  return (
    <li className="rounded-md border border-neutral-200 p-2 dark:border-neutral-800">
      <div className="flex items-center gap-2">
        <span className="shrink-0 rounded-full bg-neutral-100 px-2 py-0.5 text-xs text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
          {PLATFORM_LABELS[source.platform] ?? source.platform}
        </span>
        <span className="min-w-0 flex-1 truncate text-sm text-neutral-900 dark:text-neutral-100">
          {source.materialTitle}
        </span>
        <StatusBadge status={source.status} tone={MEDIA_STATUS_TONE[source.status]} />
      </div>

      {source.status === "failed" && source.error && (
        <p className="mt-1 text-xs text-red-600 dark:text-red-400">{source.error}</p>
      )}

      {(source.status === "fetching" || source.status === "transcribing") && (
        <p className="mt-1 text-xs text-neutral-500 dark:text-neutral-400">Working…</p>
      )}

      {source.platform === "zoom" && (
        <PasscodeEditor passcode={source.passcode} onSave={onSavePasscode} saving={updatePending || active} />
      )}

      {processError && <p className="mt-1 text-xs text-red-600 dark:text-red-400">{processError}</p>}
      {updateError && <p className="mt-1 text-xs text-red-600 dark:text-red-400">{updateError}</p>}

      {(source.status === "detected" ||
        source.status === "failed" ||
        source.status === "skipped" ||
        source.transcriptMaterialId !== null) && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {(source.status === "detected" || source.status === "failed") && (
            <>
              <button type="button" disabled={busy} onClick={onProcess} className={ACTION_BUTTON_CLASS}>
                Process
              </button>
              <button type="button" disabled={busy} onClick={onSkip} className={ACTION_BUTTON_CLASS}>
                Skip
              </button>
            </>
          )}
          {source.status === "skipped" && (
            <button type="button" disabled={busy} onClick={onUnskip} className={ACTION_BUTTON_CLASS}>
              Unskip
            </button>
          )}
          {source.transcriptMaterialId !== null && (
            <button
              type="button"
              onClick={() => onSelectTranscript(source.transcriptMaterialId as number)}
              className={ACTION_BUTTON_CLASS}
            >
              Transcript ready
            </button>
          )}
        </div>
      )}
    </li>
  );
}

interface PasscodeEditorProps {
  passcode: string | null;
  onSave: (passcode: string | null) => void;
  saving: boolean;
}

/** Small input + Save, pre-filled with the current passcode (doubles as the
 * "display" the brief asks for -- there's nothing to mask, see
 * `MediaSourceOut`'s docstring: local single-user app). Saving an emptied
 * field sends `null` (clears the stored passcode) rather than `""`.
 * Re-syncs from the prop when it changes (e.g. after a successful save
 * refetches the row) unless that would clobber an in-progress edit. */
function PasscodeEditor({ passcode, onSave, saving }: PasscodeEditorProps) {
  const [value, setValue] = useState(passcode ?? "");
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (!dirty) setValue(passcode ?? "");
  }, [passcode, dirty]);

  return (
    <div className="mt-1.5 flex items-center gap-1.5">
      <span className="text-xs text-neutral-500 dark:text-neutral-400">Passcode</span>
      <input
        type="text"
        aria-label="Passcode"
        value={value}
        onChange={(event) => {
          setValue(event.target.value);
          setDirty(true);
        }}
        placeholder="none"
        className="w-24 rounded-md border border-neutral-300 px-1.5 py-0.5 text-xs text-neutral-900 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-100"
      />
      <button
        type="button"
        disabled={saving}
        onClick={() => {
          setDirty(false);
          onSave(value === "" ? null : value);
        }}
        className={ACTION_BUTTON_CLASS}
      >
        Save
      </button>
    </div>
  );
}
