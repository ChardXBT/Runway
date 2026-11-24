import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TargetStatus } from "./target-status";
import type { PostOut, TargetOut } from "@/lib/v1-guards";

function target(overrides: Partial<TargetOut> = {}): TargetOut {
  return {
    id: 11,
    post_id: 7,
    platform: "youtube",
    post_type: "youtube_video",
    account_id: 5,
    caption: "Caption",
    caption_override: null,
    state: "uncertain",
    state_reason: "The upload timed out.",
    version: 3,
    payload_hash: "b".repeat(64),
    payload: {},
    settings: {},
    effective_settings: {},
    issues: [],
    releasable: false,
    privacy_in_words: null,
    release_notice: null,
    release: { state: "none", payload_hash: null, released_at: null, released_by: null },
    planned_at: null,
    publisher: null,
    capability: null,
    external_id: null,
    last_attempt: {
      id: 3,
      target_id: 11,
      kind: "publish",
      adapter: "youtube",
      outcome: "uncertain",
      error_code: null,
      error_detail: "timed out",
      external_id: null,
      job_id: null,
      payload_hash: null,
      request_sent_at: null,
      response: { checked: ["the video page", "the uploads list"] },
      started_at: "2026-11-20T10:00:00Z",
      finished_at: null,
    },
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
    status: "attention",
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

describe("TargetStatus", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows the uncertain state, what was checked, and the reconcile actions", async () => {
    const updated = post();
    fetchMock.mockResolvedValueOnce(jsonResponse(updated));
    const onChanged = vi.fn();
    render(<TargetStatus post={post()} target={target()} onChanged={onChanged} />);

    expect(screen.getByText("Uncertain")).toBeInTheDocument();
    expect(screen.getByText("What we checked")).toBeInTheDocument();
    expect(screen.getByText("the video page")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(updated));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      "http://127.0.0.1:8000/api/v1/posts/7/targets/11/reconcile",
    );
    expect(JSON.parse(String(init.body))).toEqual({
      resolution: "check",
      confirmed: true,
    });
  });

  it("marks a target published through the live URL form", async () => {
    const updated = post();
    fetchMock.mockResolvedValueOnce(jsonResponse(updated));
    const onChanged = vi.fn();
    render(<TargetStatus post={post()} target={target()} onChanged={onChanged} />);

    fireEvent.change(
      screen.getByPlaceholderText("Paste the live URL to mark it published"),
      { target: { value: "https://youtube.com/watch?v=abc" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Mark published" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(updated));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/v1/posts/7/targets/11/mark-published");
    expect(JSON.parse(String(init.body))).toEqual({
      published_url: "https://youtube.com/watch?v=abc",
    });
  });

  it("expands the attempts ledger", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        items: [
          {
            id: 3,
            target_id: 11,
            kind: "publish",
            adapter: "youtube",
            outcome: "uncertain",
            error_code: null,
            error_detail: "timed out",
            external_id: null,
            job_id: null,
            payload_hash: null,
            request_sent_at: null,
            response: {},
            started_at: "2026-11-20T10:00:00Z",
            finished_at: null,
          },
        ],
        next_cursor: null,
      }),
    );
    render(
      <TargetStatus
        post={post()}
        target={target({ state: "published", last_attempt: null })}
        onChanged={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Show attempts" }));
    await waitFor(() => expect(screen.getByText("Unclear")).toBeInTheDocument());
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("/api/v1/posts/7/targets/11/attempts");
  });
});
