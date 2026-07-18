import { PlatformConnection } from "@/components/platform-connection";
import { SettingsForm } from "@/components/settings-form";
import { apiGet } from "@/lib/api";
import type { PublisherQueueStatus } from "@/lib/types";

type Settings = Parameters<typeof SettingsForm>[0]["initial"];

const fallback: Settings = {
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
  caption_question_first: true,
  publisher_channel_id: "UCQ-nHijGwxNU3Go_wyLQ5Ng",
  publisher_browser_channel: "chrome",
  blocked_sources: [],
};

export default async function SettingsPage() {
  const [settings, publisherQueue] = await Promise.all([
    apiGet<Settings>("/api/settings/full", fallback),
    apiGet<PublisherQueueStatus>("/api/publisher/queue", {
      running: false,
      queued: 0,
      paused: false,
      paused_reason: null,
    }),
  ]);
  return (
    <>
      <header className="header-row">
        <div>
          <p className="eyebrow">Local configuration</p>
          <h1>Quiet rules behind the feed.</h1>
          <p className="lede">
            Secret values never appear here. Every accepted look teaches RunWay and takes
            the next open daily slot through the visible Qlob publisher.
          </p>
        </div>
      </header>
      <PlatformConnection
        publishingEnabled={settings.publishing_enabled}
        channelId={settings.publisher_channel_id}
        browserChannel={settings.publisher_browser_channel}
        initialQueue={publisherQueue}
      />
      <SettingsForm initial={settings} />
    </>
  );
}
