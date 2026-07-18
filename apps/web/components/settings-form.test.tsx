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
  caption_question_first: true,
  publisher_channel_id: "UCQ-nHijGwxNU3Go_wyLQ5Ng",
  publisher_browser_channel: "chrome",
  blocked_sources: [],
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("SettingsForm", () => {
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
      json: async () => ({ ...settings, channel_name: "Qlob Studio" }),
    });

    expect(await screen.findByText("Settings saved.")).toBeInTheDocument();
    expect(screen.getByLabelText("Channel name")).toHaveValue("Qlob Studio");
  });

  it("does not show success for a malformed response and keeps every entry", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ detail: "maybe" }),
      }),
    );
    render(<SettingsForm initial={settings} />);

    fireEvent.change(screen.getByLabelText("Channel name"), {
      target: { value: "Qlob Edited" },
    });
    fireEvent.change(screen.getByLabelText("Duplicate window"), {
      target: { value: "240" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "response could not be verified",
    );
    expect(screen.queryByText("Settings saved.")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Channel name")).toHaveValue("Qlob Edited");
    expect(screen.getByLabelText("Duplicate window")).toHaveValue(240);
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
