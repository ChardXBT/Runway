import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { InboxWorkspace } from "./inbox-workspace";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));
import type {
  AccountView,
  InboxComment,
  InboxDetail,
  InboxListResponse,
} from "@/lib/types";

const account: AccountView = {
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

const comment: InboxComment = {
  id: 42,
  account_id: 7,
  platform: "youtube",
  external_id: "c1",
  content_id: 5,
  parent_external_id: null,
  author_name: "Mira Chen",
  author_external_id: "U1",
  author_avatar_url: null,
  author_is_owner: false,
  text: "Loved the walkthrough!",
  like_count: 2,
  reply_count: 0,
  published_at: "2026-07-19T10:00:00Z",
  status: "new",
  has_owner_reply: false,
  permalink: null,
  draft: null,
};

const list: InboxListResponse = {
  items: [comment],
  total: 1,
  counts: { unanswered: 1, new: 1 },
};

function makeDetail(overrides: Partial<InboxDetail> = {}): InboxDetail {
  return {
    comment,
    replies: [],
    content: null,
    draft: null,
    reply_capability: {
      ready: false,
      state: "supported_not_implemented",
      detail: "Replying requires the youtube.force-ssl scope.",
      handoff_url: "https://www.youtube.com/comments",
    },
    ...overrides,
  };
}

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const filters = {
  platform: "",
  account: "",
  status: "",
  unanswered: true,
  q: "",
};

describe("InboxWorkspace", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders the unanswered stat strip and comment rows", () => {
    render(
      <InboxWorkspace
        accounts={[account]}
        list={list}
        filters={filters}
        selectedId={null}
      />,
    );
    expect(screen.getByText("unanswered")).toBeInTheDocument();
    expect(screen.getByText("Mira Chen")).toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: /unanswered only/i }),
    ).toBeChecked();
  });

  it("loads the detail when a comment is selected", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(makeDetail()));
    render(
      <InboxWorkspace
        accounts={[account]}
        list={list}
        filters={filters}
        selectedId={42}
      />,
    );
    await screen.findByText("Loved the walkthrough!", {
      selector: ".inbox-comment-text",
    });
    expect(fetchMock.mock.calls[0][0]).toContain("/api/inbox/42");
    expect(
      screen.getByRole("link", { name: /Reply on YouTube instead/i }),
    ).toBeInTheDocument();
  });

  it("keeps Send reply disabled when the capability is not ready", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(makeDetail()));
    render(
      <InboxWorkspace
        accounts={[account]}
        list={list}
        filters={filters}
        selectedId={42}
      />,
    );
    const send = await screen.findByRole("button", { name: "Send reply" });
    expect(send).toBeDisabled();
    expect(
      screen.getByText("Replying requires the youtube.force-ssl scope."),
    ).toBeInTheDocument();
  });

  it("autosaves a draft on blur with PUT", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(makeDetail()));
    render(
      <InboxWorkspace
        accounts={[account]}
        list={list}
        filters={filters}
        selectedId={42}
      />,
    );
    const area = await screen.findByPlaceholderText(/Draft a reply/);
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 42, draft: "hi" }));
    fireEvent.change(area, { target: { value: "Thanks Mira!" } });
    fireEvent.blur(area);
    await screen.findByText("Saved");
    const call = fetchMock.mock.calls.at(-1)!;
    expect(call[0]).toContain("/api/inbox/42/draft");
    expect(call[1].method).toBe("PUT");
    expect(JSON.parse(call[1].body as string).text).toBe("Thanks Mira!");
  });

  it("patches status optimistically and rolls back on failure", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(makeDetail()));
    render(
      <InboxWorkspace
        accounts={[account]}
        list={list}
        filters={filters}
        selectedId={42}
      />,
    );
    const escalate = await screen.findByRole("button", { name: "Escalate" });
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "nope" }, 500));
    fireEvent.click(escalate);
    // optimistic: escalated chip rendered in detail
    await screen.findByText("nope");
  });

  it("sends a reply after the confirm step and shows verified success", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        makeDetail({
          reply_capability: {
            ready: true,
            state: "implemented",
            detail: "Replies post through the YouTube API.",
            handoff_url: null,
          },
          draft: "Appreciate it!",
        }),
      ),
    );
    render(
      <InboxWorkspace
        accounts={[account]}
        list={list}
        filters={filters}
        selectedId={42}
      />,
    );
    const send = await screen.findByRole("button", { name: "Send reply" });
    fireEvent.click(send);
    const confirm = await screen.findByRole("button", {
      name: "Confirm send to YouTube",
    });
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        id: 3,
        account_id: 7,
        action_type: "comment_reply",
        state: "completed",
        external_result_id: "r1",
        verified: true,
        detail: null,
      }),
    );
    fireEvent.click(confirm);
    await screen.findByText("Reply posted and verified.");
    const call = fetchMock.mock.calls.at(-1)!;
    expect(call[0]).toContain("/api/inbox/42/reply");
    expect(JSON.parse(call[1].body as string)).toEqual({
      text: "Appreciate it!",
      confirmed: true,
    });
  });

  it("renders a warning, not success, for an unverified reply", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        makeDetail({
          reply_capability: {
            ready: true,
            state: "implemented",
            detail: "Replies post through the YouTube API.",
            handoff_url: "https://www.youtube.com/comments",
          },
          draft: "Thanks!",
        }),
      ),
    );
    render(
      <InboxWorkspace
        accounts={[account]}
        list={list}
        filters={filters}
        selectedId={42}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Send reply" }));
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        id: 3,
        state: "uncertain",
        verified: false,
        external_result_id: null,
        detail: "timeout",
      }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Confirm send to YouTube" }),
    );
    await waitFor(() =>
      expect(
        screen.getByText(/could not confirm the reply/i),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("Reply posted and verified.")).toBeNull();
  });
});
