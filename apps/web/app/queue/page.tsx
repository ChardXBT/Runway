/* eslint-disable @next/next/no-img-element */
import Link from "next/link";

import { API_URL, apiGet } from "@/lib/api";
import type { Proposal } from "@/lib/types";

type QueueDay = { date: string; proposals: Proposal[]; gap: boolean; conflict: boolean };
type Queue = { timezone: string; default_time: string; coverage: number; days: QueueDay[] };

export default async function QueuePage() {
  const queue = await apiGet<Queue>("/api/queue?days=10", {
    timezone: "America/Toronto",
    default_time: "10:00",
    coverage: 0,
    days: [],
  });

  return (
    <>
      <header className="page-header">
        <div>
          <p className="eyebrow">Queue / next ten days</p>
          <h1>{queue.coverage}/10 days covered.</h1>
          <p className="lede">
            This is LeeWay’s internal runway. Rejected proposals reopen their day; nothing here
            reaches YouTube.
          </p>
        </div>
        <div className="header-counter">
          <strong>{10 - queue.coverage}</strong>
          <span>open days</span>
        </div>
      </header>

      <section className="queue-table" aria-label="Ten-day internal schedule">
        {queue.days.map((day, index) => {
          const proposal = day.proposals[0];
          return (
            <article
              className={day.gap ? "queue-row gap" : day.conflict ? "queue-row conflict" : "queue-row"}
              key={day.date}
            >
              <span className="queue-index">{String(index + 1).padStart(2, "0")}</span>
              <time>
                {new Date(`${day.date}T12:00:00`).toLocaleDateString("en-CA", {
                  weekday: "long",
                  month: "short",
                  day: "numeric",
                })}
              </time>
              {proposal ? (
                <>
                  <div className="queue-thumb">
                    {proposal.candidate?.preview_url && (
                      <img src={`${API_URL}${proposal.candidate.preview_url}`} alt="" />
                    )}
                  </div>
                  <div className="queue-caption">
                    <strong>{proposal.final_caption}</strong>
                    <span>
                      {queue.default_time} · {proposal.status.replaceAll("_", " ")}
                    </span>
                  </div>
                  <Link className="button secondary" href={`/review?id=${proposal.id}`}>
                    Inspect
                  </Link>
                </>
              ) : (
                <>
                  <div className="queue-empty-frame" aria-hidden="true">+</div>
                  <div className="queue-gap">
                    <strong>Open day</strong>
                    <span>No active proposal</span>
                  </div>
                  <span className="status">Gap</span>
                </>
              )}
            </article>
          );
        })}
      </section>
      <p className="offline-note">
        {queue.timezone} · default review time {queue.default_time} · internal schedule only
      </p>
    </>
  );
}
