import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ContentLibrary } from "./content-library";
import type { ContentListResponse } from "@/lib/types";

const list: ContentListResponse = {
  items: [
    {
      id: 5,
      account_id: 7,
      platform: "youtube",
      external_id: "abc123",
      content_type: "video",
      title: "Studio tour",
      caption: "A look inside the studio",
      permalink: "https://youtube.com/watch?v=abc123",
      thumbnail_url: null,
      published_at: "2026-07-10T10:00:00Z",
      duration_seconds: 300,
      privacy_status: "public",
      view_count: 1240,
      like_count: 88,
      comment_count: 12,
      share_count: 4,
      metrics: {},
      metrics_fetched_at: "2026-07-19T10:00:00Z",
      group_id: null,
    },
  ],
  total: 60,
  counts: {
    by_platform: { youtube: 40, instagram: 20 },
    by_type: { video: 30, image: 30 },
  },
};

const filters = {
  platform: "",
  type: "",
  q: "",
  account: "",
  sort: "published_at",
  order: "desc",
  offset: 0,
};

describe("ContentLibrary", () => {
  afterEach(cleanup);

  it("renders totals, platform counts, and a result row", () => {
    render(
      <ContentLibrary
        accounts={[]}
        list={list}
        filters={filters}
        pageSize={50}
      />,
    );
    expect(screen.getByText("60")).toBeInTheDocument();
    expect(screen.getByText("Studio tour")).toBeInTheDocument();
    expect(screen.getAllByText("video").length).toBeGreaterThan(0);
  });

  it("renders sortable headers that toggle order in the URL", () => {
    render(
      <ContentLibrary
        accounts={[]}
        list={list}
        filters={filters}
        pageSize={50}
      />,
    );
    const views = screen.getByRole("link", { name: "Views" });
    expect(views).toHaveAttribute(
      "href",
      "/content?sort=view_count&order=desc",
    );
    const published = screen.getByRole("link", { name: /Published ↓/ });
    expect(published).toHaveAttribute(
      "href",
      "/content?sort=published_at&order=asc",
    );
  });

  it("renders Next pagination when more pages exist", () => {
    render(
      <ContentLibrary
        accounts={[]}
        list={list}
        filters={filters}
        pageSize={50}
      />,
    );
    expect(screen.getByRole("link", { name: "Next →" })).toHaveAttribute(
      "href",
      "/content?sort=published_at&order=desc&offset=50",
    );
    expect(screen.getByText("Page 1 of 2")).toBeInTheDocument();
  });

  it("keeps the empty state teachable", () => {
    render(
      <ContentLibrary
        accounts={[]}
        list={{ ...list, items: [], total: 0 }}
        filters={filters}
        pageSize={50}
      />,
    );
    expect(screen.getByText("No content imported yet")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open Accounts" }),
    ).toHaveAttribute("href", "/accounts");
  });
});
