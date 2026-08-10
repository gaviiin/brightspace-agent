import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { GraphPayload } from "../api/types";
import { ADMIN_TOPIC_ID } from "../api/types";
import { useUiStore } from "../state/uiStore";
import { OutlinePanel } from "./OutlinePanel";

function fixturePayload(): GraphPayload {
  return {
    topics: [
      { id: 1, slug: "intro", name: "Intro", description: "Intro topic", orderIndex: 0, materialCount: 1 },
      { id: 2, slug: "advanced", name: "Advanced", description: "Advanced topic", orderIndex: 1, materialCount: 1 },
    ],
    materials: [
      { id: 10, title: "Lecture 1", kind: "document", status: "summarized", maxConfidence: 0.9 },
      { id: 11, title: "Lecture 2", kind: "slides", status: "summarized", maxConfidence: 0.3 },
    ],
    topicEdges: [],
    attachments: [
      { topicId: 1, materialId: 10, confidence: 0.9, rationale: "r1" },
      { topicId: 2, materialId: 11, confidence: 0.3, rationale: "r2" },
    ],
    meta: { taxonomyVersion: 1, orphanCount: 0 },
  };
}

/** M3.5c: real topics + Unsorted + the admin bucket. Mirrors graph/build.py's
 * append order (admin BEFORE Unsorted, so admin's orderIndex is lower) --
 * OutlinePanel is expected to still render admin last regardless. */
function fixturePayloadWithAdmin(): GraphPayload {
  const base = fixturePayload();
  return {
    ...base,
    topics: [
      ...base.topics,
      {
        id: ADMIN_TOPIC_ID,
        slug: "_admin",
        name: "Logistics & admin",
        description: "Grades, scheduling, office hours.",
        orderIndex: 2,
        materialCount: 2,
      },
      {
        id: 0,
        slug: "_unsorted",
        name: "Unsorted",
        description: "Everything else",
        orderIndex: 3,
        materialCount: 1,
      },
    ],
    materials: [
      ...base.materials,
      { id: 20, title: "Grading Policy", kind: "other", status: "summarized", maxConfidence: null },
      { id: 21, title: "Office Hours", kind: "other", status: "summarized", maxConfidence: null },
      { id: 22, title: "Random Link", kind: "link", status: "fetched", maxConfidence: null },
    ],
    attachments: [
      ...base.attachments,
      { topicId: ADMIN_TOPIC_ID, materialId: 20, confidence: null, rationale: null },
      { topicId: ADMIN_TOPIC_ID, materialId: 21, confidence: null, rationale: null },
      { topicId: 0, materialId: 22, confidence: null, rationale: null },
    ],
    meta: { ...base.meta, adminCount: 2 },
  };
}

beforeEach(() => {
  useUiStore.setState({ expandedTopicIds: new Set(), selection: null });
});

afterEach(cleanup);

describe("OutlinePanel", () => {
  it("renders one row per topic, with its material count badge", () => {
    render(<OutlinePanel payload={fixturePayload()} />);
    expect(screen.getByText("Intro")).toBeTruthy();
    expect(screen.getByText("Advanced")).toBeTruthy();
    // materialCount badges (1 each) -- two rows both show "1".
    expect(screen.getAllByText("1")).toHaveLength(2);
    // Collapsed: no material rows rendered.
    expect(screen.queryByText("Lecture 1")).toBeNull();
    expect(screen.queryByText("Lecture 2")).toBeNull();
  });

  it("expanding a topic via the store shows exactly its materials", () => {
    useUiStore.setState({ expandedTopicIds: new Set([1]), selection: null });
    render(<OutlinePanel payload={fixturePayload()} />);
    expect(screen.getByText("Lecture 1")).toBeTruthy();
    expect(screen.queryByText("Lecture 2")).toBeNull();
  });

  it("clicking the chevron toggles expand in the store, without selecting the topic", () => {
    render(<OutlinePanel payload={fixturePayload()} />);
    fireEvent.click(screen.getByRole("button", { name: "Expand Intro" }));
    expect(useUiStore.getState().expandedTopicIds.has(1)).toBe(true);
    expect(useUiStore.getState().selection).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Collapse Intro" }));
    expect(useUiStore.getState().expandedTopicIds.has(1)).toBe(false);
  });

  it("clicking the topic name selects it, without toggling expand", () => {
    render(<OutlinePanel payload={fixturePayload()} />);
    fireEvent.click(screen.getByText("Intro"));
    expect(useUiStore.getState().selection).toEqual({ type: "topic", id: 1 });
    expect(useUiStore.getState().expandedTopicIds.has(1)).toBe(false);
  });

  it("clicking a material row fires selectMaterial (observed directly on the store)", () => {
    useUiStore.setState({ expandedTopicIds: new Set([1]), selection: null });
    render(<OutlinePanel payload={fixturePayload()} />);

    fireEvent.click(screen.getByText("Lecture 1"));

    expect(useUiStore.getState().selection).toEqual({ type: "material", id: 10 });
  });

  it("a low-confidence material shows the low-confidence dot", () => {
    useUiStore.setState({ expandedTopicIds: new Set([2]), selection: null });
    render(<OutlinePanel payload={fixturePayload()} />);
    expect(screen.getByTitle("Low classification confidence")).toBeTruthy();
  });
});

describe("OutlinePanel: Logistics & admin bucket (M3.5c)", () => {
  it("renders the admin bucket last (after Unsorted), muted, with its material count", () => {
    render(<OutlinePanel payload={fixturePayloadWithAdmin()} />);

    const adminLabel = screen.getByText("Logistics & admin");
    const unsortedLabel = screen.getByText("Unsorted");

    // DOM order: admin comes after Unsorted, even though the backend gives
    // admin the lower orderIndex (it's appended before Unsorted in
    // graph/build.py).
    expect(
      unsortedLabel.compareDocumentPosition(adminLabel) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    // Muted like Unsorted's own italic treatment.
    expect(adminLabel.className).toContain("italic");

    const adminRow = adminLabel.closest("li");
    if (!adminRow) throw new Error("no admin row found");
    expect(within(adminRow).getByText("2")).toBeTruthy();
  });

  it("is collapsed by default -- its materials aren't shown until expanded", () => {
    render(<OutlinePanel payload={fixturePayloadWithAdmin()} />);
    expect(screen.queryByText("Grading Policy")).toBeNull();
    expect(screen.queryByText("Office Hours")).toBeNull();
  });

  it("expanding it via the store shows exactly its materials", () => {
    useUiStore.setState({ expandedTopicIds: new Set([ADMIN_TOPIC_ID]), selection: null });
    render(<OutlinePanel payload={fixturePayloadWithAdmin()} />);
    expect(screen.getByText("Grading Policy")).toBeTruthy();
    expect(screen.getByText("Office Hours")).toBeTruthy();
    expect(screen.queryByText("Random Link")).toBeNull();
  });

  it("is absent when the payload doesn't carry it (adminCount 0/absent, old payloads)", () => {
    render(<OutlinePanel payload={fixturePayload()} />);
    expect(screen.queryByText("Logistics & admin")).toBeNull();
  });
});
