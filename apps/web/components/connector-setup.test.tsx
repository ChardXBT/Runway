import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConnectorSetup } from "./connector-setup";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ConnectorSetup", () => {
  it("teaches the least-privilege invitation flow", () => {
    render(
      <ConnectorSetup
        connectorEmail="tryrunwaytoday@gmail.com"
        configuredChannelId=""
        browserName="Google Chrome"
        publishingMode="assisted"
        publishingEnabled={false}
      />,
    );

    expect(screen.getAllByText("tryrunwaytoday@gmail.com")).toHaveLength(2);
    expect(screen.getByText("Open channel permissions")).toBeInTheDocument();
    expect(screen.getByText("Choose Editor (limited)")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Verify capability" }),
    ).toBeDisabled();
  });

  it("runs a read-only channel access check", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        valid: true,
        detail: "Community publishing controls observed.",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ConnectorSetup
        connectorEmail="tryrunwaytoday@gmail.com"
        configuredChannelId=""
        browserName="Google Chrome"
        publishingMode="assisted"
        publishingEnabled={false}
      />,
    );

    fireEvent.change(
      screen.getByLabelText("YouTube channel URL, handle, or channel ID"),
      { target: { value: "https://www.youtube.com/@creator" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Verify capability" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toContain(
      "/api/publisher/connectors/validate",
    );
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      channel_url: "https://www.youtube.com/@creator",
    });
    expect(await screen.findByText("Capability observed")).toBeInTheDocument();
    expect(
      screen.getByText("Community publishing controls observed. Publishing remains disabled."),
    ).toBeInTheDocument();
  });

  it("keeps read-only capability verification available while publishing is off", () => {
    render(
      <ConnectorSetup
        connectorEmail="tryrunwaytoday@gmail.com"
        configuredChannelId="UCQ-nHijGwxNU3Go_wyLQ5Ng"
        browserName="Google Chrome"
        publishingMode="assisted"
        publishingEnabled={false}
      />,
    );

    expect(screen.getByText("Assisted mode is active.")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Verify capability" }),
    ).toBeEnabled();
    expect(screen.getByText(/never enables publishing/i)).toBeInTheDocument();
  });
});
