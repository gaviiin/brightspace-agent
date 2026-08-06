import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { GraphPayload } from "../api/types";
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
