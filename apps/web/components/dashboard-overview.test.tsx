import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { DashboardOverview } from "./dashboard-overview";
import type { OverviewResponse } from "@/lib/types";

const baseOverview: OverviewResponse = {
  generated_at: "2026-07-20T12:00:00",
  provider_mode: "mock",
  accounts: [],
  platform_summaries: [
    {
      platform: "youtube",
      account_id: 1,
      handle: "@northwindstudio",
      display_name: "Northwind Studio",
      avatar_url: null,
      state: "connected",
      freshness: "fresh",
      followers_label: "Subscribers",
      followers: 4820,
      content_count: 12,
      views_28d: 91300,
      last_sync_at: "2026-07-20T10:00:00",
      top_content: [
        {
          id: 11,
          title: "Studio tour",
          content_type: "video",
          view_count: 12400,
          like_count: 810,
          comment_count: 42,
          published_at: "2026-07-12T09:00:00",
          permalink: "https://youtube.com/watch?v=abc",
        },
      ],
    },
    {
      platform: "instagram",
      account_id: null,
      handle: null,
      display_name: null,
      avatar_url: null,
      state: "not_connected",
      freshness: "never",
      followers_label: "Followers",
      followers: null,
      content_count: null,
      views_28d: null,
      last_sync_at: null,
      top_content: [],
    },
    {
      platform: "tiktok",
      account_id: null,
      handle: null,
      display_name: null,
      avatar_url: null,
      state: "not_configured",
      freshness: "never",
      followers_label: "Followers",
      followers: null,
      content_count: null,
      views_28d: null,
      last_sync_at: null,
      top_content: [],
    },
  ],
  inbox: {
    unanswered_total: 3,
    by_platform: { youtube: 3 },
    latest: [
      {
        id: 5,
        platform: "youtube",
        author_name: "Casey",
        text: "Loved the last upload!",
        published_at: "2026-07-20T08:00:00",
        status: "new",
      },
    ],
  },
  lineup: {
    next: [
      {
        id: 9,
        final_caption: "Morning at the studio",
        scheduled_publish_at: "2026-07-21T15:00:00",
        status: "internally_scheduled",
        image_url: "/media/preview.png",
      },
    ],
    needs_review: 2,
    in_lineup: 4,
    published_count: 30,
  },
  drafts: {
    scheduled_next: [
      { id: 3, title: "Launch teaser", scheduled_for: "2026-07-22T15:00:00", status: "scheduled" },
    ],
    counts: { draft: 1, scheduled: 1 },
  },
  recent_sync_runs: [
    {
      id: 4,
      account_id: 1,
      platform: "youtube",
      handle: "@northwindstudio",
      status: "completed",
      items_fetched: 12,
      error_summary: null,
      started_at: "2026-07-20T10:00:00",
    },
  ],
  warnings: [],
};

describe("DashboardOverview", () => {
  afterEach(cleanup);

  it("renders per-platform cards without blending totals", () => {
    render(<DashboardOverview overview={baseOverview} />);
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(screen.getByText("Northwind Studio")).toBeInTheDocument();
    expect(screen.getByText("Subscribers")).toBeInTheDocument();
    expect(screen.getByText("4.8K")).toBeInTheDocument();
    expect(screen.getByText("91.3K")).toBeInTheDocument();
    expect(screen.getByText("Not connected")).toBeInTheDocument();
    expect(screen.getByText("Not configured")).toBeInTheDocument();
    // Numbers are per-account only — no cross-platform sum tile exists.
    expect(screen.queryByText(/total followers/i)).not.toBeInTheDocument();
  });

  it("lists unanswered comments and upcoming items", () => {
    render(<DashboardOverview overview={baseOverview} />);
    expect(screen.getByText("Casey")).toBeInTheDocument();
    expect(screen.getByText(/Loved the last upload/)).toBeInTheDocument();
    expect(screen.getByText("Morning at the studio")).toBeInTheDocument();
    expect(screen.getByText("Launch teaser")).toBeInTheDocument();
    expect(screen.getByText("Studio tour")).toBeInTheDocument();
    expect(screen.getByText(/YouTube 3/)).toBeInTheDocument();
  });

  it("shows the all-caught-up empty state when nothing needs attention", () => {
    render(
      <DashboardOverview
        overview={{
          ...baseOverview,
          accounts: [
            {
              id: 1,
              platform: "youtube",
              external_account_id: "UC1",
              handle: "@northwindstudio",
              display_name: "Northwind Studio",
              avatar_url: null,
              profile_url: null,
              account_type: null,
              status: "connected",
              granted_scopes: [],
              scopes_source: null,
              connected_at: null,
              last_verified_at: null,
              last_sync_at: null,
              last_sync_status: null,
              last_error: null,
              freshness: "fresh",
              capabilities: [],
              latest_snapshot: null,
            },
          ],
          inbox: { unanswered_total: 0, by_platform: {}, latest: [] },
        }}
      />,
    );
    expect(screen.getByText("All caught up")).toBeInTheDocument();
  });
});
