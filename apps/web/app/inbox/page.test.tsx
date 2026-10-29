import { afterEach, describe, expect, it, vi } from "vitest";

import InboxPage from "./page";
import type { AccountView } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

const account: AccountView = {
  id: 7,
  platform: "youtube",
  external_account_id: "UC1",
  handle: "northwindstudio",
  display_name: "Northwind Studio",
  avatar_url: null,
  profile_url: null,
  account_type: null,
  status: "connected",
  granted_scopes: [],
  scopes_source: "granted",
  connected_at: "2026-07-01T10:00:00",
  last_verified_at: null,
  last_sync_at: null,
  last_sync_status: null,
  last_error: null,
  freshness: "fresh",
  capabilities: [],
  latest_snapshot: null,
};

const platformsPayload = {
  provider_mode: "live",
  platforms: [
    {
      platform: "youtube",
      configured: true,
      required_env: [],
      capabilities: [],
      accounts: [account],
    },
  ],
};

const listPayload = {
  items: [],
  total: 0,
  counts: { unanswered: 0, new: 0 },
};

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

async function renderPage(id?: string) {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(jsonResponse(platformsPayload))
    .mockResolvedValueOnce(jsonResponse(listPayload));
  vi.stubGlobal("fetch", fetchMock);
  const element = await InboxPage({
    searchParams: Promise.resolve(id === undefined ? {} : { id }),
  });
  return { element, fetchMock };
}

describe("InboxPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("ignores a non-numeric id instead of fetching /api/inbox/NaN", async () => {
    const { element, fetchMock } = await renderPage("abc");
    expect(element.props.selectedId).toBeNull();
    expect(
      fetchMock.mock.calls.every(
        (call) => !String(call[0]).includes("/api/inbox/NaN"),
      ),
    ).toBe(true);
  });

  it("passes a valid integer id through to the workspace", async () => {
    const { element } = await renderPage("42");
    expect(element.props.selectedId).toBe(42);
  });

  it("treats a missing id as no selection", async () => {
    const { element } = await renderPage();
    expect(element.props.selectedId).toBeNull();
  });
});
