import { describe, expect, it } from "vitest";

import type { GraphPayload } from "../api/types";
import { ADMIN_TOPIC_ID } from "../api/types";
import { styleEdge } from "./edges";
import { materialNodeId, toFlow, topicNodeId } from "./transform";

// ---------------------------------------------------------------------------
// Fixture: two topics (a prerequisite of b), one material filed under each,
// plus a third material filed under BOTH topics (for the "shared material"
// case) and an Unsorted (id 0) topic with one orphaned material.
// ---------------------------------------------------------------------------

function fixturePayload(): GraphPayload {
  return {
    topics: [
      { id: 1, slug: "intro", name: "Intro", description: "Intro topic", orderIndex: 0, materialCount: 2 },
      { id: 2, slug: "advanced", name: "Advanced", description: "Advanced topic", orderIndex: 1, materialCount: 2 },
      { id: 0, slug: "_unsorted", name: "Unsorted", description: "Everything else", orderIndex: 2, materialCount: 1 },
    ],
    materials: [
      { id: 10, title: "Lecture 1", kind: "document", status: "summarized", maxConfidence: 0.9 },
      { id: 11, title: "Lecture 2", kind: "slides", status: "summarized", maxConfidence: 0.3 },
      { id: 12, title: "Shared Reading", kind: "document", status: "summarized", maxConfidence: 0.7 },
      { id: 13, title: "Orphan Link", kind: "link", status: "fetched", maxConfidence: null },
    ],
    topicEdges: [{ fromTopicId: 1, toTopicId: 2, relation: "prerequisite" }],
    attachments: [
      { topicId: 1, materialId: 10, confidence: 0.9, rationale: "r1" },
      { topicId: 2, materialId: 11, confidence: 0.3, rationale: "r2" },
      { topicId: 1, materialId: 12, confidence: 0.6, rationale: "r3a" },
      { topicId: 2, materialId: 12, confidence: 0.5, rationale: "r3b" },
      { topicId: 0, materialId: 13, confidence: null, rationale: null },
    ],
    meta: { taxonomyVersion: 1, orphanCount: 1 },
  };
}

describe("toFlow: collapsed graph", () => {
  it("shows only topic nodes (no material nodes), with topic edges present", () => {
    const payload = fixturePayload();
    const { nodes, edges } = toFlow(payload, new Set(), null);

    const topicNodes = nodes.filter((n) => n.type === "topic");
    const materialNodes = nodes.filter((n) => n.type === "material");
    expect(topicNodes).toHaveLength(3); // topics 1, 2, and Unsorted (0)
    expect(materialNodes).toHaveLength(0);

    const topicEdges = edges.filter(
      (e) => e.data?.relation === "prerequisite" || e.data?.relation === "related",
    );
    const attachmentEdges = edges.filter((e) => e.data?.relation === "attachment");
    expect(topicEdges).toHaveLength(1);
    expect(topicEdges[0]).toMatchObject({
      source: topicNodeId(1),
      target: topicNodeId(2),
      data: { relation: "prerequisite" },
    });
    // React Flow's own `type` is left unset on purpose: it names a
    // registered renderer, and an unregistered value warns on every render
    // (see FlowEdgeRelation). The relation lives in `data`.
    expect(topicEdges[0].type).toBeUndefined();
    expect(attachmentEdges).toHaveLength(0);
  });
});

describe("toFlow: expanding a single topic", () => {
  it("shows exactly that topic's materials, with one attachment edge each", () => {
    const payload = fixturePayload();
    const { nodes, edges } = toFlow(payload, new Set([1]), null);

    const materialNodeIds = nodes.filter((n) => n.type === "material").map((n) => n.id);
    // Topic 1's materials are 10 and 12 (shared with topic 2, but topic 2 is
    // not expanded here).
    expect(materialNodeIds.sort()).toEqual([materialNodeId(10), materialNodeId(12)].sort());

    const attachmentEdges = edges.filter((e) => e.data?.relation === "attachment");
    expect(attachmentEdges).toHaveLength(2);
    expect(attachmentEdges.map((e) => e.id).sort()).toEqual(
      ["att-1-10", "att-1-12"].sort(),
    );
    // Directed topic -> material (not material -> topic): dagre (TB) ranks
    // an edge's target below its source, and topics are the layout's
    // parent/hub with materials fanning out beneath them (see
    // layout.test.ts and transform.ts's module comment). Attachment edges
    // render with no arrowhead, so this direction has no visual meaning.
    for (const edge of attachmentEdges) {
      expect(edge.source).toBe(topicNodeId(1));
      expect(edge.target).toBe(materialNodeId(edge.id === "att-1-10" ? 10 : 12));
    }
  });
});

describe("toFlow: a material attached to two expanded topics", () => {
  it("appears exactly once, with two attachment edges", () => {
    const payload = fixturePayload();
    const { nodes, edges } = toFlow(payload, new Set([1, 2]), null);

    const sharedNodes = nodes.filter((n) => n.id === materialNodeId(12));
    expect(sharedNodes).toHaveLength(1);

    const sharedEdges = edges.filter(
      (e) => e.data?.relation === "attachment" && e.target === materialNodeId(12),
    );
    expect(sharedEdges).toHaveLength(2);
    expect(sharedEdges.map((e) => e.source).sort()).toEqual([topicNodeId(1), topicNodeId(2)].sort());
  });
});

describe("toFlow: Unsorted topic", () => {
  it("is included when the payload carries it (it has attachments)", () => {
    const payload = fixturePayload();
    const { nodes } = toFlow(payload, new Set(), null);
    expect(nodes.some((n) => n.id === topicNodeId(0))).toBe(true);
  });

  it("is absent when the payload doesn't carry it (no orphans)", () => {
    const payload = fixturePayload();
    payload.topics = payload.topics.filter((t) => t.id !== 0);
    payload.attachments = payload.attachments.filter((a) => a.topicId !== 0);
    payload.materials = payload.materials.filter((m) => m.id !== 13);

    const { nodes } = toFlow(payload, new Set(), null);
    expect(nodes.some((n) => n.id === topicNodeId(0))).toBe(false);
  });
});

describe("toFlow: deterministic ordering", () => {
  it("orders topic nodes by orderIndex and material nodes by title, regardless of input array order", () => {
    const payload = fixturePayload();
    // Shuffle the input arrays.
    const shuffled: GraphPayload = {
      ...payload,
      topics: [...payload.topics].reverse(),
      materials: [...payload.materials].reverse(),
    };

    const expanded = new Set([0, 1, 2]);
    const a = toFlow(payload, expanded, null);
    const b = toFlow(shuffled, expanded, null);

    expect(a.nodes.map((n) => n.id)).toEqual(b.nodes.map((n) => n.id));

    const topicIds = a.nodes.filter((n) => n.type === "topic").map((n) => n.id);
    expect(topicIds).toEqual([topicNodeId(1), topicNodeId(2), topicNodeId(0)]); // orderIndex 0,1,2

    const materialTitlesInOrder = a.nodes
      .filter((n) => n.type === "material")
      .map((n) => (n.data as { material: { title: string } }).material.title);
    const sortedTitles = [...materialTitlesInOrder].sort((x, y) => x.localeCompare(y));
    expect(materialTitlesInOrder).toEqual(sortedTitles);
  });
});

describe("toFlow + styleEdge: the relation reaches the rendered edge", () => {
  it("gives every edge a registered (i.e. unset) type and a relation-appropriate style", () => {
    // The move of the discriminator from `edge.type` (which named a React
    // Flow renderer that didn't exist -- error 011 on every render, and the
    // default edge drawn anyway) into `edge.data.relation` is only safe if
    // styling still lands, since styling is the ONLY thing that visually
    // distinguishes the three relations. GraphView composes exactly these
    // two functions, and can't be asserted on directly: React Flow needs
    // measured node dimensions before it renders any edge at all, and
    // JSDOM never supplies them.
    const payload = fixturePayload();
    const { edges } = toFlow(payload, new Set([1, 2]), null);
    const styled = edges.map(styleEdge);

    for (const edge of styled) {
      expect(edge.type).toBeUndefined();
    }

    const byRelation = (relation: string) => styled.filter((e) => e.data?.relation === relation);
    expect(byRelation("prerequisite")[0].style).toMatchObject({ stroke: "var(--bsa-edge-strong)" });
    expect(byRelation("prerequisite")[0].markerEnd).toBeDefined();
    for (const attachment of byRelation("attachment")) {
      expect(attachment.style).toMatchObject({ stroke: "var(--bsa-edge-attach)" });
      expect(attachment.markerEnd).toBeUndefined(); // no arrowhead: direction is layout-only
    }
  });
});

describe("toFlow: admin topic (M3.5c)", () => {
  it("excludes the admin synthetic topic node from the graph canvas entirely", () => {
    const payload = fixturePayload();
    payload.topics.push({
      id: ADMIN_TOPIC_ID,
      slug: "_admin",
      name: "Logistics & admin",
      description: "Grades, scheduling, etc.",
      orderIndex: 3,
      materialCount: 1,
    });
    payload.materials.push({ id: 14, title: "Grading Policy", kind: "other", status: "summarized", maxConfidence: null });
    payload.attachments.push({ topicId: ADMIN_TOPIC_ID, materialId: 14, confidence: null, rationale: null });

    const { nodes } = toFlow(payload, new Set(), null);

    expect(nodes.some((n) => n.id === topicNodeId(ADMIN_TOPIC_ID))).toBe(false);
    // The other topics (including Unsorted) are unaffected.
    expect(nodes.some((n) => n.id === topicNodeId(1))).toBe(true);
    expect(nodes.some((n) => n.id === topicNodeId(0))).toBe(true);
  });

  it("never renders the admin material node, even if -1 ends up in expandedTopicIds", () => {
    // OutlinePanel drives the same expandedTopicIds set, and its admin row
    // is independently expandable -- this guards against a dangling
    // material node whose "source" topic node doesn't exist on the canvas.
    const payload = fixturePayload();
    payload.topics.push({
      id: ADMIN_TOPIC_ID,
      slug: "_admin",
      name: "Logistics & admin",
      description: "Grades, scheduling, etc.",
      orderIndex: 3,
      materialCount: 1,
    });
    payload.materials.push({ id: 14, title: "Grading Policy", kind: "other", status: "summarized", maxConfidence: null });
    payload.attachments.push({ topicId: ADMIN_TOPIC_ID, materialId: 14, confidence: null, rationale: null });

    const { nodes, edges } = toFlow(payload, new Set([ADMIN_TOPIC_ID]), null);

    expect(nodes.some((n) => n.id === materialNodeId(14))).toBe(false);
    expect(edges.some((e) => e.target === materialNodeId(14))).toBe(false);
  });
});

describe("toFlow: selection", () => {
  it("marks the selected topic and material in node data", () => {
    const payload = fixturePayload();
    const { nodes } = toFlow(payload, new Set([1]), { type: "topic", id: 1 });
    const topic1 = nodes.find((n) => n.id === topicNodeId(1));
    expect((topic1?.data as { selected: boolean }).selected).toBe(true);
    const topic2 = nodes.find((n) => n.id === topicNodeId(2));
    expect((topic2?.data as { selected: boolean }).selected).toBe(false);

    const { nodes: nodes2 } = toFlow(payload, new Set([1]), { type: "material", id: 10 });
    const material10 = nodes2.find((n) => n.id === materialNodeId(10));
    expect((material10?.data as { selected: boolean }).selected).toBe(true);
  });

  it("marks the expanded flag on topic node data", () => {
    const payload = fixturePayload();
    const { nodes } = toFlow(payload, new Set([1]), null);
    const topic1 = nodes.find((n) => n.id === topicNodeId(1));
    const topic2 = nodes.find((n) => n.id === topicNodeId(2));
    expect((topic1?.data as { expanded: boolean }).expanded).toBe(true);
    expect((topic2?.data as { expanded: boolean }).expanded).toBe(false);
  });
});
