import { create } from "zustand";

import type { Selection } from "../graph/transform";

export type { Selection };

interface UiState {
  /** Topics currently expanded (materials shown) in the graph + outline. */
  expandedTopicIds: Set<number>;
  /** The one thing currently selected -- drives the detail panel (Task 11)
   * and the graph/outline's highlight ring. Never both a topic and a
   * material at once. */
  selection: Selection;

  /** Expand a collapsed topic, or collapse an expanded one. */
  toggleExpandTopic: (topicId: number) => void;

  /** Select a topic, or deselect it if it's already the current selection
   * (clicking the same node twice toggles it off). NOT idempotent -- don't
   * use this to hydrate selection from a source of truth outside a user
   * click (e.g. the URL on page load); use `setSelection` for that. */
  selectTopic: (topicId: number) => void;
  /** Select a material, or deselect it if it's already the current
   * selection. Same non-idempotence caveat as `selectTopic`. */
  selectMaterial: (materialId: number) => void;
  /** Clear the selection unconditionally. */
  clearSelection: () => void;
  /** Set the selection to exactly this value, no toggle. Idempotent --
   * safe to call from an effect that might run more than once for the same
   * input (e.g. React StrictMode's dev-mode double-invoke), unlike
   * `selectTopic`/`selectMaterial`. */
  setSelection: (selection: Selection) => void;
}

export const useUiStore = create<UiState>((set) => ({
  expandedTopicIds: new Set<number>(),
  selection: null,

  toggleExpandTopic: (topicId) =>
    set((state) => {
      const next = new Set(state.expandedTopicIds);
      if (next.has(topicId)) {
        next.delete(topicId);
      } else {
        next.add(topicId);
      }
      return { expandedTopicIds: next };
    }),

  selectTopic: (topicId) =>
    set((state) => ({
      selection:
        state.selection?.type === "topic" && state.selection.id === topicId
          ? null
          : { type: "topic", id: topicId },
    })),

  selectMaterial: (materialId) =>
    set((state) => ({
      selection:
        state.selection?.type === "material" && state.selection.id === materialId
          ? null
          : { type: "material", id: materialId },
    })),

  clearSelection: () => set({ selection: null }),

  setSelection: (selection) => set({ selection }),
}));
