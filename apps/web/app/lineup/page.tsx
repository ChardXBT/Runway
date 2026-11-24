import { LineupCalendar } from "@/components/lineup-calendar";
import { apiGetOptional, apiGetRequired } from "@/lib/api";
import {
  isLineupSchedule,
  isPublisherQueueStatus,
  proposalList,
} from "@/lib/guards";
import { isPostPage, type PostPage } from "@/lib/v1-guards";
import type {
  LineupSchedule,
  Proposal,
  PublisherQueueStatus,
} from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function LineupPage() {
  const [lineup, published, settings, publisherQueue, posts] = await Promise.all([
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
    // Optional: an API without the v1 posts routes shows the Lineup alone.
    apiGetOptional<PostPage>("/api/v1/posts", isPostPage),
  ]);

  const postTargets = (posts?.items ?? []).flatMap((post) =>
    post.targets
      .filter((target) => target.planned_at !== null)
      .map((target) => ({
        postId: post.id,
        targetId: target.id,
        platform: target.platform,
        postType: target.post_type,
        state: target.state,
        plannedAt: target.planned_at as string,
      })),
  );

  return (
    <LineupCalendar
      initialLineup={lineup}
      initialPublished={published}
      publishingEnabled={settings.authorized_browser_ready}
      publishingMode={settings.publishing_mode}
      channelName={settings.channel_name}
      initialPublisherQueue={publisherQueue}
      postTargets={postTargets}
    />
  );
}
