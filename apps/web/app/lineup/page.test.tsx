import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import LineupPage from "./page";

const lineup = {
  generated_at: "2099-08-07T22:00:00Z",
  timezone: "UTC",
  default_time: "10:00",
  posts_per_day: 1,
  coverage: 0,
  next_available_at: "2099-08-10T10:00:00Z",
  scheduled: [],
};

const publisherQueue = {
  running: false,
  queued: 0,
  paused: false,
  paused_reason: null,
  proposal_ids: [],
};

const settings = {
  authorized_browser_ready: false,
  publishing_mode: "assisted",
  channel_name: "Test channel",
};

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubLineupFetches(postsResponse: () => Promise<Response>) {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/v1/posts")) return postsResponse();
      if (url.includes("/api/queue")) return Promise.resolve(jsonResponse(lineup));
      if (url.includes("/api/proposals")) return Promise.resolve(jsonResponse([]));
      if (url.includes("/api/settings/full")) {
        return Promise.resolve(jsonResponse(settings));
      }
      if (url.includes("/api/publisher/queue")) {
        return Promise.resolve(jsonResponse(publisherQueue));
      }
      return Promise.reject(new Error(`unexpected fetch ${url}`));
    }),
  );
}

describe("LineupPage", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders the calendar without target chips when /api/v1/posts fails", async () => {
    // A non-404 failure (a 500, not just "route missing") must not take the
    // whole page down: the Lineup renders with no v1 target chips.
    stubLineupFetches(() =>
      Promise.resolve(jsonResponse({ detail: "boom" }, 500)),
    );

    render(await LineupPage());

    expect(screen.getByText("Release calendar.")).toBeInTheDocument();
    expect(
      screen.queryByLabelText(/^Open post \d+:/),
    ).not.toBeInTheDocument();
  });
});
