import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReleaseDialog } from "./release-dialog";
import type { PostOut, TargetOut } from "@/lib/v1-guards";

function target(overrides: Partial<TargetOut> = {}): TargetOut {
  return {
    id: 11,
    post_id: 7,
    platform: "tiktok",
    post_type: "tiktok_video",
    account_id: 5,
    caption: "Ship it",
    caption_override: null,
    state: "awaiting_release",
    state_reason: null,
    version: 3,
    payload_hash: "a".repeat(64),
    payload: { caption: "Ship it", privacy_level: "PUBLIC_TO_EVERYONE" },
    settings: {},
    effective_settings: {},
    issues: [],
    releasable: true,
    privacy_in_words: "Everyone",
    release_notice: "By posting, you agree to TikTok's terms.",
    release: { state: "none", payload_hash: null, released_at: null, released_by: null },
    planned_at: null,
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
    status: "draft",
    source: "agent",
    source_ref: null,
    ai_generated: true,
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

describe("ReleaseDialog", () => {
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

  it("echoes the payload hash and the consent line on release", async () => {
    const released = post({ status: "scheduled" });
    fetchMock.mockResolvedValueOnce(jsonResponse(released));
    const onReleased = vi.fn();
    render(
      <ReleaseDialog
        post={post()}
        target={target()}
        accountLabel="@creator"
        onClose={vi.fn()}
        onReleased={onReleased}
      />,
    );

    expect(screen.getByText("a".repeat(64))).toBeInTheDocument();
    expect(screen.getByText("Everyone")).toBeInTheDocument();
    expect(screen.getByText("This post was drafted by an agent.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Release this exact payload" }));

    await waitFor(() => expect(onReleased).toHaveBeenCalledWith(released));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8000/api/v1/posts/7/targets/11/release");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      payload_hash: "a".repeat(64),
      confirmed: true,
      consent: "By posting, you agree to TikTok's terms.",
    });
  });

  it("keeps the release button inert when the target is not releasable", () => {
    render(
      <ReleaseDialog
        post={post()}
        target={target({ releasable: false })}
        accountLabel="@creator"
        onClose={vi.fn()}
        onReleased={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Release this exact payload" })).toBeDisabled();
    expect(screen.getByText(/not releasable/)).toBeInTheDocument();
  });
});
