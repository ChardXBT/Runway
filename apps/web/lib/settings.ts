import { isValidTimeZone } from "@/lib/datetime";
import { isRecord } from "@/lib/guards";

export type Settings = {
  connector_account_email: string;
  channel_name: string;
  channel_handle: string;
  timezone: string;
  default_post_time: string;
  duplicate_window_days: number;
  agent_runtime: string;
  codex_model: string;
  codex_reasoning_effort: string;
  codex_chatgpt_auth_required: boolean;
  paid_api_fallback_enabled: boolean;
  openai_configured: boolean;
  browser_search_enabled: boolean;
  publishing_enabled: boolean;
  publishing_mode: "assisted" | "authorized_browser";
  youtube_automation_authorized: boolean;
  authorized_browser_ready: boolean;
  caption_question_first: boolean;
  publisher_channel_id: string;
  publisher_browser_channel: string;
  blocked_sources: { id: number; type: string; value: string }[];
};

export function isSettings(value: unknown): value is Settings {
  if (!isRecord(value)) return false;
  return (
    typeof value.connector_account_email === "string" &&
    typeof value.channel_name === "string" &&
    typeof value.channel_handle === "string" &&
    typeof value.timezone === "string" &&
    isValidTimeZone(value.timezone) &&
    typeof value.default_post_time === "string" &&
    /^([01]\d|2[0-3]):[0-5]\d$/.test(value.default_post_time) &&
    typeof value.duplicate_window_days === "number" &&
    Number.isInteger(value.duplicate_window_days) &&
    value.duplicate_window_days >= 1 &&
    typeof value.agent_runtime === "string" &&
    typeof value.codex_model === "string" &&
    typeof value.codex_reasoning_effort === "string" &&
    typeof value.codex_chatgpt_auth_required === "boolean" &&
    typeof value.paid_api_fallback_enabled === "boolean" &&
    typeof value.openai_configured === "boolean" &&
    typeof value.browser_search_enabled === "boolean" &&
    typeof value.publishing_enabled === "boolean" &&
    (value.publishing_mode === "assisted" ||
      value.publishing_mode === "authorized_browser") &&
    typeof value.youtube_automation_authorized === "boolean" &&
    typeof value.authorized_browser_ready === "boolean" &&
    typeof value.caption_question_first === "boolean" &&
    typeof value.publisher_channel_id === "string" &&
    typeof value.publisher_browser_channel === "string" &&
    Array.isArray(value.blocked_sources) &&
    value.blocked_sources.every(
      (item) =>
        isRecord(item) &&
        typeof item.id === "number" &&
        typeof item.type === "string" &&
        typeof item.value === "string",
    )
  );
}
