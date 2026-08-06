import {
  Background,
  Controls,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
} from "@xyflow/react";
import type { NodeMouseHandler, NodeTypes } from "@xyflow/react";
import { useCallback, useEffect, useMemo } from "react";

import type { GraphPayload } from "../api/types";
import { useUiStore } from "../state/uiStore";
import { styleEdge } from "./edges";
import { layoutFlow } from "./layout";
import { MaterialNode } from "./nodes/MaterialNode";
import { TopicNode } from "./nodes/TopicNode";
import type { FlowNode, MaterialNodeData, TopicNodeData } from "./transform";
import { toFlow } from "./transform";

const NODE_TYPES: NodeTypes = { topic: TopicNode, material: MaterialNode };

interface GraphViewProps {
  payload: GraphPayload;
}

export function GraphView({ payload }: GraphViewProps) {
  return (
    <ReactFlowProvider>
      <GraphViewInner payload={payload} />
    </ReactFlowProvider>
  );
}

function GraphViewInner({ payload }: GraphViewProps) {
  const expandedTopicIds = useUiStore((state) => state.expandedTopicIds);
  const selection = useUiStore((state) => state.selection);
  const toggleExpandTopic = useUiStore((state) => state.toggleExpandTopic);
  const selectTopic = useUiStore((state) => state.selectTopic);
  const selectMaterial = useUiStore((state) => state.selectMaterial);
  const reactFlowInstance = useReactFlow();

  // Relayout (transform + dagre) whenever the payload, the expanded set, or
  // the selection changes -- this is the single source of truth for
  // nodes/edges; React Flow is rendered fully controlled from it (no
  // onNodesChange -- dragging is disabled below, since dagre owns layout).
  const { nodes, edges } = useMemo(() => {
    const flow = toFlow(payload, expandedTopicIds, selection);
    const laidOutNodes = layoutFlow(flow.nodes, flow.edges);
    return { nodes: laidOutNodes, edges: flow.edges.map(styleEdge) };
  }, [payload, expandedTopicIds, selection]);

  // Refit the viewport after an expand/collapse (or a fresh payload)
  // changes which nodes are visible. Selection alone doesn't change the
  // visible node set, so it's deliberately not a dependency here.
  useEffect(() => {
    reactFlowInstance.fitView({ duration: 300 });
  }, [payload, expandedTopicIds, reactFlowInstance]);

  const onNodeClick = useCallback<NodeMouseHandler<FlowNode>>(
    (_event, node) => {
      if (node.type === "topic") {
        const { topic } = node.data as TopicNodeData;
        toggleExpandTopic(topic.id);
        selectTopic(topic.id);
      } else if (node.type === "material") {
        const { material } = node.data as MaterialNodeData;
        selectMaterial(material.id);
      }
    },
    [toggleExpandTopic, selectTopic, selectMaterial],
  );

  return (
    <div className="h-full w-full bg-neutral-50 dark:bg-neutral-950">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onNodeClick={onNodeClick}
        nodesDraggable={false}
        nodesConnectable={false}
        fitView
        colorMode="system"
      >
        <Background gap={16} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
