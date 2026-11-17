import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ActivityPage from "./page";

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("ActivityPage", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("renders naive audit timestamps as UTC, not local time", async () => {
    vi.stubEnv("TZ", "America/New_York");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse([
          {
            id: 1,
            event_type: "settings.updated",
            entity_type: "settings",
            entity_id: null,
            details: {},
            created_at: "2026-07-20T11:56:00",
          },
        ]),
      ),
    );

    render(await ActivityPage({ searchParams: Promise.resolve({}) }));

    const time = screen.getByText(
      new Date("2026-07-20T11:56:00Z").toLocaleString(),
    );
    expect(time.tagName).toBe("TIME");
    expect(time).toHaveAttribute("datetime", "2026-07-20T11:56:00.000Z");
  });
});
