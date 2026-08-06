import { describe, expect, it } from "vitest";

import type { GraphPayload } from "../api/types";
import {
  addEdge,
  addTopic,
  deleteTopic,
  estimateReclassifyCount,
  initDraft,
  isStructural,
  mergeSelectedInto,
  removeEdge,
  rename,
  setDescription,
  toggleMergeSelect,
  toRequest,
  unmerge,
} from "./taxonomyDraft";

function fixturePayload(): GraphPayload {
  return {
    topics: [
      { id: 1, slug: "intro", name: "Intro", description: "Intro topic", orderIndex: 0, materialCount: 2 },
      { id: 2, slug: "advanced", name: "Advanced", description: "Advanced topic", orderIndex: 1, materialCount: 3 },
      { id: 3, slug: "extra", name: "Extra", description: "Extra topic", orderIndex: 2, materialCount: 1 },
      // The synthetic "Unsorted" topic (id 0) must never appear in the draft.
      { id: 0, slug: "_unsorted", name: "Unsorted", description: "Unfiled.", orderIndex: 3, materialCount: 5 },
    ],
    materials: [],
    topicEdges: [{ fromTopicId: 1, toTopicId: 2, relation: "prerequisite" }],
    attachments: [],
    meta: { taxonomyVersion: 3, orphanCount: 5 },
  };
}

function keyFor(draft: ReturnType<typeof initDraft>, id: number): string {
  const topic = draft.topics.find((t) => t.id === id);
  if (!topic) throw new Error(`no draft topic with id ${id}`);
  return topic.key;
}

describe("initDraft", () => {
  it("copies topics (ordered) and edges, excluding the synthetic Unsorted topic", () => {
    const draft = initDraft(fixturePayload());
    expect(draft.topics.map((t) => t.name)).toEqual(["Intro", "Advanced", "Extra"]);
    expect(draft.topics.every((t) => t.id !== 0)).toBe(true);
    expect(draft.edges).toHaveLength(1);
    expect(draft.edges[0].relation).toBe("prerequisite");
    expect(draft.mergeSelection.size).toBe(0);
  });

  it("does not mutate the source payload (copy on init)", () => {
    const payload = fixturePayload();
    const frozenTopics = JSON.stringify(payload.topics);
    const draft = initDraft(payload);
    rename(draft, draft.topics[0].key, "Changed");
    expect(JSON.stringify(payload.topics)).toBe(frozenTopics);
  });

  it("isStructural is false immediately after init (no edits yet)", () => {
    const draft = initDraft(fixturePayload());
    expect(isStructural(draft)).toBe(false);
  });
});

describe("rename / setDescription", () => {
  it("rename updates only the target topic's name", () => {
    const draft = initDraft(fixturePayload());
    const introKey = keyFor(draft, 1);
    const next = rename(draft, introKey, "Introduction");
    expect(next.topics.find((t) => t.key === introKey)?.name).toBe("Introduction");
    expect(next.topics.find((t) => t.id === 2)?.name).toBe("Advanced"); // untouched
    expect(next).not.toBe(draft); // pure -- returns a new draft
  });

  it("setDescription updates only the target topic's description", () => {
    const draft = initDraft(fixturePayload());
    const introKey = keyFor(draft, 1);
    const next = setDescription(draft, introKey, "New description.");
    expect(next.topics.find((t) => t.key === introKey)?.description).toBe("New description.");
  });

  it("rename/setDescription alone never trip isStructural", () => {
    let draft = initDraft(fixturePayload());
    draft = rename(draft, keyFor(draft, 1), "Introduction Basics");
    draft = setDescription(draft, keyFor(draft, 2), "Rewritten.");
    expect(isStructural(draft)).toBe(false);
  });
});

describe("addTopic", () => {
  it("appends a new topic with id null and a fresh unique key", () => {
    const draft = initDraft(fixturePayload());
    const next = addTopic(draft);
    expect(next.topics).toHaveLength(4);
    const added = next.topics[3];
    expect(added.id).toBeNull();
    expect(added.mergedFromTopicIds).toEqual([]);
    expect(new Set(next.topics.map((t) => t.key)).size).toBe(4); // all keys unique
  });

  it("makes the draft structural", () => {
    const draft = initDraft(fixturePayload());
    expect(isStructural(addTopic(draft))).toBe(true);
  });
});

describe("deleteTopic", () => {
  it("removes the topic from the visible list", () => {
    const draft = initDraft(fixturePayload());
    const extraKey = keyFor(draft, 3);
    const next = deleteTopic(draft, extraKey);
    expect(next.topics.find((t) => t.key === extraKey)).toBeUndefined();
    expect(next.topics).toHaveLength(2);
  });

  it("drops edges that referenced the deleted topic", () => {
    const draft = initDraft(fixturePayload());
    const introKey = keyFor(draft, 1);
    const next = deleteTopic(draft, introKey);
    expect(next.edges).toHaveLength(0); // the only edge touched topic 1
  });

  it("makes the draft structural", () => {
    const draft = initDraft(fixturePayload());
    expect(isStructural(deleteTopic(draft, keyFor(draft, 3)))).toBe(true);
  });

  it("deleting a merge-target releases (restores) the topics merged into it", () => {
    let draft = initDraft(fixturePayload());
    const introKey = keyFor(draft, 1);
    const advancedKey = keyFor(draft, 2);
    draft = toggleMergeSelect(draft, introKey);
    draft = toggleMergeSelect(draft, advancedKey);
    draft = mergeSelectedInto(draft, advancedKey);
    expect(draft.topics.find((t) => t.key === introKey)).toBeUndefined(); // merged away

    const next = deleteTopic(draft, advancedKey);
    expect(next.topics.find((t) => t.key === advancedKey)).toBeUndefined(); // target deleted
    expect(next.topics.some((t) => t.id === 1)).toBe(true); // intro released back
  });
});

describe("toggleMergeSelect / mergeSelectedInto / unmerge", () => {
  it("toggles a key into and out of the merge selection", () => {
    const draft = initDraft(fixturePayload());
    const key = keyFor(draft, 1);
    const selected = toggleMergeSelect(draft, key);
    expect(selected.mergeSelection.has(key)).toBe(true);
    const deselected = toggleMergeSelect(selected, key);
    expect(deselected.mergeSelection.has(key)).toBe(false);
  });

  it("merges the other selected topics into the target, folding their ids into mergedFromTopicIds", () => {
    let draft = initDraft(fixturePayload());
    const introKey = keyFor(draft, 1);
    const advancedKey = keyFor(draft, 2);
    const extraKey = keyFor(draft, 3);
    draft = toggleMergeSelect(draft, introKey);
    draft = toggleMergeSelect(draft, advancedKey);
    draft = toggleMergeSelect(draft, extraKey);

    const next = mergeSelectedInto(draft, extraKey);

    expect(next.topics).toHaveLength(1);
    const target = next.topics[0];
    expect(target.key).toBe(extraKey);
    expect(target.mergedFromTopicIds.sort()).toEqual([1, 2]);
    expect(next.mergeSelection.size).toBe(0); // selection cleared after merge
  });

  it("sums merged-away material counts into the target", () => {
    let draft = initDraft(fixturePayload());
    const introKey = keyFor(draft, 1); // materialCount 2
    const advancedKey = keyFor(draft, 2); // materialCount 3
    draft = toggleMergeSelect(draft, introKey);
    draft = toggleMergeSelect(draft, advancedKey);
    const next = mergeSelectedInto(draft, advancedKey);
    expect(next.topics[0].materialCount).toBe(5);
  });

  it("drops edges touching a merged-away source topic", () => {
    let draft = initDraft(fixturePayload());
    const introKey = keyFor(draft, 1);
    const advancedKey = keyFor(draft, 2);
    draft = toggleMergeSelect(draft, introKey);
    draft = toggleMergeSelect(draft, advancedKey);
    const next = mergeSelectedInto(draft, advancedKey);
    expect(next.edges).toHaveLength(0);
  });

  it("a merge makes the draft structural", () => {
    let draft = initDraft(fixturePayload());
    draft = toggleMergeSelect(draft, keyFor(draft, 1));
    draft = toggleMergeSelect(draft, keyFor(draft, 2));
    expect(isStructural(mergeSelectedInto(draft, keyFor(draft, 2)))).toBe(true);
  });

  it("unmerge restores the merged-away topics and clears mergedFromTopicIds", () => {
    let draft = initDraft(fixturePayload());
    const introKey = keyFor(draft, 1);
    const advancedKey = keyFor(draft, 2);
    draft = toggleMergeSelect(draft, introKey);
    draft = toggleMergeSelect(draft, advancedKey);
    draft = mergeSelectedInto(draft, advancedKey);

    const restored = unmerge(draft, advancedKey);
    expect(restored.topics).toHaveLength(3);
    expect(restored.topics.find((t) => t.id === 1)?.name).toBe("Intro");
    expect(restored.topics.find((t) => t.key === advancedKey)?.mergedFromTopicIds).toEqual([]);
    expect(restored.topics.find((t) => t.key === advancedKey)?.materialCount).toBe(3); // back to original
  });

  it("unmerging brings the draft back to non-structural when nothing else changed", () => {
    // Merge "extra" (3, source) into "advanced" (2, target) -- neither is
    // an endpoint the fixture's one edge (intro -> advanced) touches as a
    // SOURCE, so this merge/unmerge round-trip doesn't touch any edges and
    // "nothing else changed" is actually true. (Merging away an edge's own
    // endpoint is a separate, documented case -- see the "drops edges
    // touching a merged-away source topic" test above; unmerge restores
    // topics, not edges dropped that way.)
    let draft = initDraft(fixturePayload());
    draft = toggleMergeSelect(draft, keyFor(draft, 3));
    draft = toggleMergeSelect(draft, keyFor(draft, 2));
    draft = mergeSelectedInto(draft, keyFor(draft, 2));
    expect(isStructural(draft)).toBe(true);

    const restored = unmerge(draft, keyFor(draft, 2));
    expect(isStructural(restored)).toBe(false);
  });

  it("merging a brand-new (unsaved) topic into another simply discards it (no id to preserve)", () => {
    let draft = initDraft(fixturePayload());
    draft = addTopic(draft);
    const newKey = draft.topics[3].key;
    const advancedKey = keyFor(draft, 2);
    draft = toggleMergeSelect(draft, newKey);
    draft = toggleMergeSelect(draft, advancedKey);

    const next = mergeSelectedInto(draft, advancedKey);
    expect(next.topics.find((t) => t.key === newKey)).toBeUndefined();
    expect(next.topics.find((t) => t.key === advancedKey)?.mergedFromTopicIds).toEqual([]);
  });
});

describe("addEdge / removeEdge", () => {
  it("adds a new edge between two topics", () => {
    const draft = initDraft(fixturePayload());
    const introKey = keyFor(draft, 1);
    const extraKey = keyFor(draft, 3);
    const next = addEdge(draft, introKey, extraKey, "related");
    expect(next.edges).toHaveLength(2);
    expect(next.edges[1]).toMatchObject({ fromKey: introKey, toKey: extraKey, relation: "related" });
  });

  it("refuses a self-loop edge (no-op)", () => {
    const draft = initDraft(fixturePayload());
    const introKey = keyFor(draft, 1);
    const next = addEdge(draft, introKey, introKey, "related");
    expect(next.edges).toHaveLength(1); // unchanged
  });

  it("removeEdge drops exactly the targeted edge", () => {
    let draft = initDraft(fixturePayload());
    const introKey = keyFor(draft, 1);
    const extraKey = keyFor(draft, 3);
    draft = addEdge(draft, introKey, extraKey, "related");
    expect(draft.edges).toHaveLength(2);

    const next = removeEdge(draft, draft.edges[0].key);
    expect(next.edges).toHaveLength(1);
    expect(next.edges[0].relation).toBe("related");
  });

  it("adding, removing, or changing an edge trips isStructural even with unchanged topic wording", () => {
    const draft = initDraft(fixturePayload());
    const withNewEdge = addEdge(draft, keyFor(draft, 1), keyFor(draft, 3), "related");
    expect(isStructural(withNewEdge)).toBe(true);

    const withRemovedEdge = removeEdge(draft, draft.edges[0].key);
    expect(isStructural(withRemovedEdge)).toBe(true);
  });

  it("re-adding the exact same edge set the taxonomy already has is not structural", () => {
    let draft = initDraft(fixturePayload());
    const original = draft.edges[0];
    draft = removeEdge(draft, original.key);
    expect(isStructural(draft)).toBe(true);
    draft = addEdge(draft, original.fromKey, original.toKey, original.relation);
    expect(isStructural(draft)).toBe(false);
  });
});

describe("isStructural", () => {
  it("true when the id set changes via delete, even with no other edits", () => {
    const draft = initDraft(fixturePayload());
    expect(isStructural(deleteTopic(draft, keyFor(draft, 3)))).toBe(true);
  });

  it("true when a new topic is added", () => {
    const draft = initDraft(fixturePayload());
    expect(isStructural(addTopic(draft))).toBe(true);
  });

  it("true when topics merge", () => {
    let draft = initDraft(fixturePayload());
    draft = toggleMergeSelect(draft, keyFor(draft, 1));
    draft = toggleMergeSelect(draft, keyFor(draft, 2));
    expect(isStructural(mergeSelectedInto(draft, keyFor(draft, 2)))).toBe(true);
  });

  it("true on an edge-only change", () => {
    const draft = initDraft(fixturePayload());
    expect(isStructural(removeEdge(draft, draft.edges[0].key))).toBe(true);
  });

  it("false for a rename-only edit", () => {
    const draft = initDraft(fixturePayload());
    expect(isStructural(rename(draft, keyFor(draft, 1), "Introduction"))).toBe(false);
  });
});

describe("toRequest", () => {
  it("emits topics/edges with indexes matching the current draft order", () => {
    const draft = initDraft(fixturePayload());
    const request = toRequest(draft);
    expect(request.topics).toEqual([
      { id: 1, name: "Intro", description: "Intro topic", mergedFromTopicIds: [] },
      { id: 2, name: "Advanced", description: "Advanced topic", mergedFromTopicIds: [] },
      { id: 3, name: "Extra", description: "Extra topic", mergedFromTopicIds: [] },
    ]);
    expect(request.edges).toEqual([{ fromIndex: 0, toIndex: 1, relation: "prerequisite" }]);
  });

  it("index correctness after a delete: indexes reflect the post-delete order, not original ids", () => {
    const draft = initDraft(fixturePayload());
    const next = deleteTopic(draft, keyFor(draft, 1)); // remove the first topic
    const request = toRequest(next);
    expect(request.topics.map((t) => t.id)).toEqual([2, 3]);
    expect(request.edges).toEqual([]); // the edge touching topic 1 was dropped
  });

  it("index correctness after a merge: the edge list and indexes reflect the merged result", () => {
    let draft = initDraft(fixturePayload());
    const introKey = keyFor(draft, 1);
    const extraKey = keyFor(draft, 3);
    draft = addEdge(draft, extraKey, introKey, "related");
    draft = toggleMergeSelect(draft, introKey);
    draft = toggleMergeSelect(draft, keyFor(draft, 2));
    draft = mergeSelectedInto(draft, keyFor(draft, 2)); // merges intro (source) into advanced

    const request = toRequest(draft);
    expect(request.topics).toHaveLength(2);
    const targetIndex = request.topics.findIndex((t) => t.id === 2);
    expect(request.topics[targetIndex].mergedFromTopicIds).toEqual([1]);
    // The edge (extra -> intro) referenced a merged-away topic, so it was dropped.
    expect(request.edges).toEqual([]);
  });

  it("a brand-new topic serializes with id: null at its current index", () => {
    const draft = addTopic(initDraft(fixturePayload()));
    const named = rename(draft, draft.topics[3].key, "New Topic");
    const request = toRequest(named);
    expect(request.topics[3]).toEqual({ id: null, name: "New Topic", description: "", mergedFromTopicIds: [] });
  });
});

describe("estimateReclassifyCount", () => {
  function payloadWithMaterials(): GraphPayload {
    const payload = fixturePayload();
    payload.materials = [
      { id: 10, title: "On Intro", kind: "document", status: "summarized", maxConfidence: 0.9 },
      { id: 11, title: "On Advanced", kind: "document", status: "summarized", maxConfidence: 0.8 },
      { id: 12, title: "On Extra", kind: "document", status: "summarized", maxConfidence: 0.7 },
      { id: 13, title: "Not yet summarized", kind: "document", status: "fetched", maxConfidence: null },
    ];
    payload.attachments = [
      { topicId: 1, materialId: 10, confidence: 0.9, rationale: "r" },
      { topicId: 2, materialId: 11, confidence: 0.8, rationale: "r" },
      { topicId: 3, materialId: 12, confidence: 0.7, rationale: "r" },
    ];
    return payload;
  }

  it("is 0 when the draft isn't structural, regardless of coverage", () => {
    const payload = payloadWithMaterials();
    const draft = rename(initDraft(payload), keyFor(initDraft(payload), 1), "Renamed");
    expect(estimateReclassifyCount(draft, payload)).toBe(0);
  });

  it("counts materials whose only topic was deleted", () => {
    const payload = payloadWithMaterials();
    const draft = deleteTopic(initDraft(payload), keyFor(initDraft(payload), 3)); // deletes "extra" (material 12)
    expect(estimateReclassifyCount(draft, payload)).toBe(1);
  });

  it("does not count a material whose topic was kept, even if unrelated topics changed", () => {
    const payload = payloadWithMaterials();
    const draft = addTopic(initDraft(payload)); // structural, but nothing deleted
    expect(estimateReclassifyCount(draft, payload)).toBe(0);
  });

  it("a merged topic's materials stay covered (mapped onto the merge target)", () => {
    const payload = payloadWithMaterials();
    let draft = initDraft(payload);
    draft = toggleMergeSelect(draft, keyFor(draft, 1));
    draft = toggleMergeSelect(draft, keyFor(draft, 2));
    draft = mergeSelectedInto(draft, keyFor(draft, 2)); // intro (material 10) folds into advanced
    expect(estimateReclassifyCount(draft, payload)).toBe(0);
  });
});
