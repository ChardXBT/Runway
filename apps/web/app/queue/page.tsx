/* eslint-disable @next/next/no-img-element */

import { API_URL, apiGet } from "@/lib/api";
import type { Proposal } from "@/lib/types";

type Schedule = {
  timezone: string;
  default_time: string;
  posts_per_day: number;
  coverage: number;
  next_available_at: string;
  scheduled: Proposal[];
};

const formatter = new Intl.DateTimeFormat("en-US", {
  weekday: "short",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  hour12: true,
  timeZone: "America/Toronto",
});

export default async function QueuePage() {
  const queue = await apiGet<Schedule>("/api/queue", {
    timezone: "America/Toronto",
    default_time: "10:00",
    posts_per_day: 1,
    coverage: 0,
    next_available_at: new Date().toISOString(),
    scheduled: [],
  });

  return (
    <>
      <header className="page-header schedule-header">
        <div>
          <p className="eyebrow">Qlob schedule</p>
          <h1>{queue.coverage} bot post{queue.coverage === 1 ? "" : "s"} lined up.</h1>
          <p className="lede">
            LeeWay takes the next open 10:00 AM Eastern day. There is no horizon cap, and
            manually added posts do not affect the one-bot-post-per-day rule.
          </p>
        </div>
        <div className="schedule-next">
          <span>Next open slot</span>
          <strong>{formatter.format(new Date(queue.next_available_at))}</strong>
        </div>
      </header>

      {queue.scheduled.length ? (
        <section className="schedule-list" aria-label="Bot scheduled posts">
          {queue.scheduled.map((proposal, index) => (
            <article className="schedule-item" key={proposal.id}>
              <span className="schedule-index">
                {String(index + 1).padStart(2, "0")}
              </span>
              <div className="schedule-thumb">
                {proposal.candidate?.preview_url && (
                  <img
                    src={`${API_URL}${proposal.candidate.preview_url}`}
                    alt=""
                  />
                )}
              </div>
              <div className="schedule-copy">
                <time>
                  {formatter.format(
                    new Date(
                      proposal.scheduled_publish_at ??
                        proposal.planned_publish_at,
                    ),
                  )}
                </time>
                <strong>{proposal.final_caption}</strong>
              </div>
              <span className={`status status-${proposal.status}`}>
                {proposal.status.replaceAll("_", " ")}
              </span>
            </article>
          ))}
        </section>
      ) : (
        <section className="panel empty">
          <div>
            <strong>No bot posts are scheduled yet.</strong>
            Approve an option in Review and it will appear here.
          </div>
        </section>
      )}

      <p className="offline-note">
        {queue.timezone} · {queue.default_time} · maximum one LeeWay post per day
      </p>
    </>
  );
}
