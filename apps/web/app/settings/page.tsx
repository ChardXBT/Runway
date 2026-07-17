import { SettingsForm } from "@/components/settings-form";
import { apiGet } from "@/lib/api";

type Settings = Parameters<typeof SettingsForm>[0]["initial"];

const fallback: Settings = {
  channel_name: "Qlob",
  channel_handle: "Qlob",
  timezone: "America/Toronto",
  default_post_time: "10:00",
  planning_horizon_days: 10,
  duplicate_window_days: 180,
  agent_runtime: "codex",
  codex_model: "gpt-5.6-luna",
  codex_reasoning_effort: "low",
  codex_chatgpt_auth_required: true,
  paid_api_fallback_enabled: false,
  openai_configured: false,
  browser_search_enabled: false,
  publishing_enabled: false,
  caption_question_first: true,
  publisher_channel_id: "UCQ-nHijGwxNU3Go_wyLQ5Ng",
  publisher_confirmation_ttl_minutes: 10,
  publisher_requires_human_confirmation: true,
  publisher_visible_browser_only: true,
  blocked_sources: [],
};

export default async function SettingsPage() {
  const settings = await apiGet<Settings>("/api/settings/full", fallback);
  return (
    <>
      <header className="header-row">
        <div>
          <p className="eyebrow">Local configuration</p>
          <h1>Settings with guardrails.</h1>
          <p className="lede">
            Secret values never appear here. Caption learning is local, browser actions stay
            visible, and external scheduling always stops at a human confirmation boundary.
          </p>
        </div>
      </header>
      <SettingsForm initial={settings} />
    </>
  );
}
