import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, refresh: vi.fn() }),
}));

import { CrossPostDialog } from "./cross-post-dialog";
import type { PostOut } from "@/lib/v1-guards";

const platformsResponse = {
  provider_mode: "live",
  platforms: [
    {
      platform: "youtube",
      configured: true,
      required_env: [],
      capabilities: [],
      accounts: [
        {
          id: 5,
          platform: "youtube",
          external_account_id: "UC1",
          handle: "@nw",
          display_name: "Northwind",
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
        },
      ],
    },
  ],
};

const createdPost: PostOut = {
  id: 42,
  title: "Cross post",
  body: "",
  status: "draft",
  source: "proposal",
  source_ref: "7",
  ai_generated: false,
  version: 1,
  notes: null,
  scheduled_for: null,
  created_at: "2026-11-20T10:00:00Z",
  updated_at: "2026-11-20T10:00:00Z",
  created_by: null,
  media: [],
  targets: [],
};

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("CrossPostDialog", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    pushMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("lists connected accounts with a post type per pick and creates the post", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(platformsResponse))
      .mockResolvedValueOnce(jsonResponse(createdPost));
    render(
      <CrossPostDialog proposalId={7} proposalTitle="Fall look" onClose={vi.fn()} />,
    );

    await waitFor(() => expect(screen.getByText("Northwind")).toBeInTheDocument());
    const create = screen.getByRole("button", { name: /Create post for/ });
    expect(create).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "YouTube video" }));
    expect(create).toHaveTextContent("Create post for 1 account");
    expect(create).not.toBeDisabled();

    fireEvent.click(create);
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/posts/42"));
    const postCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/cross-post"),
    ) as [string, RequestInit];
    expect(postCall[0]).toBe(
      "http://127.0.0.1:8000/api/v1/proposals/7/cross-post",
    );
    expect(JSON.parse(String(postCall[1].body))).toEqual({
      targets: [
        { platform: "youtube", account_id: 5, post_type: "youtube_video" },
      ],
    });
  });
});
