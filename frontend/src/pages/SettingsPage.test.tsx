import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, getSettings: vi.fn(), getPairPending: vi.fn(), approvePair: vi.fn() };
});

import { approvePair, getPairPending, getSettings } from "../api/client";
import type { SettingsResponse } from "../api/types";
import { SettingsPage } from "./SettingsPage";

const mockedGetSettings = vi.mocked(getSettings);
const mockedGetPairPending = vi.mocked(getPairPending);
const mockedApprovePair = vi.mocked(approvePair);

function settingsFixture(overrides: Partial<SettingsResponse> = {}): SettingsResponse {
  return {
    pairingToken: "tok-abc123",
    dataDir: "/home/user/.brightspace-agent",
    models: { fast: "claude-fast", smart: "claude-smart" },
    mockLlm: true,
    maxCostUsdPerRun: 5,
    apiKeyConfigured: false,
    ...overrides,
  };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    );
  }
  render(<SettingsPage />, { wrapper: Wrapper });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const BANNER_TEXT = /asking to connect/;

describe("SettingsPage: pairing-request banner", () => {
  it("renders the banner and an Approve button when a pairing request is pending", async () => {
    mockedGetSettings.mockResolvedValue(settingsFixture());
    mockedGetPairPending.mockResolvedValue({ pending: true });

    renderPage();

    await screen.findByText(BANNER_TEXT);
    expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
  });

  it("renders no banner when nothing is pending", async () => {
    mockedGetSettings.mockResolvedValue(settingsFixture());
    mockedGetPairPending.mockResolvedValue({ pending: false });

    renderPage();

    await screen.findByText("Settings");
    expect(screen.queryByText(BANNER_TEXT)).toBeNull();
  });

  it("clicking Approve POSTs the approval", async () => {
    mockedGetSettings.mockResolvedValue(settingsFixture());
    mockedGetPairPending.mockResolvedValue({ pending: true });
    mockedApprovePair.mockResolvedValue({ approved: true });

    renderPage();
    await screen.findByText(BANNER_TEXT);

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await vi.waitFor(() => expect(mockedApprovePair).toHaveBeenCalledTimes(1));
  });

  it("the banner disappears once the approval is reflected back as not-pending", async () => {
    mockedGetSettings.mockResolvedValue(settingsFixture());
    mockedGetPairPending.mockResolvedValueOnce({ pending: true }).mockResolvedValue({ pending: false });
    mockedApprovePair.mockResolvedValue({ approved: true });

    renderPage();
    await screen.findByText(BANNER_TEXT);

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await vi.waitFor(() => expect(screen.queryByText(BANNER_TEXT)).toBeNull());
  });

  it("banner does not block the rest of the page from rendering while pending", async () => {
    mockedGetSettings.mockResolvedValue(settingsFixture());
    mockedGetPairPending.mockResolvedValue({ pending: true });

    renderPage();

    await screen.findByText(BANNER_TEXT);
    expect(screen.getByText("tok-abc123")).toBeTruthy();
  });
});
