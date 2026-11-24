import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

import { PostEditor } from "./post-editor";
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

/** An EventSource stub whose `emit` delivers a typed server event. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readyState = 1;
  onopen: unknown = null;
  onmessage: unknown = null;
  onerror: unknown = null;
  readonly listeners = new Map<string, (event: MessageEvent) => void>();
  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(type, listener);
  }
  removeEventListener(type: string) {
    this.listeners.delete(type);
  }
  close() {}
  emit(type: string, data = "{}") {
    this.listeners.get(type)?.({ type, data, lastEventId: "1" } as MessageEvent);
  }
}

function post(overrides: Partial<PostOut> = {}): PostOut {
  return {
    id: 7,
    title: "Launch clip",
    body: "Base caption",
    status: "draft",
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
    targets: [],
    ...overrides,
  };
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
    state: "draft",
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

const youtubeAccount = {
  id: 5,
  platform: "youtube",
  handle: "@nw",
  display_name: "Northwind",
  status: "connected",
};

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Routes fetches: `post` answers every GET /posts/{id}, others fall through. */
function routeFetches(posts: Record<string, unknown> | (() => unknown)) {
  return (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/youtube/categories")) {
      return Promise.resolve(jsonResponse({ items: [], region: "US" }));
    }
    if (/\/api\/v1\/posts\/7$/.test(url) && (init?.method ?? "GET") === "GET") {
      const payload = typeof posts === "function" ? posts() : posts;
      return Promise.resolve(jsonResponse(payload));
    }
    return Promise.reject(new Error(`unexpected fetch ${init?.method ?? "GET"} ${url}`));
  };
}

describe("PostEditor", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    FakeEventSource.instances = [];
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", NoopEventSource);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("saves with If-Match and surfaces a 409 as a reload notice", async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(
          { type: "about:blank", title: "Conflict", status: 409, detail: "stale version", code: "conflict" },
          409,
        ),
      )
      .mockResolvedValueOnce(jsonResponse(post({ version: 5 })));

    render(<PostEditor initialPost={post()} accounts={[]} timezone="UTC" />);
    fireEvent.click(screen.getByRole("button", { name: "Save post" }));

    await waitFor(() =>
      expect(
        screen.getByText("This post changed elsewhere — reloaded the latest version."),
      ).toBeInTheDocument(),
    );

    const [patchUrl, patchInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(patchUrl).toBe("http://127.0.0.1:8000/api/v1/posts/7");
    expect(patchInit.method).toBe("PATCH");
    expect(new Headers(patchInit.headers).get("If-Match")).toBe('"2"');
    const [getUrl] = fetchMock.mock.calls[1] as [string];
    expect(getUrl).toBe("http://127.0.0.1:8000/api/v1/posts/7");
  });

  it("shows a caption counter over the base caption", () => {
    render(<PostEditor initialPost={post()} accounts={[]} timezone="UTC" />);
    expect(screen.getByText("Launch clip")).toBeInTheDocument();
    expect(screen.getByText(/12 chars/)).toBeInTheDocument();
  });

  it("creates a new post once a draft target is complete", async () => {
    const accounts = [
      { id: 5, platform: "youtube", handle: "@nw", display_name: "Northwind", status: "connected" },
    ];
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({ items: [{ id: "10", title: "Music", assignable: true }], region: "US" }),
      )
      .mockResolvedValueOnce(jsonResponse(post({ id: 42 })));
    render(<PostEditor initialPost={null} accounts={accounts} timezone="UTC" />);

    expect(screen.getByRole("heading", { name: "New post" })).toBeInTheDocument();
    const create = screen.getByRole("button", { name: "Create post" });
    expect(create).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Fresh post" },
    });
    fireEvent.change(screen.getByLabelText("Add a target for"), {
      target: { value: "youtube" },
    });
    fireEvent.change(screen.getByDisplayValue("Choose an account…"), {
      target: { value: "5" },
    });
    fireEvent.change(screen.getByDisplayValue("Choose…"), {
      target: { value: "youtube_video" },
    });
    expect(create).not.toBeDisabled();

    fireEvent.click(create);
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([url, init]) =>
            String(url).includes("/api/v1/posts") &&
            (init as RequestInit).method === "POST",
        ),
      ).toBe(true),
    );
    const postCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).includes("/api/v1/posts") &&
        (init as RequestInit).method === "POST",
    ) as [string, RequestInit];
    const body = JSON.parse(String(postCall[1].body));
    expect(body).toMatchObject({ title: "Fresh post" });
    expect(body.targets[0]).toMatchObject({
      platform: "youtube",
      account_id: 5,
      post_type: "youtube_video",
    });
    expect(new Headers(postCall[1].headers).get("Idempotency-Key")).toBeTruthy();
  });

  it("keeps unsaved target edits and offers a reload when the server version moves", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const moved = post({
      targets: [
        target({
          version: 4,
          caption_override: "Server rewrite",
          planned_at: "2026-12-01T10:30:00Z",
        }),
      ],
    });
    fetchMock.mockImplementation(routeFetches(moved));
    render(
      <PostEditor
        initialPost={post({ targets: [target()] })}
        accounts={[youtubeAccount]}
        timezone="UTC"
      />,
    );

    const caption = screen.getByLabelText("Caption for this target");
    fireEvent.change(caption, { target: { value: "My unsaved caption" } });

    const source = FakeEventSource.instances.at(-1);
    expect(source).toBeDefined();
    await act(async () => {
      source?.emit("target.state_changed");
    });

    // The draft survives; a notice offers to load the latest instead of a
    // silent remount wiping the edits.
    await waitFor(() =>
      expect(
        screen.getByText(/This target changed; reload to see the latest\./),
      ).toBeInTheDocument(),
    );
    expect(caption).toHaveValue("My unsaved caption");

    fireEvent.click(screen.getByRole("button", { name: "Load the latest" }));
    await waitFor(() => expect(caption).toHaveValue("Server rewrite"));
    expect(
      screen.queryByText(/This target changed; reload to see the latest\./),
    ).not.toBeInTheDocument();
  });

  it("adopts the latest server values when nothing is unsaved", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const moved = post({
      targets: [target({ version: 4, caption_override: "Server rewrite" })],
    });
    fetchMock.mockImplementation(routeFetches(moved));
    render(
      <PostEditor
        initialPost={post({ targets: [target()] })}
        accounts={[youtubeAccount]}
        timezone="UTC"
      />,
    );

    const source = FakeEventSource.instances.at(-1);
    await act(async () => {
      source?.emit("target.state_changed");
    });

    const caption = screen.getByLabelText("Caption for this target");
    await waitFor(() => expect(caption).toHaveValue("Server rewrite"));
    expect(
      screen.queryByText(/This target changed; reload to see the latest\./),
    ).not.toBeInTheDocument();
  });

  it("keeps the picked publish date and time across a target save", async () => {
    // TargetUpdate has no planned_at field: the pickers' values must survive
    // the save's version bump instead of resetting to the server's.
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/youtube/categories")) {
        return Promise.resolve(jsonResponse({ items: [], region: "US" }));
      }
      if (url.endsWith("/targets/11") && init?.method === "PUT") {
        return Promise.resolve(jsonResponse(post({ targets: [target({ version: 4 })] })));
      }
      return Promise.reject(new Error(`unexpected fetch ${init?.method ?? "GET"} ${url}`));
    });
    render(
      <PostEditor
        initialPost={post({ targets: [target()] })}
        accounts={[youtubeAccount]}
        timezone="UTC"
      />,
    );

    fireEvent.change(screen.getByLabelText("Publish date"), {
      target: { value: "2026-12-01" },
    });
    fireEvent.change(screen.getByLabelText("Time (UTC)"), {
      target: { value: "10:30" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save target" }));

    await waitFor(() => expect(screen.getByText("Saved.")).toBeInTheDocument());
    expect(screen.getByLabelText("Publish date")).toHaveValue("2026-12-01");
    expect(screen.getByLabelText("Time (UTC)")).toHaveValue("10:30");
    expect(
      screen.queryByText(/This target changed; reload to see the latest\./),
    ).not.toBeInTheDocument();
  });
});
