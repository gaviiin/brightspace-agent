import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, getCourses: vi.fn() };
});

import { getCourses } from "../api/client";
import { CourseListPage } from "./CourseListPage";

const mockedGetCourses = vi.mocked(getCourses);

function renderPage(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  render(<CourseListPage />, { wrapper });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CourseListPage", () => {
  it("links to Settings even with no courses yet, since the pairing token lives there", async () => {
    mockedGetCourses.mockResolvedValue([]);
    renderPage();

    await screen.findByText(/No courses yet/);

    // The empty state tells the user to fetch the pairing token from Settings,
    // so this is the only route into it before a first course exists.
    expect(screen.getByRole("link", { name: "Settings" }).getAttribute("href")).toBe("/settings");
  });
});
