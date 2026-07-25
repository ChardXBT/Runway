import Link from "next/link";

import { apiGetRequired } from "@/lib/api";
import {
  activityCategories,
  activityCategory,
  activitySummary,
  activityTeachesModel,
  activityTitle,
  isActivityCategory,
} from "@/lib/activity";
import { isRecord } from "@/lib/guards";

export const dynamic = "force-dynamic";

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
  searchParams: Promise<{ category?: string; page?: string }>;
}) {
  const params = await searchParams;
  const selectedCategory =
    params.category && isActivityCategory(params.category)
      ? params.category
      : "all";
  const page = Math.max(1, Number.parseInt(params.page ?? "1", 10) || 1);
  const pageSize = 100;
  const query = new URLSearchParams({
    limit: String(pageSize),
    offset: String((page - 1) * pageSize),
  });
  if (selectedCategory !== "all") query.set("category", selectedCategory);
  const events = await apiGetRequired<Event[]>(
    `/api/activity?${query}`,
    isEventList,
  );
  const pageHref = (target: number) => {
    const next = new URLSearchParams();
    if (selectedCategory !== "all") next.set("category", selectedCategory);
    if (target > 1) next.set("page", String(target));
    const encoded = next.toString();
    return encoded ? `/activity?${encoded}` : "/activity";
  };

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
          <strong>{events.length}</strong>
          <span>page {page} · up to {pageSize} events</span>
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
        {(selectedCategory !== "all" || page > 1) && (
          <Link className="text-link" href="/activity">
            Clear
          </Link>
        )}
      </form>
      {events.length ? (
        <section className="activity-list" aria-label="Audit events">
          {events.map((event) => (
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
              {selectedCategory !== "all" || page > 1
                ? "No events match this filter or page."
                : "No audit events yet."}
            </strong>
            {selectedCategory !== "all" || page > 1
              ? "Choose another category or return to an earlier page."
              : "Actions that change Runway’s local state will appear here."}
          </div>
        </section>
      )}
      <nav className="pagination" aria-label="Activity pages">
        {page > 1 ? (
          <Link
            className="button secondary"
            href={pageHref(page - 1)}
          >
            Previous
          </Link>
        ) : (
          <span />
        )}
        <span>Page {page}</span>
        {events.length === pageSize ? (
          <Link
            className="button secondary"
            href={pageHref(page + 1)}
          >
            Next
          </Link>
        ) : (
          <span />
        )}
      </nav>
    </>
  );
}
