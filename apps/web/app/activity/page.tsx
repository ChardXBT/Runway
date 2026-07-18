import { apiGet } from "@/lib/api";

type Event = {
  id: number;
  event_type: string;
  entity_type: string;
  entity_id: number | null;
  details: Record<string, unknown>;
  created_at: string;
};

export default async function ActivityPage() {
  const events = await apiGet<Event[]>("/api/activity?limit=200", []);

  return (
    <>
      <header className="page-header">
        <div>
          <p className="eyebrow">Activity / immutable audit</p>
          <h1>Every consequential change.</h1>
          <p className="lede">
            Capture, analysis, generation, decisions, and scheduling remain inspectable
            without exposing credentials.
          </p>
        </div>
        <div className="header-counter">
          <strong>{events.length}</strong>
          <span>latest events</span>
        </div>
      </header>
      {events.length ? (
        <section className="activity-list" aria-label="Audit events">
          {events.map((event) => (
            <article key={event.id}>
              <span className="activity-index">#{event.id}</span>
              <time>{new Date(event.created_at).toLocaleString()}</time>
              <div className="activity-name">
                <strong>{event.event_type.replaceAll("_", " ")}</strong>
                <span>
                  {event.entity_type}
                  {event.entity_id ? ` #${event.entity_id}` : ""}
                </span>
              </div>
              <details>
                <summary>Inspect details</summary>
                <pre>{JSON.stringify(event.details, null, 2)}</pre>
              </details>
            </article>
          ))}
        </section>
      ) : (
        <section className="panel empty">
          <div>
            <strong>No audit events yet.</strong>
            Actions that change RunWay’s local state will appear here.
          </div>
        </section>
      )}
    </>
  );
}
