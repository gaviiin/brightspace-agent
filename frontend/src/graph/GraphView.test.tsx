import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { GraphPayload } from "../api/types";
import { useUiStore } from "../state/uiStore";
import { GraphView } from "./GraphView";

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
    topicEdges: [{ fromTopicId: 1, toTopicId: 2, relation: "prerequisite" }],
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

describe("GraphView", () => {
  it("renders one node per topic when collapsed", () => {
    const { container } = render(<GraphView payload={fixturePayload()} />);
    const nodes = container.querySelectorAll(".react-flow__node");
    expect(nodes).toHaveLength(2);
    const topicNodes = container.querySelectorAll(".react-flow__node-topic");
    expect(topicNodes).toHaveLength(2);
  });

  it("renders topic + material nodes once a topic is expanded", () => {
    useUiStore.setState({ expandedTopicIds: new Set([1]), selection: null });
    const { container } = render(<GraphView payload={fixturePayload()} />);
    const nodes = container.querySelectorAll(".react-flow__node");
    // 2 topics + 1 material (topic 1's Lecture 1; topic 2 is collapsed).
    expect(nodes).toHaveLength(3);
    const materialNodes = container.querySelectorAll(".react-flow__node-material");
    expect(materialNodes).toHaveLength(1);
  });
});
