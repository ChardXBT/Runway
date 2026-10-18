import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AccountsManager } from "./accounts-manager";
import type { AccountView, PlatformsResponse } from "@/lib/types";

const youtubeAccount: AccountView = {
  id: 7,
  platform: "youtube",
  external_account_id: "UC123",
  handle: "@northwindstudio",
  display_name: "Northwind Studio",
  avatar_url: null,
  profile_url: "https://youtube.com/@northwindstudio",
  account_type: null,
  status: "connected",
  granted_scopes: ["youtube.readonly"],
  scopes_source: "granted",
  connected_at: "2026-07-01T10:00:00",
  last_verified_at: "2026-07-19T10:00:00",
  last_sync_at: new Date().toISOString(),
  last_sync_status: "completed",
  last_error: null,
  freshness: "fresh",
  capabilities: [
    {
      capability: "account_analytics",
      state: "implemented",
      ready: true,
      missing_scopes: [],
      blocked_by_setting: false,
      detail: "Channel analytics",
    },
  ],
  latest_snapshot: null,
};

const baseData: PlatformsResponse = {
  provider_mode: "live",
  platforms: [
    {
      platform: "youtube",
      configured: true,
      required_env: ["RUNWAY_YOUTUBE_CLIENT_ID", "RUNWAY_YOUTUBE_CLIENT_SECRET"],
      capabilities: youtubeAccount.capabilities,
      accounts: [youtubeAccount],
    },
    {
      platform: "instagram",
      configured: false,
      required_env: ["RUNWAY_INSTAGRAM_APP_ID", "RUNWAY_INSTAGRAM_APP_SECRET"],
      capabilities: [],
      accounts: [],
    },
    {
      platform: "tiktok",
      configured: true,
      required_env: ["RUNWAY_TIKTOK_CLIENT_KEY", "RUNWAY_TIKTOK_CLIENT_SECRET"],
      capabilities: [],
      accounts: [],
    },
  ],
};

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("AccountsManager", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders platform sections, capabilities, and the account card", () => {
    render(<AccountsManager data={baseData} connected={null} errorCode={null} />);
    expect(screen.getByText("YouTube", { selector: ".platform-badge-label" })).toBeInTheDocument();
    expect(screen.getByText("Northwind Studio")).toBeInTheDocument();
    expect(screen.getByText("@northwindstudio")).toBeInTheDocument();
    expect(screen.getByText("instagram is not configured".replace("instagram", "Instagram"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect TikTok" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sync now" })).toBeInTheDocument();
    expect(screen.getByText("youtube.readonly")).toBeInTheDocument();
  });

  it("maps oauth error codes to human copy", () => {
    render(
      <AccountsManager data={baseData} connected={null} errorCode="denied" />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/declined/);
  });

  it("shows a success notice for ?connected=", () => {
    render(
      <AccountsManager data={baseData} connected="youtube" errorCode={null} />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(/YouTube connected/);
  });

  it("connect posts to the connect endpoint and assigns the location", async () => {
    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({
      ...window.location,
      assign,
    } as Location);
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ authorization_url: "https://accounts.example/auth", state: "abc" }),
    );
    render(<AccountsManager data={baseData} connected={null} errorCode={null} />);
    fireEvent.click(screen.getByRole("button", { name: "Connect TikTok" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://accounts.example/auth"));
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/platforms/tiktok/connect",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("keeps connect disabled while a connect request is in flight", () => {
    fetchMock.mockReturnValueOnce(new Promise(() => {}));
    render(<AccountsManager data={baseData} connected={null} errorCode={null} />);
    const button = screen.getByRole("button", { name: "Connect TikTok" });
    fireEvent.click(button);
    expect(button).toBeDisabled();
  });

  it("syncs and polls the run until completion", async () => {
    vi.useFakeTimers();
    try {
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ run_id: 42 }, 202))
        .mockResolvedValue(
          jsonResponse({
            id: 42,
            account_id: 7,
            trigger: "manual",
            status: "completed",
            started_at: null,
            completed_at: "2026-07-20T12:00:00",
            steps: [
              { step: "identity", status: "completed", items: 1, error: null, quota_units: 0 },
            ],
            items_fetched: 12,
            quota_units_estimate: 0,
            error_summary: null,
          }),
        );
      render(<AccountsManager data={baseData} connected={null} errorCode={null} />);
      fireEvent.click(screen.getByRole("button", { name: "Sync now" }));
      await vi.waitFor(() =>
        expect(fetchMock.mock.calls[0][0]).toContain("/accounts/7/sync"),
      );
      await vi.advanceTimersByTimeAsync(1600);
      await vi.waitFor(() =>
        expect(screen.getByText(/Sync completed/)).toBeInTheDocument(),
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("requires a second click to disconnect", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ account_id: 7, status: "disconnected" }),
    );
    render(<AccountsManager data={baseData} connected={null} errorCode={null} />);
    const button = screen.getByRole("button", { name: "Disconnect" });
    fireEvent.click(button);
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole("button", { name: "Confirm disconnect" }),
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toContain("/accounts/7/disconnect");
    await waitFor(() =>
      expect(screen.getByText(/Account disconnected/)).toBeInTheDocument(),
    );
  });

  it("hides connect actions in mock mode and labels demo accounts", () => {
    render(
      <AccountsManager
        data={{
          ...baseData,
          provider_mode: "mock",
          platforms: baseData.platforms.map((entry) => ({
            ...entry,
            accounts: entry.platform === "youtube" ? [youtubeAccount] : [],
          })),
        }}
        connected={null}
        errorCode={null}
      />,
    );
    expect(screen.getByText(/Demo account/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Connect / }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reconnect" })).not.toBeInTheDocument();
  });
});
