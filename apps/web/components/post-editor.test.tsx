import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

import { PostEditor } from "./post-editor";
import type { PostOut } from "@/lib/v1-guards";

class NoopEventSource {
  readyState = 0;
  onopen = null;
  onmessage = null;
  onerror = null;
  addEventListener() {}
  removeEventListener() {}
  close() {}
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

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("PostEditor", () => {
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
});
