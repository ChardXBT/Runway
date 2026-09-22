import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Settings, SettingsForm } from "./settings-form";

const settings: Settings = {
  connector_account_email: "tryrunwaytoday@gmail.com",
  channel_name: "Qlob",
  channel_handle: "@Qlob",
  timezone: "America/Toronto",
  default_post_time: "10:00",
  duplicate_window_days: 180,
  agent_runtime: "codex",
  codex_model: "gpt-5",
  codex_reasoning_effort: "low",
  codex_chatgpt_auth_required: true,
  paid_api_fallback_enabled: false,
  openai_configured: false,
  browser_search_enabled: true,
  publishing_enabled: true,
  publishing_mode: "authorized_browser",
  youtube_automation_authorized: true,
  authorized_browser_ready: true,
  caption_question_first: true,
  publisher_channel_id: "UCxxxxxxxxxxxxxxxxxxxxxx",
  publisher_browser_channel: "chrome",
  blocked_sources: [],
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("SettingsForm", () => {
  it("shows the configured local scheduling time and timezone", () => {
    render(<SettingsForm initial={settings} />);

    const preview = screen.getByText("Schedule preview").closest("p");
    expect(preview).toHaveTextContent("10:00 AM");
    expect(preview).toHaveTextContent("America/Toronto");
  });

  it("validates locally and preserves the invalid entry without submitting", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<SettingsForm initial={settings} />);

    fireEvent.change(screen.getByLabelText("Timezone"), {
      target: { value: "Toronto-ish" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("valid IANA timezone");
    expect(screen.getByLabelText("Timezone")).toHaveValue("Toronto-ish");
  });

  it("locks duplicate submissions and reflects a validated success", async () => {
    let resolveResponse!: (value: unknown) => void;
    const pending = new Promise((resolve) => {
      resolveResponse = resolve;
    });
    const fetchMock = vi.fn().mockReturnValue(pending);
    vi.stubGlobal("fetch", fetchMock);
    render(<SettingsForm initial={settings} />);

    const form = screen
      .getByRole("button", { name: "Save settings" })
      .closest("form");
    expect(form).not.toBeNull();
    fireEvent.submit(form!);
    fireEvent.submit(form!);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("button", { name: "Saving settings…" }),
    ).toBeDisabled();

    resolveResponse({
      ok: true,
      json: async () => ({ ...settings, default_post_time: "10:00" }),
    });

    expect(await screen.findByText("Settings saved.")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Qlob")).toBeDisabled();
    expect(screen.getByDisplayValue("@Qlob")).toBeDisabled();
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      fields: {
        timezone: "America/Toronto",
        default_post_time: "10:00",
        duplicate_window_days: 180,
      },
    });
  });

  it("does not show success for a malformed response and keeps every entry", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ detail: "maybe" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => settings,
      });
    vi.stubGlobal("fetch", fetchMock);
    render(<SettingsForm initial={settings} />);

    fireEvent.change(screen.getByLabelText("Duplicate window"), {
      target: { value: "240" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "response could not be verified",
    );
    expect(screen.queryByText("Settings saved.")).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("Qlob")).toHaveValue("Qlob");
    expect(screen.getByLabelText("Duplicate window")).toHaveValue(240);
    expect(screen.getByRole("button", { name: "Save settings" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Check saved state" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "save did not take effect",
    );
    expect(screen.getByLabelText("Duplicate window")).toHaveValue(240);
    expect(screen.getByRole("button", { name: "Save settings" })).toBeEnabled();
  });

  it("reports network failure without clearing the form", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
    );
    render(<SettingsForm initial={settings} />);

    fireEvent.change(screen.getByLabelText("Default post time"), {
      target: { value: "11:30" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "response could not be verified",
      ),
    );
    expect(screen.getByLabelText("Default post time")).toHaveValue("11:30");
  });
});
