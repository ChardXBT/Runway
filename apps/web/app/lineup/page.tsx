import { LineupCalendar } from "@/components/lineup-calendar";
import { apiGet } from "@/lib/api";
import { isLineupSchedule, proposalList } from "@/lib/guards";
import type { LineupSchedule, Proposal } from "@/lib/types";

const fallback: LineupSchedule = {
  timezone: "America/Toronto",
  default_time: "10:00",
  posts_per_day: 1,
  coverage: 0,
  next_available_at: new Date(Date.now() + 86_400_000).toISOString(),
  scheduled: [],
};

export default async function LineupPage() {
  const [lineup, published, settings] = await Promise.all([
    apiGet<LineupSchedule>("/api/queue?limit=5000", fallback, isLineupSchedule),
    apiGet<Proposal[]>(
      "/api/proposals?status=published&limit=500",
      [],
      proposalList,
    ),
    apiGet<{ publishing_enabled: boolean }>("/api/settings", {
      publishing_enabled: false,
    }, (value): value is { publishing_enabled: boolean } =>
      typeof value === "object" &&
      value !== null &&
      "publishing_enabled" in value &&
      typeof value.publishing_enabled === "boolean",
    ),
  ]);

  return (
    <LineupCalendar
      initialLineup={lineup}
      initialPublished={published}
      publishingEnabled={settings.publishing_enabled}
    />
  );
}
