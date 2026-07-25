import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PlatformConnection } from "./platform-connection";

const initialConnection = {
  state: "unchecked" as const,
  valid: null,
  detail: "No saved-session check has been recorded yet.",
  publisher: "youtube-visible-browser-v1",
  checked_at: null,
  stale: false,
  stale_after_hours: 24,
  checks: {},
  last_verified_publish_at: null,
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("PlatformConnection", () => {
  it("checks the saved Qlob publisher session without exposing credentials", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        state: "connected",
        valid: true,
        publisher: "youtube-community-browser",
        detail: "Qlob Editor session is ready.",
        checked_at: "2026-07-20T13:00:00Z",
        stale: false,
        stale_after_hours: 24,
        checks: {
          "configured channel URL": true,
          "Qlob posting access": true,
        },
        last_verified_publish_at: "2026-07-20T12:30:00Z",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <PlatformConnection
        publishingEnabled
        channelId="UCQ-nHijGwxNU3Go_wyLQ5Ng"
        connectorEmail="tryrunwaytoday@gmail.com"
        browserChannel="chrome"
        initialConnection={initialConnection}
        initialQueue={{
          running: false,
          queued: 0,
          paused: false,
          paused_reason: null,
          proposal_ids: [],
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
        connectorEmail="tryrunwaytoday@gmail.com"
        browserChannel="chrome"
        initialConnection={{
          ...initialConnection,
          state: "disabled",
          detail: "Enable publishing before checking YouTube.",
        }}
        initialQueue={{
          running: false,
          queued: 0,
          paused: false,
          paused_reason: null,
          proposal_ids: [],
        }}
      />,
    );

    expect(screen.getByText("Record the declared role")).toBeInTheDocument();
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
        connectorEmail="tryrunwaytoday@gmail.com"
        browserChannel="chrome"
        initialConnection={initialConnection}
        initialQueue={{
          running: false,
          queued: 0,
          paused: false,
          paused_reason: null,
          proposal_ids: [],
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
        connectorEmail="tryrunwaytoday@gmail.com"
        browserChannel="chrome"
        initialConnection={{
          ...initialConnection,
          state: "connected",
          valid: true,
          checked_at: "2026-07-25T12:00:00Z",
          detail: "Qlob publisher session is ready.",
        }}
        initialQueue={{
          running: false,
          queued: 2,
          paused: true,
          paused_reason: "Sign-in required",
          proposal_ids: [42, 43],
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

  it("shows a durable stale connection without opening the browser on render", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(
      <PlatformConnection
        publishingEnabled
        channelId="UCQ-nHijGwxNU3Go_wyLQ5Ng"
        connectorEmail="tryrunwaytoday@gmail.com"
        browserChannel="chrome"
        initialConnection={{
          ...initialConnection,
          state: "stale",
          valid: true,
          stale: true,
          checked_at: "2026-07-18T13:00:00Z",
          detail: "Qlob channel identity and posting access were verified.",
          checks: {
            "configured channel URL": true,
            "active Qlob identity": true,
            "Community composer": true,
          },
        }}
        initialQueue={{
          running: false,
          queued: 0,
          paused: false,
          paused_reason: null,
          proposal_ids: [],
        }}
      />,
    );

    expect(screen.getByText("Check is stale")).toBeInTheDocument();
    expect(screen.getByText("Configured Qlob channel")).toBeInTheDocument();
    expect(screen.getByText("Qlob identity selected")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("clears stale green capability checks when a new check has no verified result", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    render(
      <PlatformConnection
        publishingEnabled
        channelId="UCQ-nHijGwxNU3Go_wyLQ5Ng"
        connectorEmail="tryrunwaytoday@gmail.com"
        browserChannel="chrome"
        initialConnection={{
          ...initialConnection,
          state: "connected",
          valid: true,
          checked_at: "2026-07-20T13:00:00Z",
          detail: "Previously connected.",
          checks: { "configured channel": true, "Community composer": true },
        }}
        initialQueue={{
          running: false,
          queued: 0,
          paused: false,
          paused_reason: null,
          proposal_ids: [],
        }}
      />,
    );

    expect(screen.getByText("Community composer")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Check saved session" }));

    expect(await screen.findByText("Needs attention")).toBeInTheDocument();
    expect(screen.queryByText("Community composer")).not.toBeInTheDocument();
    expect(screen.getAllByText("Not recorded").length).toBeGreaterThan(0);
  });

  it("does not offer Resume until a paused queue has a fresh connected session", () => {
    render(
      <PlatformConnection
        publishingEnabled
        channelId="UCQ-nHijGwxNU3Go_wyLQ5Ng"
        connectorEmail="tryrunwaytoday@gmail.com"
        browserChannel="chrome"
        initialConnection={{
          ...initialConnection,
          state: "stale",
          valid: true,
          stale: true,
          checked_at: "2026-07-18T13:00:00Z",
        }}
        initialQueue={{
          running: false,
          queued: 1,
          paused: true,
          paused_reason: "Sign-in required",
          proposal_ids: [42],
        }}
      />,
    );

    expect(
      screen.queryByRole("button", { name: "Resume failed actions" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/fresh successful saved-session check/i)).toBeInTheDocument();
  });
});
