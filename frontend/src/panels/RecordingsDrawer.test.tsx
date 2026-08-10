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
    addMediaUrl: vi.fn(),
  };
});

import {
  addMediaUrl,
  ApiError,
  detectMedia,
  getMedia,
  processMedia,
  processMediaSource,
  updateMediaSource,
} from "../api/client";
import type { MediaHint, MediaListResponse, MediaSourceSummary } from "../api/types";
import { useUiStore } from "../state/uiStore";
import { RecordingsDrawer } from "./RecordingsDrawer";

const mockedGetMedia = vi.mocked(getMedia);
const mockedDetectMedia = vi.mocked(detectMedia);
const mockedProcessMedia = vi.mocked(processMedia);
const mockedProcessMediaSource = vi.mocked(processMediaSource);
const mockedUpdateMediaSource = vi.mocked(updateMediaSource);
const mockedAddMediaUrl = vi.mocked(addMediaUrl);

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
    hints: [],
    ...overrides,
  };
}

function hint(overrides: Partial<MediaHint> = {}): MediaHint {
  return { materialId: 50, title: "Mediasite Channel (Stern)", ...overrides };
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

describe("RecordingsDrawer: open recording link", () => {
  it("renders an external Open link to the row's source.url, regardless of status", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());

    renderDrawer();
    await screen.findByText("Lecture 1 (Mediasite)");

    // status "detected"
    const detectedLink = within(rowFor("Lecture 1 (Mediasite)")).getByRole("link", { name: /open/i });
    expect(detectedLink.getAttribute("href")).toBe("https://mediasite.example.edu/watch/1");
    expect(detectedLink.getAttribute("target")).toBe("_blank");
    expect(detectedLink.getAttribute("rel")).toContain("noopener");

    // status "done" -- still enabled, same as every other status.
    const doneLink = within(rowFor("Lecture 3 (Drive)")).getByRole("link", { name: /open/i });
    expect(doneLink.getAttribute("href")).toBe("https://drive.google.com/file/d/xyz");

    // status "skipped" -- still enabled.
    const skippedLink = within(rowFor("Lecture 4 (Zoom, skipped)")).getByRole("link", { name: /open/i });
    expect(skippedLink.getAttribute("href")).toBe("https://zoom.us/rec/share/def");
  });

  it("renders the Open link on a manually-added row too", async () => {
    mockedGetMedia.mockResolvedValue(
      fixtureMedia({
        sources: [
          source({
            id: 5,
            materialId: null,
            materialTitle: null,
            platform: "mediasite",
            url: "https://mediasite.example.edu/Mediasite/Play/manual",
          }),
        ],
      }),
    );

    renderDrawer();
    await screen.findByText("Added manually");
    const row = rowFor("Added manually");

    const link = within(row).getByRole("link", { name: /open/i });
    expect(link.getAttribute("href")).toBe("https://mediasite.example.edu/Mediasite/Play/manual");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
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

  it("keeps the typed passcode in the input when the save fails", async () => {
    // A 409 (another run started between opening the drawer and pressing
    // Save) is the realistic failure here. Silently reverting to the stored
    // passcode would make the retry the user is being told to do impossible
    // without retyping -- and would look like the save had succeeded.
    mockedGetMedia.mockResolvedValue(fixtureMedia());
    mockedUpdateMediaSource.mockRejectedValue(
      new ApiError(409, "a run is already active for this course"),
    );

    renderDrawer();
    await screen.findByText("Lecture 2 (Zoom)");

    const input = within(rowFor("Lecture 2 (Zoom)")).getByDisplayValue("1234");
    fireEvent.change(input, { target: { value: "5678" } });
    fireEvent.click(within(rowFor("Lecture 2 (Zoom)")).getByRole("button", { name: "Save" }));

    await screen.findByText("a run is already active for this course");
    const afterFailure = within(rowFor("Lecture 2 (Zoom)")).getByLabelText(
      "Passcode",
    ) as HTMLInputElement;
    expect(afterFailure.value).toBe("5678");
  });

  it("re-syncs from the server value after a successful save", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());
    mockedUpdateMediaSource.mockResolvedValue(source({ id: 2, passcode: "5678", status: "failed" }));

    renderDrawer();
    await screen.findByText("Lecture 2 (Zoom)");

    const input = within(rowFor("Lecture 2 (Zoom)")).getByDisplayValue("1234");
    fireEvent.change(input, { target: { value: "5678" } });
    fireEvent.click(within(rowFor("Lecture 2 (Zoom)")).getByRole("button", { name: "Save" }));

    await vi.waitFor(() => expect(mockedUpdateMediaSource).toHaveBeenCalled());
    // getMedia still reports "1234" (the fixture is static), so a
    // re-synced editor proves the dirty flag really was cleared on success.
    await vi.waitFor(() => {
      const field = within(rowFor("Lecture 2 (Zoom)")).getByLabelText("Passcode") as HTMLInputElement;
      expect(field.value).toBe("1234");
    });
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

describe("RecordingsDrawer: add URL", () => {
  it("Add calls addMediaUrl with the typed url and passcode", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());
    mockedAddMediaUrl.mockResolvedValue({ added: 1, skipped: 0, total: 1, sources: [] });

    renderDrawer();
    await screen.findByText("Lecture 1 (Mediasite)");

    fireEvent.change(screen.getByLabelText("Recording or channel URL"), {
      target: { value: "https://mediasite.example.edu/Mediasite/Play/xyz" },
    });
    fireEvent.change(screen.getByLabelText("Passcode (optional)"), { target: { value: "s3cret" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await vi.waitFor(() =>
      expect(mockedAddMediaUrl).toHaveBeenCalledWith(15, {
        url: "https://mediasite.example.edu/Mediasite/Play/xyz",
        passcode: "s3cret",
      }),
    );
  });

  it("an empty passcode field is sent as null", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());
    mockedAddMediaUrl.mockResolvedValue({ added: 1, skipped: 0, total: 1, sources: [] });

    renderDrawer();
    await screen.findByText("Lecture 1 (Mediasite)");

    fireEvent.change(screen.getByLabelText("Recording or channel URL"), {
      target: { value: "https://mediasite.example.edu/Mediasite/Play/xyz" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await vi.waitFor(() =>
      expect(mockedAddMediaUrl).toHaveBeenCalledWith(15, {
        url: "https://mediasite.example.edu/Mediasite/Play/xyz",
        passcode: null,
      }),
    );
  });

  it("shows the added count on success", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());
    mockedAddMediaUrl.mockResolvedValue({ added: 3, skipped: 0, total: 3, sources: [] });

    renderDrawer();
    await screen.findByText("Lecture 1 (Mediasite)");

    fireEvent.change(screen.getByLabelText("Recording or channel URL"), {
      target: { value: "https://mock.mediasite.example/mock-channel/full" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await screen.findByText("Added 3 recordings");
  });

  it("400/502 detail renders inline", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia());
    mockedAddMediaUrl.mockRejectedValue(
      new ApiError(400, "That URL wasn't recognized as a supported recording platform."),
    );

    renderDrawer();
    await screen.findByText("Lecture 1 (Mediasite)");

    fireEvent.change(screen.getByLabelText("Recording or channel URL"), {
      target: { value: "https://example.com/not-a-recording" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await screen.findByText("That URL wasn't recognized as a supported recording platform.");
  });

  it("disables the Add button while a run is active", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia({ active: true }));

    renderDrawer();
    await screen.findByText("Lecture 1 (Mediasite)");

    expect(screen.getByRole("button", { name: "Add" }).hasAttribute("disabled")).toBe(true);
  });
});

describe("RecordingsDrawer: LTI channel hints", () => {
  it("renders the hint title and instruction when hints are non-empty", async () => {
    mockedGetMedia.mockResolvedValue(
      fixtureMedia({ hints: [hint({ materialId: 50, title: "Mediasite Channel (Stern)" })] }),
    );

    renderDrawer();

    await screen.findByText("These look like recording channels the sync can't read:");
    expect(screen.getByText("Mediasite Channel (Stern)")).toBeTruthy();
    expect(
      screen.getByText(
        /Open it in Brightspace, copy the page URL from the embedded player/,
      ),
    ).toBeTruthy();
  });

  it("renders nothing when hints are empty", async () => {
    mockedGetMedia.mockResolvedValue(fixtureMedia({ hints: [] }));

    renderDrawer();
    await screen.findByText("Lecture 1 (Mediasite)");

    expect(screen.queryByText("These look like recording channels the sync can't read:")).toBeNull();
  });
});

describe("RecordingsDrawer: manually-added rows", () => {
  it("shows 'Added manually' when materialTitle is null", async () => {
    mockedGetMedia.mockResolvedValue(
      fixtureMedia({
        sources: [
          source({
            id: 5,
            materialId: null,
            materialTitle: null,
            platform: "mediasite",
            url: "https://mediasite.example.edu/Mediasite/Play/manual",
          }),
        ],
      }),
    );

    renderDrawer();

    expect(await screen.findByText("Added manually")).toBeTruthy();
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

    const detectedRow = rowFor("Lecture 1 (Mediasite)");
    expect(within(detectedRow).getByRole("button", { name: "Process" }).hasAttribute("disabled")).toBe(
      true,
    );
    expect(within(detectedRow).getByRole("button", { name: "Skip" }).hasAttribute("disabled")).toBe(true);

    const zoomFailedRow = rowFor("Lecture 2 (Zoom)");
    expect(within(zoomFailedRow).getByRole("button", { name: "Save" }).hasAttribute("disabled")).toBe(
      true,
    );

    const skippedRow = rowFor("Lecture 4 (Zoom, skipped)");
    expect(within(skippedRow).getByRole("button", { name: "Unskip" }).hasAttribute("disabled")).toBe(true);
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
