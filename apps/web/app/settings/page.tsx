import { PlatformConnection } from "@/components/platform-connection";
import { SettingsForm } from "@/components/settings-form";
import { apiGetRequired } from "@/lib/api";
import {
  isPublisherConnectionStatus,
  isPublisherQueueStatus,
} from "@/lib/guards";
import type {
  PublisherConnectionStatus,
  PublisherQueueStatus,
} from "@/lib/types";
import { isSettings, type Settings } from "@/lib/settings";

export const dynamic = "force-dynamic";

export default async function SettingsPage() {
  const [settings, publisherQueue, publisherConnection] = await Promise.all([
    apiGetRequired<Settings>("/api/settings/full", isSettings),
    apiGetRequired<PublisherQueueStatus>(
      "/api/publisher/queue",
      isPublisherQueueStatus,
    ),
    apiGetRequired<PublisherConnectionStatus>(
      "/api/publisher/session/status",
      isPublisherConnectionStatus,
    ),
  ]);
  return (
    <>
      <header className="header-row">
        <div>
          <p className="eyebrow">Local configuration</p>
          <h1>Runway settings.</h1>
          <p className="lede">
            Configure the daily schedule and local safeguards. Channel identity is
            read-only, secret values stay hidden, and
            external handling still requires a deliberate Lineup action.
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
