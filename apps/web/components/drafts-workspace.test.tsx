import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DraftsWorkspace } from "./drafts-workspace";
import type { DraftView, PlatformsResponse } from "@/lib/types";

const platforms: PlatformsResponse = {
  provider_mode: "live",
  platforms: [
    {
      platform: "youtube",
      configured: true,
      required_env: ["RUNWAY_YOUTUBE_CLIENT_ID"],
      capabilities: [],
      accounts: [
        {
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
        },
      ],
    },
    {
      platform: "instagram",
      configured: true,
      required_env: ["RUNWAY_INSTAGRAM_APP_ID"],
      capabilities: [],
      accounts: [],
    },
    {
      platform: "tiktok",
      configured: false,
      required_env: ["RUNWAY_TIKTOK_CLIENT_KEY"],
      capabilities: [],
      accounts: [],
    },
  ],
};

const draft: DraftView = {
  id: 11,
  title: "Behind the scenes",
  body: "How the studio runs.",
  scheduled_for: "2026-07-25T14:00:00",
  status: "scheduled",
  notes: null,
  created_at: "2026-07-19T10:00:00",
  updated_at: "2026-07-19T10:00:00",
  targets: [
    {
      id: 21,
      platform: "youtube",
      account_id: 7,
      caption_override: null,
      status: "pending",
      published_content_id: null,
      published_url: null,
      published_at: null,
      error: null,
      publish_capability: {
        ready: false,
        state: "supported_not_implemented",
        detail: "Direct video publishing is not implemented in this release.",
        handoff_url: null,
      },
    },
    {
      id: 22,
      platform: "instagram",
      account_id: null,
      caption_override: null,
      status: "pending",
      published_content_id: null,
      published_url: null,
      published_at: null,
      error: null,
      publish_capability: {
        ready: false,
        state: "approval_required",
        detail: "Instagram publishing needs app review approval.",
        handoff_url: null,
      },
    },
  ],
};

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("DraftsWorkspace", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("creates a draft with platform targets and account binding", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(draft));
    render(
      <DraftsWorkspace
        platforms={platforms}
        drafts={[]}
        timezone="America/New_York"
      />,
    );
    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Behind the scenes" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", { name: /YouTube/i }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Create draft" }));
    await screen.findByText("Behind the scenes", {
      selector: ".draft-card h3",
    });
    const call = fetchMock.mock.calls[0];
    expect(call[0]).toContain("/api/drafts");
    const body = JSON.parse(call[1].body as string);
    expect(body.title).toBe("Behind the scenes");
    expect(body.targets).toEqual([
      { platform: "youtube", account_id: 7, caption_override: null },
    ]);
  });

  it("shows disabled Publish with the reason and the account handle", () => {
    render(
      <DraftsWorkspace
        platforms={platforms}
        drafts={[draft]}
        timezone="America/New_York"
      />,
    );
    const publishButtons = screen.getAllByRole("button", { name: "Publish" });
    expect(publishButtons[0]).toBeDisabled();
    expect(publishButtons[0]).toHaveAttribute(
      "title",
      "Direct video publishing is not implemented in this release.",
    );
    expect(screen.getAllByText(/Not implemented|Approval required/).length).toBe(2);
    expect(screen.getAllByText("@northwindstudio").length).toBeGreaterThan(0);
  });

  it("marks a target published through the URL + confirm flow", async () => {
    render(
      <DraftsWorkspace
        platforms={platforms}
        drafts={[draft]}
        timezone="America/New_York"
      />,
    );
    const markButtons = screen.getAllByRole("button", {
      name: "Mark as published",
    });
    fireEvent.click(markButtons[0]);
    const url = screen.getAllByPlaceholderText("YouTube post URL")[0];
    fireEvent.change(url, {
      target: { value: "https://youtu.be/abc12345" },
    });
    fireEvent.click(screen.getAllByRole("button", { name: "Confirm" })[0]);
    const confirmButton = await screen.findByRole("button", {
      name: "Confirm",
    });
    const published: DraftView = {
      ...draft,
      targets: [
        {
          ...draft.targets[0],
          status: "published",
          published_url: "https://youtu.be/abc12345",
          published_at: "2026-07-20T10:00:00Z",
          published_content_id: null,
        },
        draft.targets[1],
      ],
    };
    fetchMock.mockResolvedValueOnce(jsonResponse(published));
    fireEvent.click(confirmButton);
    await waitFor(() =>
      expect(
        screen.getAllByText(/not yet matched to imported content/i).length,
      ).toBeGreaterThan(0),
    );
    const call = fetchMock.mock.calls.at(-1)!;
    expect(call[0]).toContain("/api/drafts/11/targets/21/mark-published");
    expect(JSON.parse(call[1].body as string)).toEqual({
      published_url: "https://youtu.be/abc12345",
      confirmed: true,
    });
  });

  it("archives after a two-step confirm", async () => {
    render(
      <DraftsWorkspace
        platforms={platforms}
        drafts={[draft]}
        timezone="America/New_York"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Archive" }));
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ id: 11, status: "archived" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Confirm archive" }),
    );
    await waitFor(() =>
      expect(
        screen.queryByText("Behind the scenes", { selector: "h3" }),
      ).toBeNull(),
    );
    expect(fetchMock.mock.calls.at(-1)![1].method).toBe("DELETE");
  });
});
