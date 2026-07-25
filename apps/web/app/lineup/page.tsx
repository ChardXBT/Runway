import { LineupCalendar } from "@/components/lineup-calendar";
import { apiGetRequired } from "@/lib/api";
import {
  isLineupSchedule,
  isPublisherQueueStatus,
  proposalList,
} from "@/lib/guards";
import type {
  LineupSchedule,
  Proposal,
  PublisherQueueStatus,
} from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function LineupPage() {
  const [lineup, published, settings, publisherQueue] = await Promise.all([
    apiGetRequired<LineupSchedule>("/api/queue?limit=5000", isLineupSchedule),
    apiGetRequired<Proposal[]>(
      "/api/proposals?status=published&limit=500&order=desc",
      proposalList,
    ),
    apiGetRequired<{
      authorized_browser_ready: boolean;
      publishing_mode: "assisted" | "authorized_browser";
      channel_name: string;
    }>("/api/settings/full", (value): value is {
      authorized_browser_ready: boolean;
      publishing_mode: "assisted" | "authorized_browser";
      channel_name: string;
    } =>
      typeof value === "object" &&
      value !== null &&
      "authorized_browser_ready" in value &&
      typeof value.authorized_browser_ready === "boolean" &&
      "publishing_mode" in value &&
      (value.publishing_mode === "assisted" ||
        value.publishing_mode === "authorized_browser") &&
      "channel_name" in value &&
      typeof value.channel_name === "string",
    ),
    apiGetRequired<PublisherQueueStatus>(
      "/api/publisher/queue",
      isPublisherQueueStatus,
    ),
  ]);

  return (
    <LineupCalendar
      initialLineup={lineup}
      initialPublished={published}
      publishingEnabled={settings.authorized_browser_ready}
      publishingMode={settings.publishing_mode}
      channelName={settings.channel_name}
      initialPublisherQueue={publisherQueue}
    />
  );
}
