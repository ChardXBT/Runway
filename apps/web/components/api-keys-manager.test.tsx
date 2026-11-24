import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiKeysManager } from "./api-keys-manager";
import type { ApiKeyView, PolicyResponse } from "@/lib/v1-guards";

const apiKey: ApiKeyView = {
  id: 3,
  name: "nightly planner",
  prefix: "rwk_abc",
  state: "active",
  scopes: ["posts:read"],
  effective_scopes: ["posts:read"],
  release_grants: [],
  created_at: "2026-11-01T10:00:00Z",
  created_by: "you",
  expires_at: null,
  last_used_at: null,
  revoked_at: null,
};

const policy: PolicyResponse = {
  agent_release: "never",
  default_agent_scopes: ["posts:read"],
  human_only_platforms: [],
  updated_at: null,
  updated_by: null,
};

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function listAndPolicy(keys: ApiKeyView[] = [apiKey]) {
  return [
    jsonResponse({ items: keys, next_cursor: null }),
    jsonResponse(policy),
  ];
}

describe("ApiKeysManager", () => {
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

  it("lists keys with the release policy", async () => {
    fetchMock.mockResolvedValueOnce(listAndPolicy()[0]);
    fetchMock.mockResolvedValueOnce(listAndPolicy()[1]);
    render(<ApiKeysManager />);

    await waitFor(() => expect(screen.getByText("nightly planner")).toBeInTheDocument());
    expect(screen.getByText(/rwk_abc/)).toBeInTheDocument();
    const urls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(urls).toContain("http://127.0.0.1:8000/api/v1/keys");
    expect(urls).toContain("http://127.0.0.1:8000/api/v1/policy");
  });

  it("checks the default agent scopes once the policy arrives", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ items: [], next_cursor: null }))
      .mockResolvedValueOnce(
        jsonResponse({
          ...policy,
          default_agent_scopes: ["posts:read", "posts:write"],
        }),
      );
    render(<ApiKeysManager />);

    // The form mounts with the server's defaults, not the empty list that was
    // there before the policy response landed.
    fireEvent.click(await screen.findByText("New API key"));
    await waitFor(() =>
      expect(
        screen.getByLabelText(/Read posts/) as HTMLInputElement,
      ).toBeChecked(),
    );
    expect(
      screen.getByLabelText(/Create and edit posts/) as HTMLInputElement,
    ).toBeChecked();
    expect(
      screen.getByLabelText(/Read accounts/) as HTMLInputElement,
    ).not.toBeChecked();
  });

  it("shows the new key's secret exactly once", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ items: [apiKey], next_cursor: null }))
      .mockResolvedValueOnce(jsonResponse(policy))
      .mockResolvedValueOnce(
        jsonResponse({ key: apiKey, secret: "rwk_secret_once" }),
      )
      .mockResolvedValueOnce(jsonResponse({ items: [apiKey], next_cursor: null }))
      .mockResolvedValueOnce(jsonResponse(policy));
    render(<ApiKeysManager />);

    await waitFor(() => expect(screen.getByText("nightly planner")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "weekend bot" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Create key|Create API key/i }));

    await waitFor(() =>
      expect(screen.getByText("rwk_secret_once")).toBeInTheDocument(),
    );
    const createCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith("/api/v1/keys") &&
        (init as RequestInit).method === "POST",
    ) as [string, RequestInit];
    expect(JSON.parse(String(createCall[1].body))).toMatchObject({
      name: "weekend bot",
    });

    fireEvent.click(screen.getByRole("button", { name: "I saved it — dismiss" }));
    expect(screen.queryByText("rwk_secret_once")).not.toBeInTheDocument();
  });
});
