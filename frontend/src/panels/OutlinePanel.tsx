import { ChevronRight } from "lucide-react";
import { useMemo } from "react";

import type { GraphAttachment, GraphMaterial, GraphPayload, GraphTopic } from "../api/types";
import { UNSORTED_TOPIC_ID } from "../api/types";
import { KIND_ICON } from "../graph/nodes/MaterialNode";
import { useUiStore } from "../state/uiStore";

interface OutlinePanelProps {
  payload: GraphPayload;
}

/** Materials attached to each topic, in the graph/transform's deterministic
 * order (title, then id), joined with their material record. */
function useMaterialsByTopic(payload: GraphPayload): Map<number, GraphMaterial[]> {
  return useMemo(() => {
    const materialsById = new Map(payload.materials.map((material) => [material.id, material]));
    const byTopic = new Map<number, GraphAttachment[]>();
    for (const attachment of payload.attachments) {
      const list = byTopic.get(attachment.topicId);
      if (list) {
        list.push(attachment);
      } else {
        byTopic.set(attachment.topicId, [attachment]);
      }
    }
    const result = new Map<number, GraphMaterial[]>();
    for (const [topicId, attachments] of byTopic) {
      const materials = attachments
        .map((attachment) => materialsById.get(attachment.materialId))
        .filter((material): material is GraphMaterial => material !== undefined)
        .sort((a, b) => a.title.localeCompare(b.title) || a.id - b.id);
      result.set(topicId, materials);
    }
    return result;
  }, [payload.attachments, payload.materials]);
}

/** A synced outline of the course graph: topic rows (chevron toggles
 * expand, name selects) that expand to their attached material rows
 * (select). Reads the same `GraphPayload` + `uiStore` the graph itself
 * does -- no separate fetch, and clicking a row here drives the exact same
 * store actions a click on the graph node would. */
export function OutlinePanel({ payload }: OutlinePanelProps) {
  const expandedTopicIds = useUiStore((state) => state.expandedTopicIds);
  const selection = useUiStore((state) => state.selection);
  const toggleExpandTopic = useUiStore((state) => state.toggleExpandTopic);
  const selectTopic = useUiStore((state) => state.selectTopic);
  const selectMaterial = useUiStore((state) => state.selectMaterial);

  const topics = useMemo(
    () => [...payload.topics].sort((a, b) => a.orderIndex - b.orderIndex),
    [payload.topics],
  );
  const materialsByTopic = useMaterialsByTopic(payload);

  return (
    <nav aria-label="Course outline" className="text-sm">
      <ul>
        {topics.map((topic) => (
          <TopicRow
            key={topic.id}
            topic={topic}
            expanded={expandedTopicIds.has(topic.id)}
            selected={selection?.type === "topic" && selection.id === topic.id}
            materials={materialsByTopic.get(topic.id) ?? []}
            selectedMaterialId={selection?.type === "material" ? selection.id : null}
            onToggleExpand={() => toggleExpandTopic(topic.id)}
            onSelectTopic={() => selectTopic(topic.id)}
            onSelectMaterial={selectMaterial}
          />
        ))}
      </ul>
    </nav>
  );
}

interface TopicRowProps {
  topic: GraphTopic;
  expanded: boolean;
  selected: boolean;
  materials: GraphMaterial[];
  selectedMaterialId: number | null;
  onToggleExpand: () => void;
  onSelectTopic: () => void;
  onSelectMaterial: (materialId: number) => void;
}

function TopicRow({
  topic,
  expanded,
  selected,
  materials,
  selectedMaterialId,
  onToggleExpand,
  onSelectTopic,
  onSelectMaterial,
}: TopicRowProps) {
  const isUnsorted = topic.id === UNSORTED_TOPIC_ID;

  return (
    <li>
      <div
        className={[
          "flex items-center gap-1 rounded-md px-1 py-1",
          selected ? "bg-blue-50 dark:bg-blue-950" : "",
          isUnsorted ? "text-neutral-400 dark:text-neutral-500" : "",
        ].join(" ")}
      >
        <button
          type="button"
          onClick={onToggleExpand}
          aria-label={expanded ? `Collapse ${topic.name}` : `Expand ${topic.name}`}
          aria-expanded={expanded}
          className="shrink-0 rounded p-0.5 text-neutral-500 hover:text-neutral-800 dark:text-neutral-400 dark:hover:text-neutral-100"
        >
          <ChevronRight
            size={14}
            className={`transition-transform duration-150 ${expanded ? "rotate-90" : ""}`}
            aria-hidden
          />
        </button>
        <button
          type="button"
          onClick={onSelectTopic}
          title={topic.description}
          className={[
            "min-w-0 flex-1 truncate rounded px-1 py-0.5 text-left font-medium",
            selected
              ? "text-blue-700 dark:text-blue-300"
              : isUnsorted
                ? "italic text-neutral-500 dark:text-neutral-400"
                : "text-neutral-800 dark:text-neutral-100",
          ].join(" ")}
        >
          {topic.name}
        </button>
        <span className="shrink-0 rounded-full bg-neutral-100 px-1.5 py-0.5 text-xs tabular-nums text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
          {topic.materialCount}
        </span>
      </div>

      {expanded && (
        <ul className="ml-4 border-l border-neutral-200 pl-2 dark:border-neutral-800">
          {materials.map((material) => (
            <MaterialRow
              key={material.id}
              material={material}
              selected={selectedMaterialId === material.id}
              onSelect={() => onSelectMaterial(material.id)}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

interface MaterialRowProps {
  material: GraphMaterial;
  selected: boolean;
  onSelect: () => void;
}

function MaterialRow({ material, selected, onSelect }: MaterialRowProps) {
  const Icon = KIND_ICON[material.kind];
  const lowConfidence = material.maxConfidence !== null && material.maxConfidence < 0.5;

  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        title={material.title}
        className={[
          "flex w-full min-w-0 items-center gap-1.5 rounded px-1 py-1 text-left",
          selected
            ? "bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
            : "text-neutral-600 hover:bg-neutral-100 dark:text-neutral-300 dark:hover:bg-neutral-900",
        ].join(" ")}
      >
        <Icon size={12} className="shrink-0 text-neutral-400 dark:text-neutral-500" aria-hidden />
        <span className="min-w-0 flex-1 truncate">{material.title}</span>
        {lowConfidence && (
          <span
            title="Low classification confidence"
            className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500"
          />
        )}
      </button>
    </li>
  );
}
