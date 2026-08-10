import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GraphPayload, MaterialDetail } from "../api/types";
import { ADMIN_TOPIC_ID, UNSORTED_TOPIC_ID } from "../api/types";
import { useUiStore } from "../state/uiStore";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, getMaterial: vi.fn(), getTopicEnrichment: vi.fn() };
});

import { getMaterial, getTopicEnrichment } from "../api/client";
import { DetailPanel } from "./DetailPanel";

const mockedGetMaterial = vi.mocked(getMaterial);
const mockedGetTopicEnrichment = vi.mocked(getTopicEnrichment);

function fixturePayload(): GraphPayload {
  return {
    topics: [
      { id: 1, slug: "intro", name: "Intro", description: "Intro topic desc.", orderIndex: 0, materialCount: 1 },
      { id: 2, slug: "advanced", name: "Advanced", description: "Advanced topic desc.", orderIndex: 1, materialCount: 0 },
      { id: 3, slug: "related-topic", name: "Related Topic", description: "", orderIndex: 2, materialCount: 0 },
    ],
    materials: [
      { id: 10, title: "Lecture 1", kind: "other", status: "summarized", maxConfidence: 0.9 },
    ],
    // Topic 1 must be understood before Topic 2 -> from Topic 1's detail,
    // this is a "required by" chip (Topic 2 requires Topic 1).
    topicEdges: [
      { fromTopicId: 1, toTopicId: 2, relation: "prerequisite" },
      { fromTopicId: 1, toTopicId: 3, relation: "related" },
    ],
    attachments: [{ topicId: 1, materialId: 10, confidence: 0.9, rationale: "on topic" }],
    meta: { taxonomyVersion: 1, orphanCount: 0 },
  };
}

function renderWithQueryClient(ui: ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

/** Defaults the M3.3 props every test doesn't care about (courseId,
 * mockLlm, runActive) so only the tests that do need to set them. */
function renderDetailPanel(payload: GraphPayload, overrides: { runActive?: boolean } = {}) {
  return renderWithQueryClient(
    <DetailPanel
      payload={payload}
      courseId={1}
      mockLlm={false}
      runActive={overrides.runActive ?? false}
    />,
  );
}

beforeEach(() => {
  useUiStore.setState({ expandedTopicIds: new Set(), selection: null });
  mockedGetMaterial.mockReset();
  mockedGetTopicEnrichment.mockReset();
  mockedGetTopicEnrichment.mockResolvedValue({
    topicId: 1,
    resources: [],
    meta: { suggested: 0, kept: 0, dismissed: 0, searched: false, thin: false },
  });
});

afterEach(cleanup);

describe("DetailPanel: nothing selected", () => {
  it("shows a hint", () => {
    renderDetailPanel(fixturePayload());
    expect(screen.getByText(/select a topic or material/i)).toBeTruthy();
  });
});

describe("DetailPanel: topic selected", () => {
  it("renders the topic's materials and edge chips", () => {
    useUiStore.setState({ selection: { type: "topic", id: 1 } });
    renderDetailPanel(fixturePayload());

    expect(screen.getByText("Intro")).toBeTruthy();
    expect(screen.getByText("Intro topic desc.")).toBeTruthy();
    expect(screen.getByText("Lecture 1")).toBeTruthy();
    expect(screen.getByText(/required by: Advanced/)).toBeTruthy();
    expect(screen.getByText(/related: Related Topic/)).toBeTruthy();
  });

  it("clicking an edge chip moves the selection to that topic", () => {
    useUiStore.setState({ selection: { type: "topic", id: 1 } });
    renderDetailPanel(fixturePayload());

    fireEvent.click(screen.getByText(/required by: Advanced/));

    expect(useUiStore.getState().selection).toEqual({ type: "topic", id: 2 });
  });

  it("clicking a material in the topic's list selects that material", () => {
    useUiStore.setState({ selection: { type: "topic", id: 1 } });
    renderDetailPanel(fixturePayload());

    fireEvent.click(screen.getByText("Lecture 1"));

    expect(useUiStore.getState().selection).toEqual({ type: "material", id: 10 });
  });

  it("renders the Supplementary section below the materials list", async () => {
    useUiStore.setState({ selection: { type: "topic", id: 1 } });
    renderDetailPanel(fixturePayload());

    expect(await screen.findByText(/not searched yet/i)).toBeTruthy();
    expect(mockedGetTopicEnrichment).toHaveBeenCalledWith(1);
  });
});

describe("DetailPanel: material selected", () => {
  function materialFixture(overrides: Partial<MaterialDetail> = {}): MaterialDetail {
    return {
      id: 10,
      courseId: 1,
      title: "Lecture 1",
      kind: "other",
      status: "summarized",
      mime: null,
      sizeBytes: null,
      sourceUrl: null,
      summary: "This lecture covers the basics.",
      keyTerms: ["alpha", "beta"],
      topicIds: [1],
      recording: null,
      ...overrides,
    };
  }

  it("renders the summary and key terms once loaded", async () => {
    mockedGetMaterial.mockResolvedValue(materialFixture());
    useUiStore.setState({ selection: { type: "material", id: 10 } });
    renderDetailPanel(fixturePayload());

    expect(await screen.findByText("This lecture covers the basics.")).toBeTruthy();
    expect(screen.getByText("alpha")).toBeTruthy();
    expect(screen.getByText("beta")).toBeTruthy();
    expect(mockedGetMaterial).toHaveBeenCalledWith(10);
  });

  it("shows a topic chip with confidence, wired to select that topic", async () => {
    mockedGetMaterial.mockResolvedValue(materialFixture());
    useUiStore.setState({ selection: { type: "material", id: 10 } });
    renderDetailPanel(fixturePayload());

    const chip = await screen.findByText(/Intro.*90%/);
    fireEvent.click(chip);

    expect(useUiStore.getState().selection).toEqual({ type: "topic", id: 1 });
  });

  it("omits the Open in Brightspace link when there is no http sourceUrl", async () => {
    mockedGetMaterial.mockResolvedValue(materialFixture());
    useUiStore.setState({ selection: { type: "material", id: 10 } });
    renderDetailPanel(fixturePayload());

    await screen.findByText("This lecture covers the basics.");
    expect(screen.queryByText(/Open in Brightspace/)).toBeNull();
  });

  it("shows an Open in Brightspace link when sourceUrl starts with http", async () => {
    mockedGetMaterial.mockResolvedValue({ ...materialFixture(), sourceUrl: "https://example.d2l.com/x" });
    useUiStore.setState({ selection: { type: "material", id: 10 } });
    renderDetailPanel(fixturePayload());

    const link = (await screen.findByText(/Open in Brightspace/)).closest("a");
    expect(link?.getAttribute("href")).toBe("https://example.d2l.com/x");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toContain("noopener");
  });

  it("omits the recording section when the material has no recording", async () => {
    mockedGetMaterial.mockResolvedValue(materialFixture());
    useUiStore.setState({ selection: { type: "material", id: 10 } });
    renderDetailPanel(fixturePayload());

    await screen.findByText("This lecture covers the basics.");
    expect(screen.queryByText(/Open recording/)).toBeNull();
  });

  it("shows an Open recording link when the material has a recording", async () => {
    mockedGetMaterial.mockResolvedValue(
      materialFixture({
        recording: { url: "https://mediasite.example.edu/watch/1", status: "done", transcriptMaterialId: null },
      }),
    );
    useUiStore.setState({ selection: { type: "material", id: 10 } });
    renderDetailPanel(fixturePayload());

    const link = (await screen.findByText(/Open recording/)).closest("a");
    expect(link?.getAttribute("href")).toBe("https://mediasite.example.edu/watch/1");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toContain("noopener");
  });

  it("shows the Open recording link regardless of the recording's status", async () => {
    mockedGetMaterial.mockResolvedValue(
      materialFixture({
        recording: { url: "https://mediasite.example.edu/watch/1", status: "fetching", transcriptMaterialId: null },
      }),
    );
    useUiStore.setState({ selection: { type: "material", id: 10 } });
    renderDetailPanel(fixturePayload());

    expect(await screen.findByText(/Open recording/)).toBeTruthy();
  });

  it("a source material with transcriptMaterialId shows 'View transcript' and jumps selection to it", async () => {
    mockedGetMaterial.mockResolvedValue(
      materialFixture({
        recording: { url: "https://mediasite.example.edu/watch/1", status: "done", transcriptMaterialId: 55 },
      }),
    );
    useUiStore.setState({ selection: { type: "material", id: 10 } });
    renderDetailPanel(fixturePayload());

    fireEvent.click(await screen.findByText(/View transcript/));

    expect(useUiStore.getState().selection).toEqual({ type: "material", id: 55 });
  });

  it("omits 'View transcript' when the source has no transcriptMaterialId yet", async () => {
    mockedGetMaterial.mockResolvedValue(
      materialFixture({
        recording: { url: "https://mediasite.example.edu/watch/1", status: "fetching", transcriptMaterialId: null },
      }),
    );
    useUiStore.setState({ selection: { type: "material", id: 10 } });
    renderDetailPanel(fixturePayload());

    await screen.findByText(/Open recording/);
    expect(screen.queryByText(/View transcript/)).toBeNull();
  });

  it("a transcript material shows a 'from recording' jump to the source material", async () => {
    mockedGetMaterial.mockResolvedValue(
      materialFixture({
        kind: "transcript",
        recording: { url: "https://mediasite.example.edu/watch/1", status: "done", sourceMaterialId: 42 },
      }),
    );
    useUiStore.setState({ selection: { type: "material", id: 10 } });
    renderDetailPanel(fixturePayload());

    fireEvent.click(await screen.findByText(/from recording/));

    expect(useUiStore.getState().selection).toEqual({ type: "material", id: 42 });
  });
});

describe("DetailPanel: Supplementary section hidden for synthetic topics (M3.5c)", () => {
  function payloadWithSyntheticTopics(): GraphPayload {
    const payload = fixturePayload();
    payload.topics.push(
      { id: UNSORTED_TOPIC_ID, slug: "_unsorted", name: "Unsorted", description: "", orderIndex: 3, materialCount: 0 },
      {
        id: ADMIN_TOPIC_ID,
        slug: "_admin",
        name: "Logistics & admin",
        description: "",
        orderIndex: 4,
        materialCount: 0,
      },
    );
    return payload;
  }

  it("does not render the Supplementary section for the Unsorted topic", async () => {
    useUiStore.setState({ selection: { type: "topic", id: UNSORTED_TOPIC_ID } });
    renderDetailPanel(payloadWithSyntheticTopics());

    expect(await screen.findByText("Unsorted")).toBeTruthy();
    expect(screen.queryByText("Supplementary")).toBeNull();
    expect(mockedGetTopicEnrichment).not.toHaveBeenCalled();
  });

  it("does not render the Supplementary section for the Logistics & admin topic", async () => {
    useUiStore.setState({ selection: { type: "topic", id: ADMIN_TOPIC_ID } });
    renderDetailPanel(payloadWithSyntheticTopics());

    expect(await screen.findByText("Logistics & admin")).toBeTruthy();
    expect(screen.queryByText("Supplementary")).toBeNull();
    expect(mockedGetTopicEnrichment).not.toHaveBeenCalled();
  });

  it("still renders the Supplementary section for a real topic", async () => {
    useUiStore.setState({ selection: { type: "topic", id: 1 } });
    renderDetailPanel(payloadWithSyntheticTopics());

    expect(await screen.findByText(/not searched yet/i)).toBeTruthy();
  });
});
