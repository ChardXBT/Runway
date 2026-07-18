import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PlatformConnection } from "./platform-connection";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("PlatformConnection", () => {
  it("checks the saved Qlob publisher session without exposing credentials", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        valid: true,
        publisher: "youtube-community-browser",
        detail: "Qlob Editor session is ready.",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <PlatformConnection
        publishingEnabled
        channelId="UCQ-nHijGwxNU3Go_wyLQ5Ng"
        browserChannel="chrome"
        initialQueue={{
          running: false,
          queued: 0,
          paused: false,
          paused_reason: null,
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Check connection" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toContain("/api/publisher/session/validate");
    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(screen.getByText("Qlob Editor session is ready.")).toBeInTheDocument();
  });
});
