/* eslint-disable @next/next/no-img-element */
import Link from "next/link";

import { GenerateButton } from "@/components/generate-button";
import { API_URL, apiGet } from "@/lib/api";
import type { Proposal } from "@/lib/types";

type Dashboard = {
  catalogue_count: number;
  queue_coverage: number;
  needs_review: number;
  approved: number;
  gaps: number;
  active_profile_version: number | null;
};

const emptyDashboard: Dashboard = {
  catalogue_count: 0,
  queue_coverage: 0,
  needs_review: 0,
  approved: 0,
  gaps: 10,
  active_profile_version: null,
};

type QueueDay = {
  date: string;
  proposals: Proposal[];
  gap: boolean;
  conflict: boolean;
};

type Queue = {
  timezone: string;
  default_time: string;
  coverage: number;
  days: QueueDay[];
};

const emptyQueue: Queue = {
  timezone: "America/Toronto",
  default_time: "10:00",
  coverage: 0,
  days: [],
};

function dayLabel(value: string) {
  return new Date(`${value}T12:00:00`).toLocaleDateString("en-CA", {
    weekday: "short",
    day: "numeric",
    month: "short",
  });
}

export default async function DashboardPage() {
  const [data, queue] = await Promise.all([
    apiGet<Dashboard>("/api/dashboard", emptyDashboard),
    apiGet<Queue>("/api/queue?days=10", emptyQueue),
  ]);
  const nextAction = data.needs_review
    ? `${data.needs_review} proposal${data.needs_review === 1 ? "" : "s"} waiting for your decision`
    : data.gaps
      ? `${data.gaps} open day${data.gaps === 1 ? "" : "s"} in the runway`
      : "The ten-day runway is covered";

  return (
    <>
      <header className="page-header dashboard-header">
        <div>
          <p className="eyebrow">Desk / Qlob</p>
          <h1>Ten days, one decision at a time.</h1>
          <p className="lede">
            Compare the image, tune the caption, and keep the schedule moving. Every proposal
            remains local until you approve it.
          </p>
        </div>
        <div className="dashboard-actions">
          <GenerateButton />
          <Link className="button secondary" href="/review">
            Open review
          </Link>
        </div>
      </header>

      <section className="metrics-band" aria-label="Workspace status">
        <div className="metric-primary">
          <span>Runway coverage</span>
          <strong>{data.queue_coverage}<i>/10</i></strong>
        </div>
        <div><span>Waiting for review</span><strong>{data.needs_review}</strong></div>
        <div><span>Approved</span><strong>{data.approved}</strong></div>
        <div><span>History indexed</span><strong>{data.catalogue_count}</strong></div>
        <div><span>Profile</span><strong>v{data.active_profile_version ?? "—"}</strong></div>
      </section>

      <div className="dashboard-grid">
        <section className="runway-panel" aria-labelledby="runway-title">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Ten-day runway</p>
              <h2 id="runway-title">{nextAction}</h2>
            </div>
            <Link className="text-link" href="/queue">View schedule</Link>
          </div>
          <div className="runway">
            {queue.days.map((day, index) => {
              const proposal = day.proposals[0];
              const content = (
                <>
                  <div className="runway-date">
                    <span>{String(index + 1).padStart(2, "0")}</span>
                    <time>{dayLabel(day.date)}</time>
                  </div>
                  <div className="runway-frame">
                    {proposal?.candidate?.preview_url ? (
                      <img src={`${API_URL}${proposal.candidate.preview_url}`} alt="" />
                    ) : (
                      <span aria-hidden="true">+</span>
                    )}
                  </div>
                  <div className="runway-caption">
                    <strong>{proposal?.final_caption || "Open day"}</strong>
                    <span>{proposal?.status.replaceAll("_", " ") || "No proposal"}</span>
                  </div>
                </>
              );
              return proposal ? (
                <Link
                  href={`/review?id=${proposal.id}`}
                  className={day.conflict ? "runway-day conflict" : "runway-day"}
                  key={day.date}
                >
                  {content}
                </Link>
              ) : (
                <article className="runway-day gap" key={day.date}>
                  {content}
                </article>
              );
            })}
          </div>
        </section>

        <aside className="desk-brief">
          <div>
            <p className="eyebrow">Reference engine</p>
            <h2>{data.catalogue_count} posts behind every suggestion.</h2>
            <p>
              Profile v{data.active_profile_version ?? "—"} retrieves visual and caption evidence
              from the complete local Qlob archive.
            </p>
            <Link className="text-link" href="/profile">Inspect the evidence</Link>
          </div>
          <div className="safety-note">
            <span className="lock-signal" aria-hidden="true" />
            <div>
              <strong>No live publishing path</strong>
              <p>Approval and scheduling stop inside LeeWay.</p>
            </div>
          </div>
        </aside>
      </div>
    </>
  );
}
