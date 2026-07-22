import { LineupCalendar } from "@/components/lineup-calendar";
import { apiGet } from "@/lib/api";
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

const fallback: LineupSchedule = {
  timezone: "America/Toronto",
  default_time: "10:00",
  posts_per_day: 1,
  coverage: 0,
  next_available_at: new Date(Date.now() + 86_400_000).toISOString(),
  scheduled: [],
};

const fallbackPublisherQueue: PublisherQueueStatus = {
  running: false,
  queued: 0,
  paused: false,
  paused_reason: null,
};

export default async function LineupPage() {
  const [lineup, published, settings, publisherQueue] = await Promise.all([
    apiGet<LineupSchedule>("/api/queue?limit=5000", fallback, isLineupSchedule),
    apiGet<Proposal[]>(
      "/api/proposals?status=published&limit=500",
      [],
      proposalList,
    ),
    apiGet<{
      authorized_browser_ready: boolean;
      publishing_mode: "assisted" | "authorized_browser";
      channel_name: string;
    }>("/api/settings", {
      authorized_browser_ready: false,
      publishing_mode: "assisted",
      channel_name: "Qlob",
    }, (value): value is {
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
    apiGet<PublisherQueueStatus>(
      "/api/publisher/queue",
      fallbackPublisherQueue,
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
