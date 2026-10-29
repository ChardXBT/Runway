import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
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

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

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

  it("renders the empty state, not a permanent loader, for a 404", () => {
    render(
      <AnalyticsWorkspace
        accounts={[youtubeAccount]}
        account={youtubeAccount}
        analytics={null}
        analyticsFailed={false}
        range="28"
      />,
    );
    expect(screen.getByText("No data yet")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Sync now" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Loading analytics")).toBeNull();
  });

  it("renders the error state when the analytics request failed", () => {
    render(
      <AnalyticsWorkspace
        accounts={[youtubeAccount]}
        account={youtubeAccount}
        analytics={null}
        analyticsFailed={true}
        range="28"
      />,
    );
    expect(
      screen.getByText("Analytics could not be loaded"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Loading analytics")).toBeNull();
    expect(screen.queryByText("No data yet")).toBeNull();
  });

  it("labels the latest day-only point without a fake time", () => {
    render(
      <AnalyticsWorkspace
        accounts={[youtubeAccount]}
        account={youtubeAccount}
        analytics={analytics}
        analyticsFailed={false}
        range="28"
      />,
    );
    expect(screen.getByText("Data through Jul 2")).toBeInTheDocument();
  });

  it("polls a queued sync run until it completes", async () => {
    vi.useFakeTimers();
    try {
      const run = {
        id: 3,
        account_id: 7,
        trigger: "manual",
        status: "queued",
        started_at: null,
        completed_at: null,
        steps: [],
        items_fetched: 0,
        quota_units_estimate: 0,
        error_summary: null,
      };
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(jsonResponse(run))
        .mockResolvedValueOnce(jsonResponse({ ...run, status: "running" }))
        .mockResolvedValueOnce(jsonResponse({ ...run, status: "completed" }));
      vi.stubGlobal("fetch", fetchMock);
      const empty: AccountAnalytics = {
        ...analytics,
        series: [],
        snapshot_series: [],
        top_content: [],
        kpis: [],
      };
      render(
        <AnalyticsWorkspace
          accounts={[youtubeAccount]}
          account={youtubeAccount}
          analytics={empty}
          analyticsFailed={false}
          range="28"
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: "Sync now" }));

      await vi.advanceTimersByTimeAsync(1600);
      await vi.advanceTimersByTimeAsync(1600);
      await vi.waitFor(() =>
        expect(
          screen.getByText("Sync complete. Refreshing analytics."),
        ).toBeInTheDocument(),
      );
      expect(fetchMock).toHaveBeenCalledTimes(3);
      expect(fetchMock.mock.calls[1][0]).toContain("/api/platforms/sync-runs/3");
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });

  it("stops polling a sync run after unmount", async () => {
    vi.useFakeTimers();
    try {
      const run = {
        id: 3,
        account_id: 7,
        trigger: "manual",
        status: "queued",
        started_at: null,
        completed_at: null,
        steps: [],
        items_fetched: 0,
        quota_units_estimate: 0,
        error_summary: null,
      };
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(jsonResponse(run))
        .mockResolvedValue(jsonResponse({ ...run, status: "running" }));
      vi.stubGlobal("fetch", fetchMock);
      const empty: AccountAnalytics = {
        ...analytics,
        series: [],
        snapshot_series: [],
        top_content: [],
        kpis: [],
      };
      const { unmount } = render(
        <AnalyticsWorkspace
          accounts={[youtubeAccount]}
          account={youtubeAccount}
          analytics={empty}
          analyticsFailed={false}
          range="28"
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: "Sync now" }));

      await vi.advanceTimersByTimeAsync(1600);
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
      unmount();
      await vi.advanceTimersByTimeAsync(10_000);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });
});
