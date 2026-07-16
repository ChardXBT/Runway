import Link from "next/link";

import { apiGet } from "@/lib/api";
import { GenerateButton } from "@/components/generate-button";

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

export default async function DashboardPage() {
  const data = await apiGet<Dashboard>("/api/dashboard", emptyDashboard);
  return (
    <>
      <header className="header-row">
        <div>
          <p className="eyebrow">Qlob planning room</p>
          <h1>Good posts need leeway.</h1>
          <p className="lede">
            A quiet, local workspace for finding the next ten images, shaping the captions,
            and approving every decision yourself.
          </p>
        </div>
        <div className="dashboard-actions"><GenerateButton /><Link className="button secondary" href="/review">Review queue</Link></div>
      </header>
      <section className="stat-grid" aria-label="Queue summary">
        <article className="stat"><span>Days covered</span><strong>{data.queue_coverage}/10</strong></article>
        <article className="stat"><span>Needs review</span><strong>{data.needs_review}</strong></article>
        <article className="stat"><span>Approved</span><strong>{data.approved}</strong></article>
        <article className="stat"><span>Catalogue posts</span><strong>{data.catalogue_count}</strong></article>
      </section>
      <section className="panel">
        <p className="eyebrow">Next ten days</p>
        {data.queue_coverage === 0 ? (
          <div className="empty">
            <div>
              <strong>Your queue is ready for its first fixture batch.</strong>
              Initialize, capture fixtures, build a profile, then generate ten proposals.
            </div>
          </div>
        ) : (
          <p>{data.gaps === 0 ? "Every day is covered." : `${data.gaps} queue gaps remain.`}</p>
        )}
      </section>
      <p className="offline-note">
        Profile {data.active_profile_version ?? "not built"} · localhost only · publishing disabled
      </p>
    </>
  );
}
