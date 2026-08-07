// Focused tests for the M3.3 enrichment client fns -- URL/method/header
// wiring and response-shape parsing. Other client.ts fns (getCourses,
// putTaxonomy, ...) are already exercised indirectly through the page/panel
// tests that mock this module, so this file only covers what's new here.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  enrichCourse,
  enrichDryRun,
  enrichTopic,
  getTopicEnrichment,
  setEnrichmentStatus,
} from "./client";
import type { EnrichDryRunResponse, EnrichmentResource, TopicEnrichment } from "./types";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "",
    json: async () => body,
  } as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("getTopicEnrichment", () => {
  it("GETs /api/topics/{id}/enrichment and parses the shape verbatim", async () => {
    const body: TopicEnrichment = {
      topicId: 5,
      resources: [
        {
          id: 1,
          url: "https://example.edu/a",
          title: "A",
          resourceType: "video",
          intent: "video_lecture",
          rationale: "Covers the same idea a different way.",
          scores: { relevance: 0.9 },
          verification: { ok: true },
          rank: 1,
          shared: false,
          status: "suggested",
        },
      ],
      meta: { suggested: 1, kept: 0, dismissed: 0 },
    };
    fetchMock.mockResolvedValue(jsonResponse(body));

    const result = await getTopicEnrichment(5);

    expect(fetchMock).toHaveBeenCalledWith("/api/topics/5/enrichment", undefined);
    expect(result).toEqual(body);
  });
});

describe("enrichTopic", () => {
  it("POSTs /api/topics/{id}/enrich with the CSRF header and no body", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ runToken: 42 }));

    const result = await enrichTopic(5);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/topics/5/enrich");
    expect(init).toMatchObject({ method: "POST", headers: { "X-BSA-Request": "1" } });
    expect(result).toEqual({ runToken: 42 });
  });
});

describe("enrichCourse", () => {
  it("POSTs /api/courses/{id}/enrich with the CSRF header", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ runToken: 7 }));

    const result = await enrichCourse(3);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/courses/3/enrich");
    expect(init).toMatchObject({ method: "POST", headers: { "X-BSA-Request": "1" } });
    expect(result).toEqual({ runToken: 7 });
  });
});

describe("setEnrichmentStatus", () => {
  it("PUTs /api/enrichment/{id} with the CSRF header and the status body", async () => {
    const updated: EnrichmentResource = {
      id: 9,
      url: "https://example.edu/b",
      title: "B",
      resourceType: "article",
      intent: "university_notes",
      rationale: "Notes from a university course.",
      scores: {},
      verification: {},
      rank: 2,
      shared: false,
      status: "kept",
    };
    fetchMock.mockResolvedValue(jsonResponse(updated));

    const result = await setEnrichmentStatus(9, "kept");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/enrichment/9");
    expect(init).toMatchObject({
      method: "PUT",
      headers: { "X-BSA-Request": "1", "Content-Type": "application/json" },
    });
    expect(JSON.parse(init.body as string)).toEqual({ status: "kept" });
    expect(result).toEqual(updated);
  });
});

describe("enrichDryRun", () => {
  it("GETs /api/courses/{id}/enrich/dry-run without a CSRF header and parses the estimate", async () => {
    const body: EnrichDryRunResponse = {
      topicsNeedingEnrichment: 4,
      callsPerTopic: 12,
      estCostPerTopicUsd: 0.05,
      totalEstCostUsd: 0.2,
    };
    fetchMock.mockResolvedValue(jsonResponse(body));

    const result = await enrichDryRun(3);

    expect(fetchMock).toHaveBeenCalledWith("/api/courses/3/enrich/dry-run", undefined);
    expect(result).toEqual(body);
  });
});
