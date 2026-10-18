import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ContentDetailView } from "./content-detail";
import type { ContentDetail, ContentItem } from "@/lib/types";

const base: ContentDetail = {
  id: 5,
  account_id: 7,
  platform: "youtube",
  external_id: "abc123",
  content_type: "video",
  title: "Studio tour",
  caption: "Inside the space.",
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
  metric_history: [
    { captured_at: "2026-07-18T10:00:00Z", metrics: { view_count: 1000 } },
    { captured_at: "2026-07-19T10:00:00Z", metrics: { view_count: 1240 } },
  ],
  comment_count_local: 3,
  group: null,
};

const candidate: ContentItem = {
  id: 9,
  account_id: 8,
  platform: "instagram",
  external_id: "xyz",
  content_type: "image",
  title: null,
  caption: "Studio tour on IG",
  permalink: null,
  thumbnail_url: null,
  published_at: "2026-07-10T12:00:00Z",
  duration_seconds: null,
  privacy_status: null,
  view_count: null,
  like_count: 50,
  comment_count: 2,
  share_count: null,
  metrics: {},
  metrics_fetched_at: null,
  group_id: null,
};

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("ContentDetailView", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders metrics, history note, comments, and version panels", () => {
    render(<ContentDetailView detail={base} />);
    expect(screen.getByRole("heading", { name: "Studio tour" })).toBeInTheDocument();
    expect(screen.getAllByText("Views over syncs").length).toBeGreaterThan(0);
    expect(screen.getByText("Inside the space.")).toBeInTheDocument();
    expect(screen.getByText("3 imported")).toBeInTheDocument();
    expect(screen.getByText("This is the only imported version")).toBeInTheDocument();
  });

  it("links a version by creating a group through the API", async () => {
    render(<ContentDetailView detail={base} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Link another version" }),
    );
    const input = screen.getByPlaceholderText("Search imported content…");
    fireEvent.change(input, { target: { value: "studio" } });
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        items: [candidate],
        total: 1,
        counts: { by_platform: { instagram: 1 }, by_type: { image: 1 } },
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    const link = await screen.findByRole("button", { name: "Link" });
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ id: 4, title: "Studio tour", content_ids: [5, 9] }),
    );
    fireEvent.click(link);
    await waitFor(() =>
      expect(screen.getByText(/2\s+versions/)).toBeInTheDocument(),
    );
    const calls = fetchMock.mock.calls;
    expect(calls[0][0]).toContain("/api/content?q=studio");
    expect(calls[1][0]).toContain("/api/content/groups");
    expect(calls[1][1].method).toBe("POST");
    expect(JSON.parse(calls[1][1].body as string)).toEqual({
      title: "Studio tour",
      content_ids: [5, 9],
    });
  });
});
