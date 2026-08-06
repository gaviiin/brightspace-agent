import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GraphPayload, TaxonomyApplyResponse } from "../api/types";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, putTaxonomy: vi.fn() };
});

import { putTaxonomy } from "../api/client";
import { TaxonomyEditor } from "./TaxonomyEditor";

const mockedPutTaxonomy = vi.mocked(putTaxonomy);

function fixturePayload(): GraphPayload {
  return {
    topics: [
      { id: 1, slug: "intro", name: "Intro", description: "Intro topic.", orderIndex: 0, materialCount: 2 },
      { id: 2, slug: "advanced", name: "Advanced", description: "Advanced topic.", orderIndex: 1, materialCount: 0 },
    ],
    materials: [],
    topicEdges: [{ fromTopicId: 1, toTopicId: 2, relation: "prerequisite" }],
    attachments: [],
    meta: { taxonomyVersion: 1, orphanCount: 0 },
  };
}

function renderWithQueryClient(ui: ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

interface RenderEditorOptions {
  courseId?: number;
  payload?: GraphPayload;
  pipelineActive?: boolean;
  onClose?: () => void;
  onSaved?: (result: TaxonomyApplyResponse) => void;
}

/** Defaults every prop except the ones a test cares about -- in particular
 * `pipelineActive: false`, so only the active-run tests below need to
 * think about it at all. */
function renderEditor(options: RenderEditorOptions = {}) {
  const props = {
    courseId: options.courseId ?? 1,
    payload: options.payload ?? fixturePayload(),
    pipelineActive: options.pipelineActive ?? false,
    onClose: options.onClose ?? vi.fn(),
    onSaved: options.onSaved ?? vi.fn(),
  };
  renderWithQueryClient(<TaxonomyEditor {...props} />);
  return props;
}

beforeEach(() => {
  mockedPutTaxonomy.mockReset();
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(cleanup);

describe("TaxonomyEditor", () => {
  it("renders one row per topic and the existing edge, with a plain 'Save' label (no edits yet)", () => {
    renderEditor();

    expect(screen.getByDisplayValue("Intro")).toBeTruthy();
    expect(screen.getByDisplayValue("Advanced")).toBeTruthy();
    expect(screen.getByText(/Intro.*requires.*Advanced/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save" })).toBeTruthy();
  });

  it("renaming a topic keeps the Save label plain (patch-only edit)", () => {
    renderEditor();

    fireEvent.change(screen.getByDisplayValue("Intro"), { target: { value: "Introduction" } });

    expect(screen.getByDisplayValue("Introduction")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save" })).toBeTruthy();
  });

  it("adding a topic switches the Save button to 'Save & re-classify'", () => {
    renderEditor();

    fireEvent.click(screen.getByText("+ Add topic"));

    expect(screen.getByRole("button", { name: /Save & re-classify/ })).toBeTruthy();
  });

  it("deleting a topic with zero materials needs no confirmation", () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    renderEditor();

    // "Advanced" has materialCount: 0 -- its Delete button is the second one.
    fireEvent.click(screen.getAllByText("Delete")[1]);

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(screen.queryByDisplayValue("Advanced")).toBeNull();
  });

  it("deleting a topic with materials confirms first, and backs out on cancel", () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderEditor();

    // "Intro" has materialCount: 2.
    fireEvent.click(screen.getAllByText("Delete")[0]);

    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("2 materials"));
    expect(screen.getByDisplayValue("Intro")).toBeTruthy(); // still there -- cancelled
  });

  it("Cancel and the header Close button both call onClose without saving", () => {
    const onClose = vi.fn();
    renderEditor({ onClose });

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(mockedPutTaxonomy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("Save calls putTaxonomy with the draft's request body and reports the result via onSaved", async () => {
    const response: TaxonomyApplyResponse = { taxonomyVersion: 1, reclassify: false, runToken: null };
    mockedPutTaxonomy.mockResolvedValue(response);
    const onSaved = vi.fn();
    renderEditor({ courseId: 7, onSaved });

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await vi.waitFor(() =>
      expect(mockedPutTaxonomy).toHaveBeenCalledWith(7, {
        topics: [
          { id: 1, name: "Intro", description: "Intro topic.", mergedFromTopicIds: [] },
          { id: 2, name: "Advanced", description: "Advanced topic.", mergedFromTopicIds: [] },
        ],
        edges: [{ fromIndex: 0, toIndex: 1, relation: "prerequisite" }],
      }),
    );
    await vi.waitFor(() => expect(onSaved).toHaveBeenCalledWith(response));
  });
});

describe("TaxonomyEditor: active-run guard (Task 12 fix)", () => {
  it("a structural edit's Save is disabled while a pipeline run is active, with a hint", () => {
    renderEditor({ pipelineActive: true });

    fireEvent.click(screen.getByText("+ Add topic")); // forces structural

    const saveButton = screen.getByRole("button", { name: /Save & re-classify/ });
    expect(saveButton.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText(/pipeline run is already active/i)).toBeTruthy();
  });

  it("a patch-only edit's Save stays enabled even while a pipeline run is active", () => {
    renderEditor({ pipelineActive: true });

    fireEvent.change(screen.getByDisplayValue("Intro"), { target: { value: "Introduction" } });

    const saveButton = screen.getByRole("button", { name: "Save" });
    expect(saveButton.hasAttribute("disabled")).toBe(false);
    expect(screen.queryByText(/pipeline run is already active/i)).toBeNull();
  });

  it("a structural edit's Save re-enables once pipelineActive goes false, with no hint shown up front", () => {
    renderEditor({ pipelineActive: false });

    fireEvent.click(screen.getByText("+ Add topic"));

    const saveButton = screen.getByRole("button", { name: /Save & re-classify/ });
    expect(saveButton.hasAttribute("disabled")).toBe(false);
    expect(screen.queryByText(/pipeline run is already active/i)).toBeNull();
  });
});
