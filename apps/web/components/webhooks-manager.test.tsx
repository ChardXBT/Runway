import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WebhooksManager } from "./webhooks-manager";
import type { WebhookView } from "@/lib/v1-guards";

const endpoint: WebhookView = {
  id: 4,
  url: "https://example.test/hook",
  description: "CI listener",
  event_types: ["target.state_changed"],
  state: "active",
  consecutive_failures: 0,
  failing_since: null,
  last_error: null,
  last_failure_at: null,
  last_success_at: null,
  disabled_at: null,
  disabled_reason: null,
  created_at: "2026-11-01T10:00:00Z",
  created_by: null,
};

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("WebhooksManager", () => {
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

  it("lists endpoints and their subscriptions", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [endpoint], next_cursor: null }),
    );
    render(<WebhooksManager />);

    await waitFor(() => expect(screen.getByText("CI listener")).toBeInTheDocument());
    expect(screen.getByText("https://example.test/hook")).toBeInTheDocument();
    // The subscribed pill plus the same event type in the create-form list.
    expect(screen.getAllByText("target.state_changed").length).toBeGreaterThanOrEqual(1);
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("http://127.0.0.1:8000/api/v1/webhooks");
  });

  it("creates an endpoint and shows the signing secret once", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ items: [], next_cursor: null }))
      .mockResolvedValueOnce(
        jsonResponse({ ...endpoint, secret: "whsec_shown_once" }),
      )
      .mockResolvedValueOnce(jsonResponse({ items: [endpoint], next_cursor: null }));
    render(<WebhooksManager />);

    await waitFor(() =>
      expect(screen.getByText("No webhook endpoints yet.")).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByLabelText("Endpoint URL"), {
      target: { value: "https://example.test/hook" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create endpoint" }));

    await waitFor(() =>
      expect(screen.getByText("whsec_shown_once")).toBeInTheDocument(),
    );
    const createCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith("/api/v1/webhooks") &&
        (init as RequestInit).method === "POST",
    ) as [string, RequestInit];
    expect(JSON.parse(String(createCall[1].body))).toMatchObject({
      url: "https://example.test/hook",
      event_types: [],
    });

    fireEvent.click(screen.getByRole("button", { name: "I saved it — dismiss" }));
    expect(screen.queryByText("whsec_shown_once")).not.toBeInTheDocument();
  });

  it("opens the delivery ledger for an endpoint", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ items: [endpoint], next_cursor: null }))
      .mockResolvedValueOnce(
        jsonResponse({
          items: [
            {
              id: 1,
              endpoint_id: 4,
              event_id: 9,
              event_type: "target.state_changed",
              state: "delivered",
              attempts: 1,
              delivered_at: "2026-11-20T10:01:00Z",
              next_attempt_at: "2026-11-20T10:05:00Z",
              last_status_code: 200,
              last_error: null,
              created_at: "2026-11-20T10:00:00Z",
            },
          ],
          next_cursor: null,
        }),
      );
    render(<WebhooksManager />);
    await waitFor(() => expect(screen.getByText("CI listener")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Deliveries" }));
    await waitFor(() => expect(screen.getByText(/delivered · 1 attempt/)).toBeInTheDocument());
    const ledgerCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/deliveries"),
    ) as [string];
    expect(ledgerCall[0]).toContain("/api/v1/webhooks/4/deliveries");
  });
});
