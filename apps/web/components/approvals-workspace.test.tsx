import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApprovalsWorkspace } from "./approvals-workspace";
import type { ApprovalItem, PostOut, TargetOut } from "@/lib/v1-guards";

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
    caption: "Agent caption",
    caption_override: null,
    state: "awaiting_release",
    state_reason: null,
    version: 3,
    payload_hash: "c".repeat(64),
    payload: { caption: "Agent caption" },
    settings: {},
    effective_settings: {},
    issues: [],
    releasable: true,
    privacy_in_words: "Public",
    release_notice: null,
    release: { state: "none", payload_hash: null, released_at: null, released_by: null },
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
    title: "Agent draft",
    body: "Body",
    status: "attention",
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

const item: ApprovalItem = {
  target_id: 11,
  post_id: 7,
  platform: "youtube",
  account_id: 5,
  post_type: "youtube_video",
  title: "Agent draft",
  planned_at: "2026-11-25T15:00:00Z",
  payload: { caption: "Agent caption" },
  payload_hash: "c".repeat(64),
  version: 3,
  source: "agent",
  created_by: "planner",
  ai_generated: true,
};

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("ApprovalsWorkspace", () => {
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

  it("lists queued targets and opens the release dialog for one", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(post()));
    render(<ApprovalsWorkspace initialPage={{ items: [item], next_cursor: null }} />);

    expect(screen.getByText("Agent draft")).toBeInTheDocument();
    expect(screen.getByText("agent-drafted")).toBeInTheDocument();
    expect(screen.getByText(/hash cccc/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Review & release" }));
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: /Release YouTube video/ }),
      ).toBeInTheDocument(),
    );
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("http://127.0.0.1:8000/api/v1/posts/7");
  });

  it("rejects a target only once a reason is given", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(jsonResponse({ items: [], next_cursor: null }));
    render(<ApprovalsWorkspace initialPage={{ items: [item], next_cursor: null }} />);

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    const submit = screen.getByRole("button", { name: "Reject this target" });
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Why are you rejecting it?"), {
      target: { value: "Wrong hook" },
    });
    expect(submit).not.toBeDisabled();
    fireEvent.click(submit);

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("/api/v1/approvals/11/reject"),
        ),
      ).toBe(true),
    );
    const rejectCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/api/v1/approvals/11/reject"),
    ) as [string, RequestInit];
    expect(JSON.parse(String(rejectCall[1].body))).toEqual({
      reason: "Wrong hook",
      edits: [],
    });
  });

  it("shows the empty panel when nothing waits", () => {
    render(<ApprovalsWorkspace initialPage={{ items: [], next_cursor: null }} />);
    expect(screen.getByText("Nothing waiting")).toBeInTheDocument();
  });
});
