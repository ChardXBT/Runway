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
      />,
    );

    expect(screen.getAllByText("tryrunwaytoday@gmail.com")).toHaveLength(2);
    expect(screen.getByText("Open channel permissions")).toBeInTheDocument();
    expect(screen.getByText("Choose Editor (limited)")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Verify connection" }),
    ).toBeDisabled();
  });

  it("runs a read-only channel access check", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        valid: true,
        detail: "Editor access and Community publishing controls verified.",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ConnectorSetup
        connectorEmail="tryrunwaytoday@gmail.com"
        configuredChannelId=""
        browserName="Google Chrome"
      />,
    );

    fireEvent.change(
      screen.getByLabelText("YouTube channel URL, handle, or channel ID"),
      { target: { value: "https://www.youtube.com/@creator" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Verify connection" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toContain(
      "/api/publisher/connectors/validate",
    );
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      channel_url: "https://www.youtube.com/@creator",
    });
    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(
      screen.getByText("Editor access and Community publishing controls verified."),
    ).toBeInTheDocument();
  });
});
