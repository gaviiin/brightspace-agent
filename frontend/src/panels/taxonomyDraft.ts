// Pure draft-state logic for the taxonomy editor (Task 12). No React, no
// network -- `initDraft` copies a `GraphPayload` into an editable `Draft`,
// every operation below takes a `Draft` and returns a NEW `Draft` (nothing
// is mutated in place, so the query cache `initDraft` reads from is never
// touched), and `toRequest`/`isStructural` are the two things
// TaxonomyEditor.tsx and the PUT call ultimately need.
//
// Topics carry a client-side `key` (stable across reorders/deletes) distinct
// from their server `id` (`null` for a topic the student just added) -- every
// operation addresses a topic by `key`, and `toRequest` resolves `key`s back
// into array indexes only at the very end, exactly once.
import type { GraphPayload, TaxonomyEditRequest, TopicEdgeRelation } from "../api/types";
import { ADMIN_TOPIC_ID, UNSORTED_TOPIC_ID } from "../api/types";

export type EdgeRelation = TopicEdgeRelation;

export interface DraftTopic {
  /** Stable client-side identity -- NOT sent to the server. */
  key: string;
  /** The server's topic id, or `null` for a topic added in this draft. */
  id: number | null;
  name: string;
  description: string;
  /** ids of CURRENT-version topics folded into this one (own id excluded). */
  mergedFromTopicIds: number[];
  /** How many materials are attached right now -- drives the editor's
   * "N materials will be re-classified" delete confirmation. Summed into
   * the target when other topics merge into this one. */
  materialCount: number;
  /** The exact draft topics folded into this one, kept around so `unmerge`
   * can restore them verbatim. Empty unless this topic is a merge target. */
  mergedAway: DraftTopic[];
}

export interface DraftEdge {
  key: string;
  fromKey: string;
  toKey: string;
  relation: EdgeRelation;
}

/** A snapshot of the taxonomy this draft started from, used only to decide
 * `isStructural` -- never mutated after `initDraft`. */
interface DraftBase {
  topicIds: Set<number>;
  edgeTriples: Set<string>;
}

export interface Draft {
  topics: DraftTopic[];
  edges: DraftEdge[];
  mergeSelection: Set<string>;
  base: DraftBase;
  nextTopicSeq: number;
  nextEdgeSeq: number;
}

function edgeTriple(fromId: number, toId: number, relation: string): string {
  return `${fromId}|${toId}|${relation}`;
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

/** Builds an editable draft from a course's current graph. The synthetic
 * "Unsorted" topic (id 0) and "Logistics & admin" topic (id -1, M3.5a --
 * see graph/build.py) are never editable and are dropped here, along with
 * any edge that happens to touch either one (neither has one in practice,
 * but dropping defensively costs nothing). Missing either id here means it
 * flows into `toRequest`'s PUT body, which `taxonomy_apply.py` rejects as
 * an unknown topic id -- the whole editor 422s for any course that has so
 * much as one material in that bucket. */
export function initDraft(payload: GraphPayload): Draft {
  const isSynthetic = (id: number) => id === UNSORTED_TOPIC_ID || id === ADMIN_TOPIC_ID;

  const realTopics = [...payload.topics]
    .filter((topic) => !isSynthetic(topic.id))
    .sort((a, b) => a.orderIndex - b.orderIndex);

  const topics: DraftTopic[] = realTopics.map((topic) => ({
    key: `existing-${topic.id}`,
    id: topic.id,
    name: topic.name,
    description: topic.description,
    mergedFromTopicIds: [],
    materialCount: topic.materialCount,
    mergedAway: [],
  }));

  const keyById = new Map(topics.map((topic) => [topic.id as number, topic.key]));
  const realEdges = payload.topicEdges.filter(
    (edge) => !isSynthetic(edge.fromTopicId) && !isSynthetic(edge.toTopicId),
  );
  const edges: DraftEdge[] = realEdges
    .map((edge, index) => ({
      key: `edge-${index}`,
      fromKey: keyById.get(edge.fromTopicId),
      toKey: keyById.get(edge.toTopicId),
      relation: edge.relation,
    }))
    .filter((edge): edge is DraftEdge => edge.fromKey !== undefined && edge.toKey !== undefined);

  return {
    topics,
    edges,
    mergeSelection: new Set(),
    base: {
      topicIds: new Set(topics.map((topic) => topic.id as number)),
      edgeTriples: new Set(realEdges.map((edge) => edgeTriple(edge.fromTopicId, edge.toTopicId, edge.relation))),
    },
    nextTopicSeq: topics.length,
    nextEdgeSeq: edges.length,
  };
}

// ---------------------------------------------------------------------------
// Wording edits -- never structural
// ---------------------------------------------------------------------------

export function rename(draft: Draft, key: string, name: string): Draft {
  return {
    ...draft,
    topics: draft.topics.map((topic) => (topic.key === key ? { ...topic, name } : topic)),
  };
}

export function setDescription(draft: Draft, key: string, description: string): Draft {
  return {
    ...draft,
    topics: draft.topics.map((topic) => (topic.key === key ? { ...topic, description } : topic)),
  };
}

// ---------------------------------------------------------------------------
// Add / delete
// ---------------------------------------------------------------------------

export function addTopic(draft: Draft): Draft {
  const topic: DraftTopic = {
    key: `new-${draft.nextTopicSeq}`,
    id: null,
    name: "",
    description: "",
    mergedFromTopicIds: [],
    materialCount: 0,
    mergedAway: [],
  };
  return { ...draft, topics: [...draft.topics, topic], nextTopicSeq: draft.nextTopicSeq + 1 };
}

/** Removes a topic from the visible list. If it's a merge target (holds
 * topics folded into it via `mergeSelectedInto`), those are released --
 * restored to the visible list first, exactly as `unmerge` would -- so
 * deleting a merge target can never silently delete the topics merged into
 * it too. Any edge touching the deleted key is dropped along with it. */
export function deleteTopic(draft: Draft, key: string): Draft {
  const target = draft.topics.find((topic) => topic.key === key);
  const restored = target && target.mergedAway.length > 0 ? unmerge(draft, key) : draft;

  return {
    ...restored,
    topics: restored.topics.filter((topic) => topic.key !== key),
    edges: restored.edges.filter((edge) => edge.fromKey !== key && edge.toKey !== key),
    mergeSelection: _withoutKey(restored.mergeSelection, key),
  };
}

// ---------------------------------------------------------------------------
// Merge
// ---------------------------------------------------------------------------

export function toggleMergeSelect(draft: Draft, key: string): Draft {
  const next = new Set(draft.mergeSelection);
  if (next.has(key)) {
    next.delete(key);
  } else {
    next.add(key);
  }
  return { ...draft, mergeSelection: next };
}

/** Folds every OTHER currently-selected topic into `targetKey`: a source
 * with a server id contributes it to the target's `mergedFromTopicIds`; a
 * source that was itself only added in this draft (`id: null`) has nothing
 * to preserve and is simply discarded. Any edge touching a folded-away
 * source is dropped (no retargeting -- keep it simple, per the brief).
 * Clears the merge selection either way. */
export function mergeSelectedInto(draft: Draft, targetKey: string): Draft {
  const sourceKeys = new Set([...draft.mergeSelection].filter((key) => key !== targetKey));
  if (sourceKeys.size === 0) {
    return { ...draft, mergeSelection: new Set() };
  }

  const sources = draft.topics.filter((topic) => sourceKeys.has(topic.key));
  const mergedIds = sources.map((topic) => topic.id).filter((id): id is number => id !== null);
  const addedMaterialCount = sources.reduce((sum, topic) => sum + topic.materialCount, 0);

  const topics = draft.topics
    .filter((topic) => !sourceKeys.has(topic.key))
    .map((topic) =>
      topic.key === targetKey
        ? {
            ...topic,
            mergedFromTopicIds: [...topic.mergedFromTopicIds, ...mergedIds],
            mergedAway: [...topic.mergedAway, ...sources],
            materialCount: topic.materialCount + addedMaterialCount,
          }
        : topic,
    );

  const edges = draft.edges.filter((edge) => !sourceKeys.has(edge.fromKey) && !sourceKeys.has(edge.toKey));

  return { ...draft, topics, edges, mergeSelection: new Set() };
}

/** Reverses every merge currently folded into `targetKey`: the exact draft
 * topics it absorbed reappear in the visible list, its
 * `mergedFromTopicIds`/`mergedAway` clear, and its material count returns
 * to what it was before the merge. A no-op if the target has nothing
 * merged into it. */
export function unmerge(draft: Draft, targetKey: string): Draft {
  const target = draft.topics.find((topic) => topic.key === targetKey);
  if (!target || target.mergedAway.length === 0) return draft;

  const restoredCount = target.materialCount - target.mergedAway.reduce((sum, t) => sum + t.materialCount, 0);
  const topics = draft.topics.flatMap((topic) =>
    topic.key === targetKey
      ? [{ ...topic, mergedFromTopicIds: [], mergedAway: [], materialCount: restoredCount }, ...topic.mergedAway]
      : [topic],
  );

  return { ...draft, topics };
}

// ---------------------------------------------------------------------------
// Edges
// ---------------------------------------------------------------------------

/** No-ops on a self-loop (`fromKey === toKey`) or an exact duplicate of an
 * edge already in the draft (same from/to/relation) -- the server rejects
 * both outright (422: self-loop, or a repeated triple hitting TopicEdge's
 * UniqueConstraint), so the draft never lets either exist in the first
 * place. */
export function addEdge(draft: Draft, fromKey: string, toKey: string, relation: EdgeRelation): Draft {
  if (fromKey === toKey) return draft;
  const isDuplicate = draft.edges.some(
    (edge) => edge.fromKey === fromKey && edge.toKey === toKey && edge.relation === relation,
  );
  if (isDuplicate) return draft;
  const edge: DraftEdge = { key: `edge-new-${draft.nextEdgeSeq}`, fromKey, toKey, relation };
  return { ...draft, edges: [...draft.edges, edge], nextEdgeSeq: draft.nextEdgeSeq + 1 };
}

export function removeEdge(draft: Draft, edgeKey: string): Draft {
  return { ...draft, edges: draft.edges.filter((edge) => edge.key !== edgeKey) };
}

// ---------------------------------------------------------------------------
// Decision + request shape
// ---------------------------------------------------------------------------

/** Mirrors the backend's patch-vs-structural rule exactly (see
 * pipeline/taxonomy_apply.py): true unless the draft keeps the exact same
 * topic id set (no adds, no deletes, no merges) AND the exact same edge set
 * -- wording (name/description) never factors in, which is the whole point
 * of the patch path. */
export function isStructural(draft: Draft): boolean {
  if (draft.topics.some((topic) => topic.id === null || topic.mergedFromTopicIds.length > 0)) {
    return true;
  }
  const currentIds = new Set(draft.topics.map((topic) => topic.id as number));
  if (currentIds.size !== draft.base.topicIds.size || [...currentIds].some((id) => !draft.base.topicIds.has(id))) {
    return true;
  }

  const currentEdges = new Set(
    draft.edges.map((edge) => {
      const from = draft.topics.find((topic) => topic.key === edge.fromKey);
      const to = draft.topics.find((topic) => topic.key === edge.toKey);
      return edgeTriple(from?.id ?? -1, to?.id ?? -1, edge.relation);
    }),
  );
  if (currentEdges.size !== draft.base.edgeTriples.size) return true;
  for (const triple of currentEdges) {
    if (!draft.base.edgeTriples.has(triple)) return true;
  }
  return false;
}

/** The PUT body, per the Task 12 contract -- indexes are resolved from
 * `key`s here, once, against the draft's current (post delete/merge) topic
 * order. An edge whose endpoint key can't be found (shouldn't happen --
 * `deleteTopic`/`mergeSelectedInto` already drop those) is dropped rather
 * than sent with a bogus index. */
export function toRequest(draft: Draft): TaxonomyEditRequest {
  const indexByKey = new Map(draft.topics.map((topic, index) => [topic.key, index]));

  const topics: TaxonomyEditRequest["topics"] = draft.topics.map((topic) => ({
    id: topic.id,
    name: topic.name,
    description: topic.description,
    mergedFromTopicIds: topic.mergedFromTopicIds,
  }));

  const edges: TaxonomyEditRequest["edges"] = draft.edges
    .map((edge) => ({
      fromIndex: indexByKey.get(edge.fromKey),
      toIndex: indexByKey.get(edge.toKey),
      relation: edge.relation,
    }))
    .filter(
      (edge): edge is TaxonomyEditRequest["edges"][number] =>
        edge.fromIndex !== undefined && edge.toIndex !== undefined,
    );

  return { topics, edges };
}

/** An estimate of how many materials the structural save will queue for
 * re-classification (drives the editor's "Save & re-classify (N)" label).
 * Mirrors the server's carry-over rule using only what `GraphPayload`
 * already has on hand: a material stays covered if at least one of its
 * current attachments points at a topic id that's still kept (directly, or
 * folded into a merge target) -- everything else (including a material
 * that was already unfiled) is counted, matching what S3's classify
 * worklist ("summarized materials with zero rows at the new version")
 * would actually pick up. `0` for a patch, since nothing gets re-classified
 * then. */
export function estimateReclassifyCount(draft: Draft, payload: GraphPayload): number {
  if (!isStructural(draft)) return 0;

  const survivingOldIds = new Set<number>();
  for (const topic of draft.topics) {
    if (topic.id !== null) survivingOldIds.add(topic.id);
    for (const mergedId of topic.mergedFromTopicIds) survivingOldIds.add(mergedId);
  }

  const topicIdsByMaterial = new Map<number, number[]>();
  for (const attachment of payload.attachments) {
    if (attachment.topicId === UNSORTED_TOPIC_ID) continue; // never real coverage
    const list = topicIdsByMaterial.get(attachment.materialId);
    if (list) {
      list.push(attachment.topicId);
    } else {
      topicIdsByMaterial.set(attachment.materialId, [attachment.topicId]);
    }
  }

  let count = 0;
  for (const material of payload.materials) {
    if (material.status !== "summarized") continue; // only summarized materials are ever classified
    const topicIds = topicIdsByMaterial.get(material.id) ?? [];
    const staysCovered = topicIds.some((id) => survivingOldIds.has(id));
    if (!staysCovered) count += 1;
  }
  return count;
}

function _withoutKey(set: Set<string>, key: string): Set<string> {
  if (!set.has(key)) return set;
  const next = new Set(set);
  next.delete(key);
  return next;
}
