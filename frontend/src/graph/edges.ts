// Edge type -> style constants. Colors are CSS custom properties (see
// src/index.css) so edges stay legible in both light and dark mode without
// this module needing to know which theme is active.
import { MarkerType } from "@xyflow/react";
import type { CSSProperties } from "react";

import type { FlowEdge, FlowEdgeType } from "./transform";

interface EdgeStylePreset {
  style: CSSProperties;
  markerEnd?: { type: MarkerType; color?: string };
}

export const EDGE_STYLE: Record<FlowEdgeType, EdgeStylePreset> = {
  prerequisite: {
    style: { stroke: "var(--bsa-edge-strong)", strokeWidth: 1.5 },
    markerEnd: { type: MarkerType.ArrowClosed, color: "var(--bsa-edge-strong)" },
  },
  related: {
    style: { stroke: "var(--bsa-edge-soft)", strokeWidth: 1.5, strokeDasharray: "6 4" },
  },
  attachment: {
    style: { stroke: "var(--bsa-edge-attach)", strokeWidth: 1 },
  },
};

/** Apply the type-appropriate style/marker to a single edge. */
export function styleEdge(edge: FlowEdge): FlowEdge {
  const preset = EDGE_STYLE[edge.type as FlowEdgeType];
  if (!preset) return edge;
  return { ...edge, ...preset };
}
