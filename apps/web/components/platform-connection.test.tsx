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

    fireEvent.click(
      screen.getByRole("button", { name: "Check saved session" }),
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toContain("/api/publisher/session/validate");
    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(screen.getByText("Qlob Editor session is ready.")).toBeInTheDocument();
  });

  it("shows the real setup flow without a fake connect control", () => {
    render(
      <PlatformConnection
        publishingEnabled={false}
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

    expect(screen.getByText("Confirm Editor access")).toBeInTheDocument();
    expect(screen.getByText("Save the publisher login")).toBeInTheDocument();
    expect(
      screen.getByText(
        ".\\.venv\\Scripts\\runway.exe publisher login",
        { selector: "code" },
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^connect$/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Check saved session" }),
    ).toBeDisabled();
  });

  it("blocks double checks and rejects a malformed success response", async () => {
    let resolveResponse!: (value: unknown) => void;
    const pending = new Promise((resolve) => {
      resolveResponse = resolve;
    });
    const fetchMock = vi.fn().mockReturnValue(pending);
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

    const button = screen.getByRole("button", { name: "Check saved session" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    resolveResponse({
      ok: true,
      json: async () => ({ detail: "No valid flag" }),
    });

    expect(await screen.findByText("Needs attention")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "unexpected response",
    );
    expect(screen.queryByText("Connected")).not.toBeInTheDocument();
  });

  it("keeps a paused queue paused when recovery fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        json: async () => ({ detail: "Sign-in is still required." }),
      }),
    );
    render(
      <PlatformConnection
        publishingEnabled
        channelId="UCQ-nHijGwxNU3Go_wyLQ5Ng"
        browserChannel="chrome"
        initialQueue={{
          running: false,
          queued: 2,
          paused: true,
          paused_reason: "Sign-in required",
        }}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Resume failed actions" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Sign-in is still required.",
    );
    expect(screen.getByText("Paused")).toBeInTheDocument();
  });
});
