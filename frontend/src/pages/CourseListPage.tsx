import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { getCourses } from "../api/client";

export function CourseListPage() {
  const {
    data: courses,
    isLoading,
    isError,
  } = useQuery({ queryKey: ["courses"], queryFn: getCourses });

  return (
    <div className="mx-auto max-w-4xl p-6">
      <div className="mb-4 flex items-baseline gap-3">
        <h1 className="text-xl font-semibold text-neutral-900 dark:text-neutral-100">Courses</h1>
        <div className="flex-1" />
        <Link
          to="/settings"
          className="text-sm text-neutral-500 hover:text-neutral-700 dark:text-neutral-400 dark:hover:text-neutral-200"
        >
          Settings
        </Link>
      </div>

      {isLoading && <p className="text-neutral-500 dark:text-neutral-400">Loading courses…</p>}
      {isError && (
        <p className="text-red-600 dark:text-red-400">
          Couldn't reach the backend. Is it running?
        </p>
      )}

      {courses && courses.length === 0 && (
        <div className="rounded-lg border border-dashed border-neutral-300 p-8 text-center dark:border-neutral-700">
          <p className="font-medium text-neutral-700 dark:text-neutral-200">
            No courses yet — sync one from the extension popup.
          </p>
          <p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">
            Open the BrightSpace Agent browser extension, pair it with this backend using the
            pairing token from Settings, then sync a course to see it here.
          </p>
        </div>
      )}

      {courses && courses.length > 0 && (
        <ul className="grid gap-3 sm:grid-cols-2">
          {courses.map((course) => (
            <li key={course.id}>
              <Link
                to={`/courses/${course.id}`}
                className="block rounded-lg border border-neutral-200 bg-white p-4 shadow-sm transition hover:border-neutral-300 hover:shadow dark:border-neutral-700 dark:bg-neutral-900 dark:hover:border-neutral-600"
              >
                <div className="flex items-center justify-between gap-2">
                  <h2 className="truncate font-medium text-neutral-900 dark:text-neutral-100">
                    {course.name}
                  </h2>
                  <span className="shrink-0 rounded-full bg-neutral-100 px-2 py-0.5 text-xs text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
                    v{course.taxonomyVersion}
                  </span>
                </div>
                <p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">
                  {[course.code, course.term].filter(Boolean).join(" · ") || "No code or term"}
                </p>
                <div className="mt-3 flex items-center gap-2 text-xs text-neutral-500 dark:text-neutral-400">
                  <span>
                    {course.materialCounts.summarized}/{course.materialCounts.total} materials
                    summarized
                  </span>
                  {course.pipeline && (
                    <span className="rounded-full bg-blue-50 px-2 py-0.5 text-blue-700 dark:bg-blue-950 dark:text-blue-300">
                      {course.pipeline.status}
                    </span>
                  )}
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
