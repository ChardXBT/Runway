import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AnalyticsWorkspace } from "./analytics-workspace";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));
import type { AccountAnalytics, AccountView } from "@/lib/types";

const youtubeAccount: AccountView = {
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

const tiktokAccount: AccountView = {
  ...youtubeAccount,
  id: 9,
  platform: "tiktok",
  handle: "northwind.tt",
};

const analytics: AccountAnalytics = {
  account: { id: 7, platform: "youtube", handle: "northwindstudio" },
  range: 28,
  kpis: [
    {
      key: "views",
      label: "Views",
      value: 12000,
      delta: 0.1,
      source: "YouTube Analytics",
      as_of: "2026-07-18",
    },
  ],
  series: [
    {
      metric: "views",
      label: "Views",
      points: [
        { day: "2026-07-01", value: 100 },
        { day: "2026-07-02", value: 200 },
      ],
    },
  ],
  snapshot_series: [],
  top_content: [
    {
      id: 5,
      platform: "youtube",
      content_type: "video",
      title: "Studio tour",
      caption: null,
      thumbnail_url: null,
      view_count: 400,
      like_count: 30,
      comment_count: 3,
      published_at: "2026-07-10T10:00:00Z",
      permalink: null,
    },
  ],
  notes: ["Recent days may be incomplete."],
  data_sources: [{ label: "YouTube Analytics", url: "https://example.com" }],
  latest_snapshot: null,
};

describe("AnalyticsWorkspace", () => {
  afterEach(cleanup);

  it("renders account and range segmented controls linking to URL params", () => {
    render(
      <AnalyticsWorkspace
        accounts={[youtubeAccount, tiktokAccount]}
        account={youtubeAccount}
        analytics={analytics}
        analyticsFailed={false}
        range="28"
      />,
    );
    const tiktok = screen.getByRole("link", { name: /northwind\.tt/i });
    expect(tiktok).toHaveAttribute("href", "/analytics?account=9&range=28");
    expect(screen.getByRole("link", { name: "90d" })).toHaveAttribute(
      "href",
      "/analytics?account=7&range=90",
    );
  });

  it("renders KPI tiles, primary chart, top content, and notes", () => {
    render(
      <AnalyticsWorkspace
        accounts={[youtubeAccount]}
        account={youtubeAccount}
        analytics={analytics}
        analyticsFailed={false}
        range="28"
      />,
    );
    expect(screen.getAllByText("Views").length).toBeGreaterThan(0);
    expect(screen.getByText("Studio tour")).toBeInTheDocument();
    expect(
      screen.getByText("Recent days may be incomplete."),
    ).toBeInTheDocument();
  });

  it("shows the not-connected state with no accounts", () => {
    render(
      <AnalyticsWorkspace
        accounts={[]}
        account={null}
        analytics={null}
        analyticsFailed={false}
        range="28"
      />,
    );
    expect(screen.getByText("No accounts connected")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open Accounts" }),
    ).toHaveAttribute("href", "/accounts");
  });

  it("offers Sync now when an account has no data", () => {
    const empty: AccountAnalytics = { ...analytics, series: [], kpis: [] };
    render(
      <AnalyticsWorkspace
        accounts={[youtubeAccount]}
        account={youtubeAccount}
        analytics={empty}
        analyticsFailed={false}
        range="28"
      />,
    );
    expect(screen.getByText("No data yet")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Sync now" }),
    ).toBeInTheDocument();
  });
});
