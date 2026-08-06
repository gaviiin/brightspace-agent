// Vitest setup file (see vitest.config.ts's test.setupFiles).
//
// jsdom has no ResizeObserver implementation at all, but @xyflow/react uses
// one internally to measure node dimensions -- without this polyfill,
// mounting <ReactFlow> under jsdom throws "ResizeObserver is not defined".
// A no-op observer is enough for the component smoke tests here (they don't
// assert on measured pixel sizes, just rendered node/edge counts).
class ResizeObserverMock implements ResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

globalThis.ResizeObserver = ResizeObserverMock;
