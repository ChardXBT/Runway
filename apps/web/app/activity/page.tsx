import { apiGet } from "@/lib/api";

type Event = { id: number; event_type: string; entity_type: string; entity_id: number | null; details: Record<string, unknown>; created_at: string };

export default async function ActivityPage() { const events = await apiGet<Event[]>("/api/activity?limit=200", []); return <><header className="header-row"><div><p className="eyebrow">Audit history</p><h1>Every important change.</h1><p className="lede">Capture, analysis, search, generation, approval, and internal scheduling events remain inspectable.</p></div></header><section className="activity-list">{events.map((event) => <article key={event.id}><time>{new Date(event.created_at).toLocaleString()}</time><div><strong>{event.event_type.replaceAll("_", " ")}</strong><span>{event.entity_type}{event.entity_id ? ` #${event.entity_id}` : ""}</span></div><code>{JSON.stringify(event.details)}</code></article>)}</section></>; }
