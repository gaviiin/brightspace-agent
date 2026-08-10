import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunsResponse } from "../api/types";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, getRuns: vi.fn() };
});

import { getRuns } from "../api/client";
import { RunsDrawer } from "./RunsDrawer";

const mockedGetRuns = vi.mocked(getRuns);

function fixtureRuns(): RunsResponse {
  return {
    syncRuns: [
      {
        id: 3,
        source: "extension",
        status: "failed",
        startedAt: "2026-08-06T22:28:46+00:00",
        finishedAt: "2026-08-06T22:29:20+00:00",
        files: 0,
        bytes: 0,
        notNeeded: 0,
        errorCount: 73,
        errors: [
          { d2lTopicId: 12988284, message: "Failed to fetch" },
          { d2lTopicId: 12988281, message: "Failed to fetch" },
          { d2lTopicId: 13020401, message: "Failed to fetch" },
          { d2lTopicId: 13031769, message: "Failed to fetch" },
          { d2lTopicId: 13046928, message: "Failed to fetch" },
        ],
      },
      {
        id: 2,
        source: "extension",
        status: "complete",
        startedAt: "2026-08-06T22:33:41+00:00",
        finishedAt: "2026-08-06T22:34:25+00:00",
        files: 73,
        bytes: 58487965,
        notNeeded: 2,
        errorCount: 0,
        errors: [],
      },
    ],
    pipelineRuns: [
      {
        id: 7,
        stage: "summarize",
        status: "complete",
        startedAt: "2026-08-06T23:00:00+00:00",
        finishedAt: "2026-08-06T23:05:00+00:00",
        inputTokens: 275792,
        outputTokens: 9147,
        estCostUsd: 0.3215,
        error: null,
      },
      {
        id: 8,
        stage: "classify",
        status: "failed",
        startedAt: "2026-08-06T23:06:00+00:00",
        finishedAt: null,
        inputTokens: 0,
        outputTokens: 0,
        estCostUsd: 0,
        error: "cost cap exceeded",
      },
    ],
  };
}

function renderDrawer(onClose = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }
  render(<RunsDrawer courseId={15} onClose={onClose} />, { wrapper: Wrapper });
  return onClose;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("RunsDrawer", () => {
  it("renders sync runs with file counts, sizes, and status", async () => {
    mockedGetRuns.mockResolvedValue(fixtureRuns());

    renderDrawer();

    expect(await screen.findByText("73 files · 55.8 MB")).toBeTruthy();
    expect(screen.getAllByText("complete").length).toBeGreaterThan(0);
    expect(screen.getAllByText("failed").length).toBeGreaterThan(0);
  });

  it("lists a failed sync's first errors and says how many more there are", async () => {
    mockedGetRuns.mockResolvedValue(fixtureRuns());

    renderDrawer();

    expect(await screen.findByText("73 errors")).toBeTruthy();
    expect(screen.getAllByText(/Failed to fetch/).length).toBe(5);
    expect(screen.getByText("…and 68 more")).toBeTruthy();
  });

  it("renders pipeline runs with stage, tokens, cost, and error", async () => {
    mockedGetRuns.mockResolvedValue(fixtureRuns());

    renderDrawer();

    expect(await screen.findByText("Summarize")).toBeTruthy();
    expect(screen.getByText("275,792 in / 9,147 out")).toBeTruthy();
    // Once on the run row, once as the total (the fixture has one paid run).
    expect(screen.getAllByText("$0.3215")).toHaveLength(2);
    expect(screen.getByText("cost cap exceeded")).toBeTruthy();
  });

  it("totals the shown pipeline spend", async () => {
    mockedGetRuns.mockResolvedValue(fixtureRuns());

    renderDrawer();

    expect(await screen.findByText("Total shown")).toBeTruthy();
    expect(screen.getByText("$0.3215", { selector: ".tabular-nums.font-medium" })).toBeTruthy();
  });

  it("shows empty states when there is no history", async () => {
    mockedGetRuns.mockResolvedValue({ syncRuns: [], pipelineRuns: [] });

    renderDrawer();

    expect(await screen.findByText("No syncs yet.")).toBeTruthy();
    expect(screen.getByText("No pipeline runs yet.")).toBeTruthy();
  });

  it("shows an error message when the request fails", async () => {
    mockedGetRuns.mockRejectedValue(new Error("boom"));

    renderDrawer();

    expect(await screen.findByText("Couldn't load run history.")).toBeTruthy();
  });

  it("calls onClose from the close button", async () => {
    mockedGetRuns.mockResolvedValue(fixtureRuns());
    const onClose = renderDrawer();

    fireEvent.click(await screen.findByRole("button", { name: "Close" }));

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("labels a 'media' stage pipeline run as 'Process recordings' (M2.5)", async () => {
    mockedGetRuns.mockResolvedValue({
      syncRuns: [],
      pipelineRuns: [
        {
          id: 9,
          stage: "media",
          status: "complete",
          startedAt: "2026-08-06T23:06:00+00:00",
          finishedAt: "2026-08-06T23:07:00+00:00",
          inputTokens: 0,
          outputTokens: 0,
          estCostUsd: 0,
          error: null,
        },
      ],
    });

    renderDrawer();

    expect(await screen.findByText("Process recordings")).toBeTruthy();
  });
});
