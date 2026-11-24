import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PostsWorkspace } from "./posts-workspace";
import type { PostOut, TargetOut } from "@/lib/v1-guards";

class NoopEventSource {
  readyState = 0;
  onopen = null;
  onmessage = null;
  onerror = null;
  addEventListener() {}
  removeEventListener() {}
  close() {}
}

function target(overrides: Partial<TargetOut> = {}): TargetOut {
  return {
    id: 11,
    post_id: 7,
    platform: "youtube",
    post_type: "youtube_video",
    account_id: 5,
    caption: "Caption",
    caption_override: null,
    state: "published",
    state_reason: null,
    version: 3,
    payload_hash: "b".repeat(64),
    payload: {},
    settings: {},
    effective_settings: {},
    issues: [],
    releasable: false,
    privacy_in_words: null,
    release_notice: null,
    release: { state: "released", payload_hash: "b".repeat(64), released_at: null, released_by: "you" },
    planned_at: "2026-11-25T15:00:00Z",
    publisher: null,
    capability: null,
    external_id: null,
    last_attempt: null,
    published_at: null,
    published_url: null,
    ...overrides,
  };
}

function post(overrides: Partial<PostOut> = {}): PostOut {
  return {
    id: 7,
    title: "Launch clip",
    body: "Body",
    status: "scheduled",
    source: "manual",
    source_ref: null,
    ai_generated: false,
    version: 2,
    notes: null,
    scheduled_for: null,
    created_at: "2026-11-01T10:00:00Z",
    updated_at: "2026-11-20T10:00:00Z",
    created_by: null,
    media: [],
    targets: [target()],
    ...overrides,
  };
}

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("PostsWorkspace", () => {
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

  it("lists posts with per-target state pills", () => {
    render(
      <PostsWorkspace
        initialPage={{ items: [post()], next_cursor: null }}
      />,
    );
    expect(screen.getByText("Launch clip")).toBeInTheDocument();
    expect(screen.getByText("Scheduled")).toBeInTheDocument();
    expect(screen.getByText("YouTube video")).toBeInTheDocument();
    expect(screen.getByText("Published")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "New post" })).toHaveAttribute(
      "href",
      "/posts/new",
    );
  });

  it("shows the empty panel when there are no posts", () => {
    render(<PostsWorkspace initialPage={{ items: [], next_cursor: null }} />);
    expect(screen.getByText("No posts yet")).toBeInTheDocument();
  });

  it("loads the next cursor page on demand", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        items: [post({ id: 8, title: "Second post", targets: [] })],
        next_cursor: null,
      }),
    );
    render(
      <PostsWorkspace
        initialPage={{ items: [post()], next_cursor: "cursor-2" }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(screen.getByText("Second post")).toBeInTheDocument());
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("/api/v1/posts?cursor=cursor-2");
  });

  it("shows a load-more failure instead of dropping it", async () => {
    fetchMock.mockRejectedValueOnce(new Error("network down"));
    render(
      <PostsWorkspace
        initialPage={{ items: [post()], next_cursor: "cursor-2" }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByRole("alert")).toHaveTextContent(
      /could not reach the local service/,
    );
    // The button recovers for a retry; the first page is still shown.
    expect(screen.getByRole("button", { name: "Load more" })).toBeEnabled();
    expect(screen.getByText("Launch clip")).toBeInTheDocument();
  });
});
