import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    getMedia: vi.fn(),
    detectMedia: vi.fn(),
    processMedia: vi.fn(),
    processMediaSource: vi.fn(),
    updateMediaSource: vi.fn(),
  };
});

import {
  ApiError,
  detectMedia,
  getMedia,
  processMedia,
  processMediaSource,
  updateMediaSource,
} from "../api/client";
import type { MediaListResponse, MediaSourceSummary } from "../api/types";
import { useUiStore } from "../state/uiStore";
import { RecordingsDrawer } from "./RecordingsDrawer";

const mockedGetMedia = vi.mocked(getMedia);
const mockedDetectMedia = vi.mocked(detectMedia);
const mockedProcessMedia = vi.mocked(processMedia);
const mockedProcessMediaSource = vi.mocked(processMediaSource);
const mockedUpdateMediaSource = vi.mocked(updateMediaSource);

function source(overrides: Partial<MediaSourceSummary> = {}): MediaSourceSummary {
  return {
    id: 1,
    materialId: 10,
    materialTitle: "Lecture 1",
    platform: "mediasite",
    url: "https://mediasite.example.edu/watch/1",
    passcode: null,
    status: "detected",
    error: null,
    transcriptMaterialId: null,
    updatedAt: "2026-08-06T22:28:46+00:00",
    ...overrides,
  };
}

function fixtureMedia(overrides: Partial<MediaListResponse> = {}): MediaListResponse {
  return {
    sources: [
      source({ id: 1, materialTitle: "Lecture 1 (Mediasite)", platform: "mediasite", status: "detected" }),
      source({
        id: 2,
        materialId: 11,
        materialTitle: "Lecture 2 (Zoom)",
        platform: "zoom",
        url: "https://zoom.us/rec/share/abc",
        passcode: "1234",
        status: "failed",
        error: "wrong_passcode: the passcode was rejected",
      }),
      source({
        id: 3,
        materialId: 12,
        materialTitle: "Lecture 3 (Drive)",
        platform: "gdrive",
        url: "https://drive.google.com/file/d/xyz",
        status: "done",
        transcriptMaterialId: 99,
      }),
      source({
        id: 4,
        materialId: 13,
        materialTitle: "Lecture 4 (Zoom, skipped)",
        platform: "zoom",
        url: "https://zoom.us/rec/share/def",
        status: "skipped",
      }),
    ],
    active: false,
    ...overrides,
  };
}

function renderDrawer(onClose = vi.fn()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }
  render(<RecordingsDrawer courseId={15} onClose={onClose} />, { wrapper: Wrapper });
  return onClose;
}

function rowFor(materialTitle: string): HTMLElement {
  const titleNode = screen.getByText(materialTitle);
  const row = titleNode.closest("li");
  if (!row) throw new Error(`no row found for ${materialTitle}`);
  return row as HTMLElement;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  useUiStore.setState({ selection: null, expandedTopicIds: new Set() });
});

describe("RecordingsDrawer: rows", () => {
  it("renders each row's platform, title, and status badge", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());

    renderDrawer();

    await screen.findByText("Lecture 1 (Mediasite)");
    expect(within(rowFor("Lecture 1 (Mediasite)")).getByText("Mediasite")).toBeTruthy();
    expect(within(rowFor("Lecture 1 (Mediasite)")).getByText("detected")).toBeTruthy();
    expect(within(rowFor("Lecture 2 (Zoom)")).getByText("Zoom")).toBeTruthy();
    expect(within(rowFor("Lecture 3 (Drive)")).getByText("Drive")).toBeTruthy();
    expect(within(rowFor("Lecture 3 (Drive)")).getByText("done")).toBeTruthy();
    expect(within(rowFor("Lecture 4 (Zoom, skipped)")).getByText("skipped")).toBeTruthy();
  });

  it("shows a failed row's error text", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());

    renderDrawer();

    await screen.findByText("Lecture 2 (Zoom)");
    expect(screen.getByText("wrong_passcode: the passcode was rejected")).toBeTruthy();
  });
});

describe("RecordingsDrawer: passcode", () => {
  it("saves a typed passcode via updateMediaSource", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());
    mockedUpdateMediaSource.mockResolvedValue(source({ id: 2, passcode: "5678", status: "failed" }));

    renderDrawer();
    await screen.findByText("Lecture 2 (Zoom)");
    const row = rowFor("Lecture 2 (Zoom)");

    const input = within(row).getByDisplayValue("1234");
    fireEvent.change(input, { target: { value: "5678" } });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    await vi.waitFor(() =>
      expect(mockedUpdateMediaSource).toHaveBeenCalledWith(2, { passcode: "5678" }),
    );
  });

  it("saving an emptied passcode field sends null", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());
    mockedUpdateMediaSource.mockResolvedValue(source({ id: 2, passcode: null, status: "failed" }));

    renderDrawer();
    await screen.findByText("Lecture 2 (Zoom)");
    const row = rowFor("Lecture 2 (Zoom)");

    const input = within(row).getByDisplayValue("1234");
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    await vi.waitFor(() =>
      expect(mockedUpdateMediaSource).toHaveBeenCalledWith(2, { passcode: null }),
    );
  });

  it("does not render a passcode editor for non-Zoom platforms", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());

    renderDrawer();
    await screen.findByText("Lecture 1 (Mediasite)");
    const row = rowFor("Lecture 1 (Mediasite)");

    expect(within(row).queryByRole("button", { name: "Save" })).toBeNull();
  });
});

describe("RecordingsDrawer: detect", () => {
  it("Detect calls detectMedia(courseId)", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());
    mockedDetectMedia.mockResolvedValue({ scannedMaterials: 12, found: 3, added: 1 });

    renderDrawer();
    await screen.findByText("Lecture 1 (Mediasite)");

    fireEvent.click(screen.getByRole("button", { name: "Detect" }));

    await vi.waitFor(() => expect(mockedDetectMedia).toHaveBeenCalledWith(15));
  });
});

describe("RecordingsDrawer: process", () => {
  it("Process all calls processMedia(courseId)", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());
    mockedProcessMedia.mockResolvedValue({ runToken: 5 });

    renderDrawer();
    await screen.findByText("Lecture 1 (Mediasite)");

    fireEvent.click(screen.getByRole("button", { name: "Process all" }));

    await vi.waitFor(() => expect(mockedProcessMedia).toHaveBeenCalledWith(15));
  });

  it("a per-row Process button calls processMediaSource(sourceId)", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());
    mockedProcessMediaSource.mockResolvedValue({ runToken: 6 });

    renderDrawer();
    await screen.findByText("Lecture 1 (Mediasite)");
    const row = rowFor("Lecture 1 (Mediasite)");

    fireEvent.click(within(row).getByRole("button", { name: "Process" }));

    await vi.waitFor(() => expect(mockedProcessMediaSource).toHaveBeenCalledWith(1));
  });

  it("disables Process all and per-row actions while a run is active", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia({ active: true }));

    renderDrawer();
    await screen.findByText("Lecture 1 (Mediasite)");

    expect(screen.getByRole("button", { name: "Process all" }).hasAttribute("disabled")).toBe(true);
  });

  it("does not offer Process/Skip on a done row, or Process on a skipped row", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());

    renderDrawer();
    await screen.findByText("Lecture 3 (Drive)");

    const doneRow = rowFor("Lecture 3 (Drive)");
    expect(within(doneRow).queryByRole("button", { name: "Process" })).toBeNull();
    expect(within(doneRow).queryByRole("button", { name: "Skip" })).toBeNull();

    const skippedRow = rowFor("Lecture 4 (Zoom, skipped)");
    expect(within(skippedRow).queryByRole("button", { name: "Process" })).toBeNull();
  });

  it("409 from processMedia renders the detail inline", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());
    mockedProcessMedia.mockRejectedValue(new ApiError(409, "a run is already active for this course"));

    renderDrawer();
    await screen.findByText("Lecture 1 (Mediasite)");

    fireEvent.click(screen.getByRole("button", { name: "Process all" }));

    await screen.findByText("a run is already active for this course");
  });
});

describe("RecordingsDrawer: skip / unskip", () => {
  it("Skip calls updateMediaSource with status 'skipped'", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());
    mockedUpdateMediaSource.mockResolvedValue(source({ id: 1, status: "skipped" }));

    renderDrawer();
    await screen.findByText("Lecture 1 (Mediasite)");
    const row = rowFor("Lecture 1 (Mediasite)");

    fireEvent.click(within(row).getByRole("button", { name: "Skip" }));

    await vi.waitFor(() =>
      expect(mockedUpdateMediaSource).toHaveBeenCalledWith(1, { status: "skipped" }),
    );
  });

  it("Unskip calls updateMediaSource with status 'detected'", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());
    mockedUpdateMediaSource.mockResolvedValue(source({ id: 4, status: "detected" }));

    renderDrawer();
    await screen.findByText("Lecture 4 (Zoom, skipped)");
    const row = rowFor("Lecture 4 (Zoom, skipped)");

    fireEvent.click(within(row).getByRole("button", { name: "Unskip" }));

    await vi.waitFor(() =>
      expect(mockedUpdateMediaSource).toHaveBeenCalledWith(4, { status: "detected" }),
    );
  });
});

describe("RecordingsDrawer: transcript ready", () => {
  it("selects the transcript material in the uiStore", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());

    renderDrawer();
    await screen.findByText("Lecture 3 (Drive)");
    const row = rowFor("Lecture 3 (Drive)");

    fireEvent.click(within(row).getByRole("button", { name: /transcript/i }));

    expect(useUiStore.getState().selection).toEqual({ type: "material", id: 99 });
  });

  it("does not show a transcript affordance when transcriptMaterialId is null", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());

    renderDrawer();
    await screen.findByText("Lecture 1 (Mediasite)");
    const row = rowFor("Lecture 1 (Mediasite)");

    expect(within(row).queryByRole("button", { name: /transcript/i })).toBeNull();
  });
});

describe("RecordingsDrawer: empty and error states", () => {
  it("shows the empty-state hint when there are no sources", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia({ sources: [] }));

    renderDrawer();

    await screen.findByText("No recordings detected yet.");
    expect(screen.getByText("Sync the course, then Scan.")).toBeTruthy();
  });

  it("shows an error message when the request fails", async () => {
    mockedGetMedia.mockRejectedValue(new Error("boom"));

    renderDrawer();

    expect(await screen.findByText("Couldn't load recordings.")).toBeTruthy();
  });

  it("calls onClose from the close button", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());
    const onClose = renderDrawer();

    fireEvent.click(await screen.findByRole("button", { name: "Close" }));

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
