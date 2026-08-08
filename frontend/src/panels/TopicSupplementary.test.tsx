import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    getTopicEnrichment: vi.fn(),
    enrichTopic: vi.fn(),
    setEnrichmentStatus: vi.fn(),
    enrichDryRun: vi.fn(),
  };
});

import { enrichDryRun, enrichTopic, getTopicEnrichment, setEnrichmentStatus } from "../api/client";
import type { EnrichDryRunResponse, EnrichmentResource, TopicEnrichment } from "../api/types";
import { TopicSupplementary } from "./TopicSupplementary";

const mockedGetTopicEnrichment = vi.mocked(getTopicEnrichment);
const mockedEnrichTopic = vi.mocked(enrichTopic);
const mockedSetEnrichmentStatus = vi.mocked(setEnrichmentStatus);
const mockedEnrichDryRun = vi.mocked(enrichDryRun);

function resource(overrides: Partial<EnrichmentResource> = {}): EnrichmentResource {
  return {
    id: 1,
    url: "https://ocw.mit.edu/lecture-1",
    title: "MIT OCW: Lecture 1",
    resourceType: "video",
    intent: "video_lecture",
    rationale: "Covers the same idea with a worked video walkthrough.",
    scores: { relevance: 0.9, authority: 0.8 },
    verification: { ok: true, level_fit: "on_level" },
    rank: 1,
    shared: false,
    status: "suggested",
    ...overrides,
  };
}

function enrichmentFixture(overrides: Partial<TopicEnrichment> = {}): TopicEnrichment {
  return {
    topicId: 5,
    resources: [resource()],
    meta: { suggested: 1, kept: 0, dismissed: 0, searched: true, thin: false },
    ...overrides,
  };
}

const EMPTY_NEVER_SEARCHED: Partial<TopicEnrichment> = {
  resources: [],
  meta: { suggested: 0, kept: 0, dismissed: 0, searched: false, thin: false },
};

const EMPTY_SEARCHED_THIN: Partial<TopicEnrichment> = {
  resources: [],
  meta: { suggested: 0, kept: 0, dismissed: 0, searched: true, thin: true },
};

function dryRunFixture(overrides: Partial<EnrichDryRunResponse> = {}): EnrichDryRunResponse {
  return {
    topicsNeedingEnrichment: 3,
    callsPerTopic: 12,
    estCostPerTopicUsd: 0.05,
    totalEstCostUsd: 0.15,
    webSearchesPerTopic: 40,
    ...overrides,
  };
}

interface RenderOptions {
  topicId?: number;
  courseId?: number;
  mockLlm?: boolean;
  runActive?: boolean;
}

function renderSupplementary(options: RenderOptions = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return render(
    <TopicSupplementary
      topicId={options.topicId ?? 5}
      courseId={options.courseId ?? 1}
      mockLlm={options.mockLlm ?? false}
      runActive={options.runActive ?? false}
    />,
    { wrapper },
  );
}

beforeEach(() => {
  mockedGetTopicEnrichment.mockReset();
  mockedEnrichTopic.mockReset();
  mockedSetEnrichmentStatus.mockReset();
  mockedEnrichDryRun.mockReset();
});

afterEach(cleanup);

describe("TopicSupplementary: resources present", () => {
  it("renders each resource's title link, rationale, and intent chip", async () => {
    mockedGetTopicEnrichment.mockResolvedValue(enrichmentFixture());
    renderSupplementary();

    const link = await screen.findByRole("link", { name: /MIT OCW: Lecture 1/ });
    expect(link.getAttribute("href")).toBe("https://ocw.mit.edu/lecture-1");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");

    expect(screen.getByText("Covers the same idea with a worked video walkthrough.")).toBeTruthy();
    expect(screen.getByText(/video lecture/i)).toBeTruthy();
  });

  it("Keep calls setEnrichmentStatus(id, 'kept'); Dismiss calls with 'dismissed'", async () => {
    mockedGetTopicEnrichment.mockResolvedValue(enrichmentFixture());
    mockedSetEnrichmentStatus.mockResolvedValue(resource({ status: "kept" }));
    renderSupplementary();

    await screen.findByRole("link", { name: /MIT OCW: Lecture 1/ });

    fireEvent.click(screen.getByRole("button", { name: "Keep" }));
    await vi.waitFor(() => expect(mockedSetEnrichmentStatus).toHaveBeenCalledWith(1, "kept"));

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await vi.waitFor(() => expect(mockedSetEnrichmentStatus).toHaveBeenCalledWith(1, "dismissed"));
  });
});

describe("TopicSupplementary: empty state", () => {
  it("says 'not searched yet' when enrichment has never run for this topic", async () => {
    mockedGetTopicEnrichment.mockResolvedValue(enrichmentFixture(EMPTY_NEVER_SEARCHED));
    renderSupplementary();

    await screen.findByText(/not searched yet/i);
    expect(screen.queryByText(/nothing good enough/i)).toBeNull();
    expect(screen.getByRole("button", { name: /find supplementary materials/i })).toBeTruthy();
  });

  it("says it searched and found nothing when the last run came back thin", async () => {
    mockedGetTopicEnrichment.mockResolvedValue(enrichmentFixture(EMPTY_SEARCHED_THIN));
    renderSupplementary();

    await screen.findByText(/searched, but nothing good enough was found/i);
    expect(screen.queryByText(/not searched yet/i)).toBeNull();
  });
});

describe("TopicSupplementary: find/refresh dry-run -> confirm flow", () => {
  it("opens the confirm dialog on dry-run success, then calls enrichTopic on confirm", async () => {
    mockedGetTopicEnrichment.mockResolvedValue(enrichmentFixture(EMPTY_NEVER_SEARCHED));
    mockedEnrichDryRun.mockResolvedValue(dryRunFixture());
    mockedEnrichTopic.mockResolvedValue({ runToken: 99 });
    renderSupplementary();

    const findButton = await screen.findByRole("button", { name: /find supplementary materials/i });
    fireEvent.click(findButton);

    await vi.waitFor(() => expect(mockedEnrichDryRun).toHaveBeenCalledWith(1));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await vi.waitFor(() => expect(mockedEnrichTopic).toHaveBeenCalledWith(5));
  });

  it("shows the mock-mode note when mockLlm is true", async () => {
    mockedGetTopicEnrichment.mockResolvedValue(enrichmentFixture(EMPTY_NEVER_SEARCHED));
    mockedEnrichDryRun.mockResolvedValue(dryRunFixture());
    renderSupplementary({ mockLlm: true });

    fireEvent.click(await screen.findByRole("button", { name: /find supplementary materials/i }));
    await screen.findByRole("dialog");

    expect(screen.getByText(/mock mode/i)).toBeTruthy();
  });

  it("surfaces an enrichTopic failure in the dialog instead of silently un-'Starting…'", async () => {
    mockedGetTopicEnrichment.mockResolvedValue(enrichmentFixture(EMPTY_NEVER_SEARCHED));
    mockedEnrichDryRun.mockResolvedValue(dryRunFixture());
    mockedEnrichTopic.mockRejectedValue(new Error("409: a run is already active for this course"));
    renderSupplementary();

    fireEvent.click(await screen.findByRole("button", { name: /find supplementary materials/i }));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("a run is already active");
    // The dialog stays open so the student can retry or cancel.
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("disables the Find/Refresh button while a course run is active", async () => {
    mockedGetTopicEnrichment.mockResolvedValue(enrichmentFixture(EMPTY_NEVER_SEARCHED));
    renderSupplementary({ runActive: true });

    await screen.findByText(/not searched yet/i);
    // Only one button renders in the empty state, and while a course run is
    // active its label changes to "Running…" (mirroring CourseWorkspacePage's
    // own "Run pipeline" button), so this queries by role alone rather than
    // by the Find/Refresh name.
    const findButton = screen.getByRole("button");
    expect(findButton.hasAttribute("disabled")).toBe(true);
  });
});
