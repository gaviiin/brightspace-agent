// Pure dagre wrapper: nodes + edges -> nodes with computed positions. No
// React imports (only type-only imports from @xyflow/react).
import { graphlib, layout as dagreLayout } from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";

export const TOPIC_NODE_SIZE = { width: 220, height: 72 };
export const MATERIAL_NODE_SIZE = { width: 180, height: 48 };

function sizeFor(node: Node): { width: number; height: number } {
  return node.type === "material" ? MATERIAL_NODE_SIZE : TOPIC_NODE_SIZE;
}

export function layoutFlow<N extends Node = Node, E extends Edge = Edge>(nodes: N[], edges: E[]): N[] {
  const graph = new graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir: "TB", ranksep: 80, nodesep: 40 });

  for (const node of nodes) {
    const { width, height } = sizeFor(node);
    graph.setNode(node.id, { width, height });
  }
  for (const edge of edges) {
    if (graph.hasNode(edge.source) && graph.hasNode(edge.target)) {
      graph.setEdge(edge.source, edge.target);
    }
  }

  dagreLayout(graph);

  return nodes.map((node) => {
    const dagreNode = graph.node(node.id) as { x: number; y: number } | undefined;
    if (!dagreNode || Number.isNaN(dagreNode.x) || Number.isNaN(dagreNode.y)) {
      console.warn(`layoutFlow: node "${node.id}" has no dagre position; defaulting to (0, 0)`);
      return { ...node, position: { x: 0, y: 0 } };
    }
    const { width, height } = sizeFor(node);
    // dagre's (x, y) is the node's CENTER; React Flow's `position` is the
    // node's top-left corner.
    return {
      ...node,
      position: { x: dagreNode.x - width / 2, y: dagreNode.y - height / 2 },
    };
  });
}
