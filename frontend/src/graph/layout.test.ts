import { describe, expect, it, vi } from "vitest";

import type { FlowEdge, FlowEdgeRelation, FlowNode, MaterialNodeData, TopicNodeData } from "./transform";
import { layoutFlow, MATERIAL_NODE_SIZE, TOPIC_NODE_SIZE } from "./layout";

function topicNode(id: string): FlowNode {
  return {
    id,
    type: "topic",
    position: { x: 0, y: 0 },
    data: { topic: { id: 1, slug: "t", name: "T", description: "", orderIndex: 0, materialCount: 0 }, expanded: true, selected: false } satisfies TopicNodeData,
  };
}

function materialNode(id: string): FlowNode {
  return {
    id,
    type: "material",
    position: { x: 0, y: 0 },
    data: { material: { id: 1, title: "M", kind: "document", status: "summarized", maxConfidence: null }, selected: false } satisfies MaterialNodeData,
  };
}

function edge(
  id: string,
  source: string,
  target: string,
  relation: FlowEdgeRelation = "attachment",
): FlowEdge {
  return { id, source, target, data: { relation } };
}

describe("layoutFlow", () => {
  it("assigns a finite, non-NaN position to every node", () => {
    const nodes = [topicNode("topic-1"), materialNode("material-10"), materialNode("material-11")];
    const edges = [edge("e1", "topic-1", "material-10"), edge("e2", "topic-1", "material-11")];

    const laidOut = layoutFlow(nodes, edges);

    expect(laidOut).toHaveLength(3);
    for (const node of laidOut) {
      expect(Number.isFinite(node.position.x)).toBe(true);
      expect(Number.isFinite(node.position.y)).toBe(true);
      expect(Number.isNaN(node.position.x)).toBe(false);
      expect(Number.isNaN(node.position.y)).toBe(false);
    }
  });

  it("ranks a material strictly below its parent topic in TB", () => {
    const nodes = [topicNode("topic-1"), materialNode("material-10")];
    const edges = [edge("e1", "topic-1", "material-10")];

    const laidOut = layoutFlow(nodes, edges);
    const topic = laidOut.find((n) => n.id === "topic-1")!;
    const material = laidOut.find((n) => n.id === "material-10")!;

    expect(topic.position.y).not.toBe(material.position.y);
    expect(material.position.y).toBeGreaterThan(topic.position.y);
  });

  it("uses the documented fixed sizes for topic vs material nodes", () => {
    expect(TOPIC_NODE_SIZE).toEqual({ width: 220, height: 72 });
    expect(MATERIAL_NODE_SIZE).toEqual({ width: 180, height: 48 });
  });

  it("defaults a node absent from the dagre graph to (0,0) and warns, instead of NaN", () => {
    // A node whose id collides with nothing dagre laid out (simulated by
    // passing an edge that references a node not present in `nodes` --
    // layoutFlow must not crash, and must not hand back NaN for anyone).
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const nodes = [topicNode("topic-1")];
    const edges = [edge("e1", "topic-1", "material-does-not-exist")];

    const laidOut = layoutFlow(nodes, edges);

    expect(laidOut).toHaveLength(1);
    expect(Number.isNaN(laidOut[0].position.x)).toBe(false);
    expect(Number.isNaN(laidOut[0].position.y)).toBe(false);

    warnSpy.mockRestore();
  });

  it("never returns NaN even for an empty graph", () => {
    expect(layoutFlow([], [])).toEqual([]);
  });
});
