import { LineupCalendar } from "@/components/lineup-calendar";
import { apiGet } from "@/lib/api";
import type { LineupSchedule } from "@/lib/types";

const fallback: LineupSchedule = {
  timezone: "America/Toronto",
  default_time: "10:00",
  posts_per_day: 1,
  coverage: 0,
  next_available_at: new Date(Date.now() + 86_400_000).toISOString(),
  scheduled: [],
};

export default async function LineupPage() {
  const [lineup, settings] = await Promise.all([
    apiGet<LineupSchedule>("/api/queue?limit=5000", fallback),
    apiGet<{ publishing_enabled: boolean }>("/api/settings", {
      publishing_enabled: false,
    }),
  ]);

  return (
    <LineupCalendar
      initialLineup={lineup}
      publishingEnabled={settings.publishing_enabled}
    />
  );
}
