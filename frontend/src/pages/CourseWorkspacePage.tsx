import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { getCourse, getGraph, openEvents, pipelineStatus, runPipeline } from "../api/client";
import type { BsaEvent } from "../api/types";
import { GraphView } from "../graph/GraphView";
import { useUiStore } from "../state/uiStore";

export function CourseWorkspacePage() {
  const { courseId: courseIdParam } = useParams<{ courseId: string }>();
  const courseId = Number(courseIdParam);
  const courseIdValid = Number.isFinite(courseId);

  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();

  const selection = useUiStore((state) => state.selection);
  const setSelection = useUiStore((state) => state.setSelection);

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

  const runMutation = useMutation({
    mutationFn: () => runPipeline(courseId),
    onSuccess: () => {
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
  // counts) since that's when the data actually changed.
  useEffect(() => {
    if (!courseIdValid) return;
    const source = openEvents((event: BsaEvent) => {
      if (event.type !== "pipeline" || event.courseId !== courseId) return;
      queryClient.invalidateQueries({ queryKey: ["pipeline-status", courseId] });
      if (event.status === "run-finished") {
        queryClient.invalidateQueries({ queryKey: ["graph", courseId] });
        queryClient.invalidateQueries({ queryKey: ["course", courseId] });
      }
    });
    return () => source.close();
  }, [courseId, courseIdValid, queryClient]);

  const course = courseQuery.data;
  const active = statusQuery.data?.active ?? false;

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
        <button
          type="button"
          disabled={!courseIdValid || active || runMutation.isPending}
          onClick={() => runMutation.mutate()}
          className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {active ? "Running…" : "Run pipeline"}
        </button>
      </header>

      <div className="flex flex-1 overflow-hidden">
        <aside className="w-[240px] shrink-0 overflow-y-auto border-r border-neutral-200 p-3 text-sm text-neutral-500 dark:border-neutral-800 dark:text-neutral-400">
          Outline — Task 11
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

        <aside className="w-[320px] shrink-0 overflow-y-auto border-l border-neutral-200 p-3 text-sm dark:border-neutral-800">
          <p className="mb-2 text-neutral-500 dark:text-neutral-400">Details — Task 11</p>
          <pre className="whitespace-pre-wrap break-words text-xs text-neutral-600 dark:text-neutral-300">
            {JSON.stringify(selection, null, 2)}
          </pre>
        </aside>
      </div>
    </div>
  );
}
