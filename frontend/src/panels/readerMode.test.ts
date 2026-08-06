import { describe, expect, it } from "vitest";

import { chooseReaderMode } from "./readerMode";

describe("chooseReaderMode: pdf-iframe", () => {
  it("a pdf mime always wins, regardless of kind/status", () => {
    expect(
      chooseReaderMode({ kind: "document", status: "extracted", mime: "application/pdf" }),
    ).toBe("pdf-iframe");
  });

  it("precedence: pdf mime beats the text kind/status rule", () => {
    // Without the mime check, kind=document + status=extracted would hit
    // the "text" branch below -- pdf mime must take priority over it.
    expect(
      chooseReaderMode({ kind: "slides", status: "summarized", mime: "application/pdf" }),
    ).toBe("pdf-iframe");
  });

  it("precedence: pdf mime beats the link-kind rule", () => {
    expect(chooseReaderMode({ kind: "link", status: "fetched", mime: "application/pdf" })).toBe(
      "pdf-iframe",
    );
  });
});

describe("chooseReaderMode: text", () => {
  it.each([
    "document",
    "transcript",
    "announcement",
    "assignment",
    "syllabus",
    "slides",
  ] as const)("kind=%s with status=extracted -> text", (kind) => {
    expect(chooseReaderMode({ kind, status: "extracted", mime: null })).toBe("text");
  });

  it("status=summarized also renders as text", () => {
    expect(chooseReaderMode({ kind: "document", status: "summarized", mime: null })).toBe("text");
  });

  it("a text-bearing kind that hasn't been extracted yet is not text mode", () => {
    expect(chooseReaderMode({ kind: "document", status: "fetched", mime: null })).not.toBe(
      "text",
    );
  });

  it("slides/pptx has no browser-native preview, so it renders extracted text once summarized", () => {
    expect(
      chooseReaderMode({ kind: "slides", status: "summarized", mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation" }),
    ).toBe("text");
  });
});

describe("chooseReaderMode: link-out", () => {
  it("kind=link -> link-out", () => {
    expect(chooseReaderMode({ kind: "link", status: "fetched", mime: null })).toBe("link-out");
  });

  it("precedence: link kind beats the download-with-blob rule", () => {
    expect(chooseReaderMode({ kind: "link", status: "fetched", mime: "text/html" })).toBe(
      "link-out",
    );
  });
});

describe("chooseReaderMode: video-link", () => {
  it("kind=video -> video-link", () => {
    expect(chooseReaderMode({ kind: "video", status: "fetched", mime: null })).toBe("video-link");
  });

  it("kind=video wins over the download rule even with a blob present", () => {
    expect(chooseReaderMode({ kind: "video", status: "extracted", mime: "video/mp4" })).toBe(
      "video-link",
    );
  });
});

describe("chooseReaderMode: download", () => {
  it("a blob with no other matching rule -> download", () => {
    expect(
      chooseReaderMode({ kind: "document", status: "fetched", mime: "application/octet-stream" }),
    ).toBe("download");
  });

  it("kind=other with a blob -> download", () => {
    expect(chooseReaderMode({ kind: "other", status: "fetched", mime: "image/png" })).toBe(
      "download",
    );
  });
});

describe("chooseReaderMode: none", () => {
  it("no blob, not a text/link/video case -> none", () => {
    expect(chooseReaderMode({ kind: "document", status: "fetched", mime: null })).toBe("none");
  });

  it("kind=other with no blob -> none", () => {
    expect(chooseReaderMode({ kind: "other", status: "fetched", mime: null })).toBe("none");
  });
});
