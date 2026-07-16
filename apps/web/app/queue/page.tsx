/* eslint-disable @next/next/no-img-element */
import Link from "next/link";

import { API_URL, apiGet } from "@/lib/api";
import type { Proposal } from "@/lib/types";

type QueueDay = { date: string; proposals: Proposal[]; gap: boolean; conflict: boolean };
type Queue = { timezone: string; default_time: string; coverage: number; days: QueueDay[] };

export default async function QueuePage() {
  const queue = await apiGet<Queue>("/api/queue?days=10", { timezone: "America/Toronto", default_time: "10:00", coverage: 0, days: [] });
  return <><header className="header-row"><div><p className="eyebrow">Ten-day calendar</p><h1>{queue.coverage}/10 days covered.</h1><p className="lede">Every timestamp is stored with the Toronto daylight-saving offset. Rejected days remain visible as gaps.</p></div></header><section className="queue-table">{queue.days.map((day) => { const proposal = day.proposals[0]; return <article className={day.gap ? "queue-row gap" : day.conflict ? "queue-row conflict" : "queue-row"} key={day.date}><time>{new Date(`${day.date}T12:00:00`).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}</time>{proposal ? <><div className="queue-thumb">{proposal.candidate?.preview_url && <img src={`${API_URL}${proposal.candidate.preview_url}`} alt="" />}</div><div className="queue-caption"><strong>{proposal.final_caption}</strong><span>10:00 · {proposal.status.replaceAll("_", " ")}</span></div><Link className="button secondary" href={`/review?id=${proposal.id}`}>Open</Link></> : <><div className="queue-gap">No active proposal</div><span className="status">Gap</span></>}</article>; })}</section><p className="offline-note">Timezone: {queue.timezone} · default time: {queue.default_time}</p></>;
}
