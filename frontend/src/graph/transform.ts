// Pure transform: GraphPayload + UI state -> React Flow nodes/edges. No
// React imports (only type-only imports from @xyflow/react, which are
// erased at build time) -- this module is unit-testable without a DOM.
//
// Rules (see the Task 10 brief):
// - Topic nodes are always present, one per entry in payload.topics
//   (including the synthetic Unsorted topic, id 0, when the backend has
//   included it -- that inclusion decision is the backend's, not ours).
// - Material nodes appear iff at least one of their attached topics is
//   expanded. One node per material, never per attachment.
// - Attachment edges connect a material to EACH expanded topic it's
//   attached to. Directed topic -> material for layout.ts's benefit: dagre
//   ranks a TB edge's target below its source, and topics are the layout's
//   parents/hubs with materials fanning out beneath them. Attachment edges
//   render with no arrowhead (see edges.ts), so this direction carries no
//   visual meaning for the user -- it only steers the layout.
// - Topic edges (prerequisite/related) are always visible, directed
//   fromTopicId -> toTopicId exactly as the wire type names them.
// - Every edge carries its kind in `data.relation` (see FlowEdgeRelation),
//   never in React Flow's `edge.type`.
import type { Edge, Node } from "@xyflow/react";

import type { GraphAttachment, GraphMaterial, GraphPayload, GraphTopic } from "../api/types";
import { ADMIN_TOPIC_ID } from "../api/types";

export type Selection = { type: "topic"; id: number } | { type: "material"; id: number } | null;

export interface TopicNodeData extends Record<string, unknown> {
  topic: GraphTopic;
  expanded: boolean;
  selected: boolean;
}

export interface MaterialNodeData extends Record<string, unknown> {
  material: GraphMaterial;
  selected: boolean;
}

export type TopicFlowNode = Node<TopicNodeData, "topic">;
export type MaterialFlowNode = Node<MaterialNodeData, "material">;
export type FlowNode = TopicFlowNode | MaterialFlowNode;

/** What an edge MEANS. Deliberately carried in `edge.data.relation`, not in
 * React Flow's own `edge.type`: `type` names a registered *renderer*, and
 * React Flow falls back to the default renderer (logging error 011 as it
 * goes) for anything it doesn't recognize -- so setting it to
 * "prerequisite" bought a console warning per edge and changed nothing
 * about how the edge drew. The entire visual difference between relations
 * is stroke/marker styling, which edges.ts applies from this
 * discriminator. */
export type FlowEdgeRelation = "prerequisite" | "related" | "attachment";

export interface FlowEdgeData extends Record<string, unknown> {
  relation: FlowEdgeRelation;
}

export type FlowEdge = Edge<FlowEdgeData>;

export function topicNodeId(topicId: number): string {
  return `topic-${topicId}`;
}

export function materialNodeId(materialId: number): string {
  return `material-${materialId}`;
}

export function toFlow(
  payload: GraphPayload,
  expandedTopicIds: Set<number>,
  selection: Selection,
): { nodes: FlowNode[]; edges: FlowEdge[] } {
  // M3.5c: the Logistics & admin synthetic topic (ADMIN_TOPIC_ID, -1) is
  // outline-only (see OutlinePanel) -- excluded here from BOTH the topic
  // node list and the attachments considered for material nodes/edges, so
  // it can never surface on the graph canvas even indirectly: without the
  // attachment filter, a material filed only under admin would still gain
  // a node (and a dangling edge sourced from a topic node that was never
  // created) the moment -1 ended up in expandedTopicIds -- which
  // OutlinePanel's own, independent expand toggle for this bucket can do,
  // since it shares the same `expandedTopicIds` set as the canvas.
  const topics = [...payload.topics]
    .filter((topic) => topic.id !== ADMIN_TOPIC_ID)
    .sort((a, b) => a.orderIndex - b.orderIndex);
  const materials = [...payload.materials].sort(
    (a, b) => a.title.localeCompare(b.title) || a.id - b.id,
  );

  const attachmentsByMaterial = new Map<number, GraphAttachment[]>();
  for (const attachment of payload.attachments) {
    if (attachment.topicId === ADMIN_TOPIC_ID) continue;
    const list = attachmentsByMaterial.get(attachment.materialId);
    if (list) {
      list.push(attachment);
    } else {
      attachmentsByMaterial.set(attachment.materialId, [attachment]);
    }
  }

  const nodes: FlowNode[] = [];
  const edges: FlowEdge[] = [];

  for (const topic of topics) {
    nodes.push({
      id: topicNodeId(topic.id),
      type: "topic",
      position: { x: 0, y: 0 },
      data: {
        topic,
        expanded: expandedTopicIds.has(topic.id),
        selected: selection?.type === "topic" && selection.id === topic.id,
      },
    });
  }

  for (const material of materials) {
    const attachments = (attachmentsByMaterial.get(material.id) ?? [])
      .filter((attachment) => expandedTopicIds.has(attachment.topicId))
      .sort((a, b) => a.topicId - b.topicId);
    if (attachments.length === 0) continue;

    nodes.push({
      id: materialNodeId(material.id),
      type: "material",
      position: { x: 0, y: 0 },
      data: {
        material,
        selected: selection?.type === "material" && selection.id === material.id,
      },
    });

    for (const attachment of attachments) {
      edges.push({
        id: `att-${attachment.topicId}-${material.id}`,
        data: { relation: "attachment" },
        source: topicNodeId(attachment.topicId),
        target: materialNodeId(material.id),
      });
    }
  }

  for (const topicEdge of payload.topicEdges) {
    edges.push({
      id: `topic-edge-${topicEdge.fromTopicId}-${topicEdge.toTopicId}-${topicEdge.relation}`,
      data: { relation: topicEdge.relation },
      source: topicNodeId(topicEdge.fromTopicId),
      target: topicNodeId(topicEdge.toTopicId),
    });
  }

  return { nodes, edges };
}
