import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AccountFreshness } from "./account-freshness";
import type { AccountView } from "@/lib/types";

class NoopEventSource {
  readyState = 0;
  onopen = null;
  onmessage = null;
  onerror = null;
  addEventListener() {}
  removeEventListener() {}
  close() {}
}

const account: AccountView = {
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
  capabilities: [],
  latest_snapshot: null,
};

const syncStatus = {
  push_enabled: true,
  quota: { "youtube.units": 42 },
  adaptive_sync_enabled: false,
  scopes: [
    {
      scope: "content",
      last_run_at: new Date().toISOString(),
      last_status: "success",
      last_error: null,
      next_run_at: null,
      boosted_until: null,
      source: "push",
    },
  ],
};

const capabilities = {
  account_id: 7,
  platform: "youtube",
  items: [
    {
      post_type: "youtube_video",
      state: "needs_scope",
      detail: "The upload scope is missing.",
      direct_adapter: false,
      default_publisher: null,
      missing_scopes: ["youtube.upload"],
      available_publishers: [],
    },
  ],
};

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("AccountFreshness", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", NoopEventSource);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows scope freshness and capability rows once expanded", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(syncStatus))
      .mockResolvedValueOnce(jsonResponse(capabilities));
    render(<AccountFreshness account={account} />);

    fireEvent.click(screen.getByRole("button", { name: "Freshness & publishing" }));

    await waitFor(() => expect(screen.getByText("Content")).toBeInTheDocument());
    expect(screen.getByText(/Push updates on/)).toBeInTheDocument();
    // Both the capability pill and the consent button carry this label.
    expect(screen.getAllByText("Enable publishing").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("The upload scope is missing.")).toBeInTheDocument();
    expect(screen.getByText(/youtube\.upload/)).toBeInTheDocument();
    const urls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(urls).toContain("http://127.0.0.1:8000/api/v1/accounts/7/sync-status");
    expect(urls).toContain(
      "http://127.0.0.1:8000/api/v1/accounts/7/publishing/capability",
    );
  });

  it("starts the incremental consent and follows the authorization URL", async () => {
    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({
      ...window.location,
      assign,
    } as Location);
    fetchMock
      .mockResolvedValueOnce(jsonResponse(syncStatus))
      .mockResolvedValueOnce(jsonResponse(capabilities))
      .mockResolvedValueOnce(
        jsonResponse({
          status: "consent_required",
          authorization_url: "https://accounts.example/consent",
          state: "xyz",
          scopes: ["youtube.upload"],
        }),
      );
    render(<AccountFreshness account={account} />);
    fireEvent.click(screen.getByRole("button", { name: "Freshness & publishing" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Enable publishing" })).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Enable publishing" }));
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("https://accounts.example/consent"),
    );
    const enableCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/publishing/enable"),
    ) as [string, RequestInit];
    expect(enableCall[1].method).toBe("POST");
  });
});
