import { Handle, Position } from "@xyflow/react";
import type { NodeProps } from "@xyflow/react";
import { ChevronRight } from "lucide-react";
import { memo } from "react";

import { UNSORTED_TOPIC_ID } from "../../api/types";
import { TOPIC_NODE_SIZE } from "../layout";
import type { TopicFlowNode } from "../transform";

function TopicNodeImpl({ data }: NodeProps<TopicFlowNode>) {
  const { topic, expanded, selected } = data;
  const isUnsorted = topic.id === UNSORTED_TOPIC_ID;

  return (
    <div
      title={topic.description}
      style={{ width: TOPIC_NODE_SIZE.width, height: TOPIC_NODE_SIZE.height }}
      className={[
        "flex items-center gap-2 rounded-lg border px-3 py-2 shadow-sm transition-colors",
        isUnsorted
          ? "border-dashed border-neutral-300 bg-neutral-100 text-neutral-500 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-400"
          : "border-neutral-200 bg-white text-neutral-900 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-100",
        selected ? "ring-2 ring-blue-500 ring-offset-1 dark:ring-offset-neutral-950" : "",
      ].join(" ")}
    >
      <Handle type="target" position={Position.Top} className="!opacity-0" />
      <ChevronRight
        size={16}
        className={`shrink-0 transition-transform duration-150 ${expanded ? "rotate-90" : ""}`}
        aria-hidden
      />
      <span className="min-w-0 flex-1 truncate text-sm font-medium">{topic.name}</span>
      <span className="shrink-0 rounded-full bg-neutral-100 px-1.5 py-0.5 text-xs tabular-nums text-neutral-600 dark:bg-neutral-700 dark:text-neutral-300">
        {topic.materialCount}
      </span>
      <Handle type="source" position={Position.Bottom} className="!opacity-0" />
    </div>
  );
}

export const TopicNode = memo(TopicNodeImpl);
