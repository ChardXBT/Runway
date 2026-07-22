import { PlatformConnection } from "@/components/platform-connection";
import { isSettings, SettingsForm } from "@/components/settings-form";
import { apiGet } from "@/lib/api";
import {
  isPublisherConnectionStatus,
  isPublisherQueueStatus,
} from "@/lib/guards";
import type {
  PublisherConnectionStatus,
  PublisherQueueStatus,
} from "@/lib/types";

type Settings = Parameters<typeof SettingsForm>[0]["initial"];

const fallback: Settings = {
  connector_account_email: "tryrunwaytoday@gmail.com",
  channel_name: "Qlob",
  channel_handle: "Qlob",
  timezone: "America/Toronto",
  default_post_time: "10:00",
  duplicate_window_days: 180,
  agent_runtime: "codex",
  codex_model: "gpt-5.6-luna",
  codex_reasoning_effort: "low",
  codex_chatgpt_auth_required: true,
  paid_api_fallback_enabled: false,
  openai_configured: false,
  browser_search_enabled: false,
  publishing_enabled: false,
  publishing_mode: "assisted",
  youtube_automation_authorized: false,
  authorized_browser_ready: false,
  caption_question_first: true,
  publisher_channel_id: "UCQ-nHijGwxNU3Go_wyLQ5Ng",
  publisher_browser_channel: "chrome",
  blocked_sources: [],
};

export default async function SettingsPage() {
  const [settings, publisherQueue, publisherConnection] = await Promise.all([
    apiGet<Settings>("/api/settings/full", fallback, isSettings),
    apiGet<PublisherQueueStatus>("/api/publisher/queue", {
      running: false,
      queued: 0,
      paused: false,
      paused_reason: null,
    }, isPublisherQueueStatus),
    apiGet<PublisherConnectionStatus>(
      "/api/publisher/session/status",
      {
        state: "unchecked",
        valid: null,
        detail: "Connection history is unavailable until the local service responds.",
        publisher: "youtube-visible-browser-v1",
        checked_at: null,
        stale: false,
        stale_after_hours: 24,
        checks: {},
        last_verified_publish_at: null,
      },
      isPublisherConnectionStatus,
    ),
  ]);
  return (
    <>
      <header className="header-row">
        <div>
          <p className="eyebrow">Local configuration</p>
          <h1>Quiet rules behind the feed.</h1>
          <p className="lede">
            Secret values never appear here. Accept is editorial and local-only; external
            handling starts from a deliberate Lineup action.
          </p>
        </div>
      </header>
      <PlatformConnection
        publishingEnabled={settings.authorized_browser_ready}
        publishingMode={settings.publishing_mode}
        channelId={settings.publisher_channel_id}
        connectorEmail={settings.connector_account_email}
        browserChannel={settings.publisher_browser_channel}
        initialQueue={publisherQueue}
        initialConnection={publisherConnection}
      />
      <SettingsForm initial={settings} />
    </>
  );
}
