import { Handle, Position } from "@xyflow/react";
import type { NodeProps } from "@xyflow/react";
import {
  BookOpen,
  Captions,
  ClipboardList,
  FileText,
  File,
  Link as LinkIcon,
  Megaphone,
  Presentation,
  Video,
} from "lucide-react";
import { memo } from "react";
import type { ComponentType } from "react";

import type { MaterialKind } from "../../api/types";
import { MATERIAL_NODE_SIZE } from "../layout";
import type { MaterialFlowNode } from "../transform";

/** Per-kind icon, shared with the outline/detail panels (Task 11) so the
 * kind→icon mapping lives in exactly one place. */
export const KIND_ICON: Record<MaterialKind, ComponentType<{ size?: number; className?: string }>> = {
  syllabus: BookOpen,
  slides: Presentation,
  document: FileText,
  assignment: ClipboardList,
  announcement: Megaphone,
  video: Video,
  transcript: Captions,
  link: LinkIcon,
  other: File,
};

function MaterialNodeImpl({ data }: NodeProps<MaterialFlowNode>) {
  const { material, selected } = data;
  const Icon = KIND_ICON[material.kind] ?? File;
  const lowConfidence = material.maxConfidence !== null && material.maxConfidence < 0.5;

  return (
    <div
      title={material.title}
      style={{ width: MATERIAL_NODE_SIZE.width, height: MATERIAL_NODE_SIZE.height }}
      className={[
        "flex items-center gap-2 rounded-md border px-2 py-1.5 text-xs shadow-sm transition-colors",
        "border-neutral-200 bg-white text-neutral-700 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-200",
        selected ? "ring-2 ring-blue-500 ring-offset-1 dark:ring-offset-neutral-950" : "",
      ].join(" ")}
    >
      <Handle type="target" position={Position.Top} className="!opacity-0" />
      <Icon size={14} className="shrink-0 text-neutral-500 dark:text-neutral-400" aria-hidden />
      <span className="min-w-0 flex-1 truncate">{material.title}</span>
      {lowConfidence && (
        <span
          title="Low classification confidence"
          className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500"
        />
      )}
    </div>
  );
}

export const MaterialNode = memo(MaterialNodeImpl);
