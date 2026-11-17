import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import CataloguePage from "./page";

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("CataloguePage", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("renders naive post publish times as UTC, not local time", async () => {
    vi.stubEnv("TZ", "America/New_York");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/catalog/status")) {
          return jsonResponse({ total_posts: 1, training_eligible: 0, media_assets: 0 });
        }
        if (url.includes("/api/proposals")) return jsonResponse([]);
        return jsonResponse([
          {
            id: 9,
            post_type: "image",
            caption: "Late-night post",
            // 02:00 UTC on Jul 20 is still Jul 19 in New York.
            published_at: "2026-07-20T02:00:00",
            date_precision: "exact",
            is_training_eligible: false,
            media: [],
          },
        ]);
      }),
    );

    render(await CataloguePage({ searchParams: Promise.resolve({}) }));

    expect(
      screen.getByText(new Date("2026-07-20T02:00:00Z").toLocaleDateString()),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(new Date("2026-07-20T02:00:00").toLocaleDateString()),
    ).toBeNull();
  });
});
