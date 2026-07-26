import { describe, expect, it } from "vitest";

import { isSettings, type Settings } from "./settings";

const validSettings: Settings = {
  connector_account_email: "editor@example.com",
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
  publishing_enabled: false,
  publishing_mode: "assisted",
  youtube_automation_authorized: false,
  authorized_browser_ready: false,
  caption_question_first: true,
  publisher_channel_id: "UCexample",
  publisher_browser_channel: "chrome",
  blocked_sources: [],
};

describe("isSettings", () => {
  it("accepts the complete server settings payload", () => {
    expect(isSettings(validSettings)).toBe(true);
  });

  it("rejects invalid schedule and blocked-source values", () => {
    expect(isSettings({ ...validSettings, timezone: "Toronto-ish" })).toBe(false);
    expect(isSettings({ ...validSettings, default_post_time: "25:00" })).toBe(false);
    expect(
      isSettings({ ...validSettings, blocked_sources: [{ id: 1, type: "domain" }] }),
    ).toBe(false);
  });
});
