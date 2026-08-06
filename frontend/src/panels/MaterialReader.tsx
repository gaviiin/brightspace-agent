import { useQuery } from "@tanstack/react-query";

import { getMaterialFileUrl, getMaterialTextUrl } from "../api/client";
import type { MaterialDetail } from "../api/types";
import { chooseReaderMode } from "./readerMode";

interface MaterialReaderProps {
  material: MaterialDetail;
}

/** Renders the appropriate preview for a material -- an embedded PDF, its
 * extracted text, a link-out button, a video-source anchor, a download
 * link, or nothing -- per `chooseReaderMode`'s dispatch. */
export function MaterialReader({ material }: MaterialReaderProps) {
  const mode = chooseReaderMode(material);

  switch (mode) {
    case "pdf-iframe":
      return (
        <iframe
          src={getMaterialFileUrl(material.id)}
          title={material.title}
          className="h-[60vh] w-full rounded-md border border-neutral-200 dark:border-neutral-800"
        />
      );
    case "text":
      return <ExtractedText materialId={material.id} />;
    case "link-out":
      return (
        <a
          href={material.sourceUrl ?? undefined}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-blue-700"
        >
          Open link
        </a>
      );
    case "video-link":
      return (
        <a
          href={material.sourceUrl ?? undefined}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-blue-700"
        >
          Watch video
        </a>
      );
    case "download":
      return (
        <a
          href={getMaterialFileUrl(material.id)}
          download
          className="inline-flex items-center rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium text-neutral-700 transition hover:bg-neutral-100 dark:border-neutral-700 dark:text-neutral-200 dark:hover:bg-neutral-800"
        >
          Download
        </a>
      );
    case "none":
      return <p className="text-sm text-neutral-500 dark:text-neutral-400">No preview available.</p>;
  }
}

function ExtractedText({ materialId }: { materialId: number }) {
  const textQuery = useQuery({
    queryKey: ["material-text", materialId],
    queryFn: async () => {
      const response = await fetch(getMaterialTextUrl(materialId));
      if (!response.ok) throw new Error(`failed to load extracted text (${response.status})`);
      return response.text();
    },
  });

  return (
    <div>
      <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
        Extracted text
      </h3>
      {textQuery.isLoading && (
        <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading text…</p>
      )}
      {textQuery.isError && (
        <p className="text-sm text-red-600 dark:text-red-400">Couldn't load the extracted text.</p>
      )}
      {textQuery.data !== undefined && (
        <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-words rounded-md border border-neutral-200 bg-neutral-50 p-3 font-sans text-sm text-neutral-800 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-200">
          {textQuery.data}
        </pre>
      )}
    </div>
  );
}
