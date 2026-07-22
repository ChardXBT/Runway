import Link from "next/link";

import { apiGet } from "@/lib/api";
import {
  activityCategories,
  activityCategory,
  activitySummary,
  activityTeachesModel,
  activityTitle,
  isActivityCategory,
} from "@/lib/activity";
import { isRecord } from "@/lib/guards";

type Event = {
  id: number;
  event_type: string;
  entity_type: string;
  entity_id: number | null;
  details: Record<string, unknown>;
  created_at: string;
};

function isEventList(value: unknown): value is Event[] {
  return (
    Array.isArray(value) &&
    value.every(
      (event) =>
        isRecord(event) &&
        typeof event.id === "number" &&
        typeof event.event_type === "string" &&
        typeof event.entity_type === "string" &&
        (event.entity_id === null || typeof event.entity_id === "number") &&
        isRecord(event.details) &&
        typeof event.created_at === "string" &&
        !Number.isNaN(new Date(event.created_at).getTime()),
    )
  );
}

export default async function ActivityPage({
  searchParams,
}: {
  searchParams: Promise<{ category?: string }>;
}) {
  const params = await searchParams;
  const selectedCategory =
    params.category && isActivityCategory(params.category)
      ? params.category
      : "all";
  const events = await apiGet<Event[]>(
    "/api/activity?limit=500",
    [],
    isEventList,
  );
  const visibleEvents =
    selectedCategory === "all"
      ? events
      : events.filter(
          (event) => activityCategory(event.event_type) === selectedCategory,
        );

  return (
    <>
      <header className="page-header">
        <div>
          <p className="eyebrow">Immutable audit</p>
          <h1>Activity log.</h1>
          <p className="lede">
            Inspect capture, analysis, generation, decisions, and scheduling without
            exposing credentials.
          </p>
        </div>
        <div className="header-counter">
          <strong>{visibleEvents.length}</strong>
          <span>{events.length} latest events checked</span>
        </div>
      </header>
      <form className="activity-filter" method="get">
        <label>
          <span>Show</span>
          <select name="category" defaultValue={selectedCategory}>
            {activityCategories.map(([value, label]) => (
              <option value={value} key={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <button className="button secondary" type="submit">
          Apply filter
        </button>
        {selectedCategory !== "all" && (
          <Link className="text-link" href="/activity">
            Clear
          </Link>
        )}
      </form>
      {visibleEvents.length ? (
        <section className="activity-list" aria-label="Audit events">
          {visibleEvents.map((event) => (
            <article key={event.id}>
              <span className="activity-index">#{event.id}</span>
              <time dateTime={event.created_at}>
                {new Date(event.created_at).toLocaleString()}
              </time>
              <div className="activity-name">
                <span className="activity-category">
                  {activityCategory(event.event_type)}
                </span>
                <strong>{activityTitle(event.event_type)}</strong>
                <p>{activitySummary(event.event_type, event.details)}</p>
                <span>
                  {event.entity_type}
                  {event.entity_id ? ` #${event.entity_id}` : ""}
                </span>
                {activityTeachesModel(event.event_type) && (
                  <span className="learning-label">Teaches Runway</span>
                )}
              </div>
              <details>
                <summary>Technical record</summary>
                <pre>{JSON.stringify(event.details, null, 2)}</pre>
              </details>
            </article>
          ))}
        </section>
      ) : (
        <section className="panel empty">
          <div>
            <strong>
              {events.length
                ? "No events match this filter."
                : "No audit events yet."}
            </strong>
            {events.length
              ? "Choose another activity category."
              : "Actions that change Runway’s local state will appear here."}
          </div>
        </section>
      )}
    </>
  );
}
