import { beforeEach, describe, expect, it } from "vitest";

import { useUiStore } from "./uiStore";

function reset() {
  useUiStore.setState({ expandedTopicIds: new Set(), selection: null });
}

beforeEach(reset);

describe("uiStore: toggleExpandTopic", () => {
  it("expands a collapsed topic and collapses an expanded one", () => {
    useUiStore.getState().toggleExpandTopic(1);
    expect(useUiStore.getState().expandedTopicIds.has(1)).toBe(true);

    useUiStore.getState().toggleExpandTopic(1);
    expect(useUiStore.getState().expandedTopicIds.has(1)).toBe(false);
  });

  it("leaves other expanded topics alone", () => {
    useUiStore.getState().toggleExpandTopic(1);
    useUiStore.getState().toggleExpandTopic(2);
    expect([...useUiStore.getState().expandedTopicIds].sort()).toEqual([1, 2]);

    useUiStore.getState().toggleExpandTopic(1);
    expect([...useUiStore.getState().expandedTopicIds]).toEqual([2]);
  });

  it("produces a new Set instance on each toggle (referential-equality-friendly for memoized consumers)", () => {
    const before = useUiStore.getState().expandedTopicIds;
    useUiStore.getState().toggleExpandTopic(1);
    const after = useUiStore.getState().expandedTopicIds;
    expect(after).not.toBe(before);
  });
});

describe("uiStore: selectTopic / selectMaterial (select/deselect)", () => {
  it("selects a topic", () => {
    useUiStore.getState().selectTopic(5);
    expect(useUiStore.getState().selection).toEqual({ type: "topic", id: 5 });
  });

  it("clicking the same topic again deselects it", () => {
    useUiStore.getState().selectTopic(5);
    useUiStore.getState().selectTopic(5);
    expect(useUiStore.getState().selection).toBeNull();
  });

  it("selecting a different topic replaces the selection (not a toggle-off)", () => {
    useUiStore.getState().selectTopic(5);
    useUiStore.getState().selectTopic(6);
    expect(useUiStore.getState().selection).toEqual({ type: "topic", id: 6 });
  });

  it("selects a material, independent of any topic selection", () => {
    useUiStore.getState().selectTopic(5);
    useUiStore.getState().selectMaterial(9);
    expect(useUiStore.getState().selection).toEqual({ type: "material", id: 9 });
  });

  it("clicking the same material again deselects it", () => {
    useUiStore.getState().selectMaterial(9);
    useUiStore.getState().selectMaterial(9);
    expect(useUiStore.getState().selection).toBeNull();
  });

  it("clearSelection resets to null unconditionally", () => {
    useUiStore.getState().selectTopic(5);
    useUiStore.getState().clearSelection();
    expect(useUiStore.getState().selection).toBeNull();
  });
});

describe("uiStore: setSelection (idempotent, non-toggling)", () => {
  it("sets the selection directly, with no toggle-off on a repeated call", () => {
    useUiStore.getState().setSelection({ type: "topic", id: 5 });
    expect(useUiStore.getState().selection).toEqual({ type: "topic", id: 5 });

    // Unlike selectTopic, calling it again with the SAME value must not
    // clear it -- this is what makes it safe to call from an effect that
    // may run twice for the same input (e.g. React StrictMode's dev-mode
    // double-invoke of effects), such as hydrating selection from a URL
    // search param on page load.
    useUiStore.getState().setSelection({ type: "topic", id: 5 });
    expect(useUiStore.getState().selection).toEqual({ type: "topic", id: 5 });
  });

  it("can set the selection to null directly", () => {
    useUiStore.getState().setSelection({ type: "material", id: 9 });
    useUiStore.getState().setSelection(null);
    expect(useUiStore.getState().selection).toBeNull();
  });
});
