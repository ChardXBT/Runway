import Link from "next/link";

import { AvatarImage } from "@/components/account-avatar";
import { PlatformBadge, platformLabel } from "@/components/platform-badge";
import { StatePanel } from "@/components/state-panel";
import {
  excerpt,
  formatAbsoluteTime,
  formatCompactNumber,
  formatHandle,
  formatInteger,
  formatRelativeTime,
} from "@/lib/format";
import type { OverviewResponse, PlatformSummary } from "@/lib/types";

const ACCOUNT_STATE_LABELS: Record<string, { label: string; tone: string }> = {
  connected: { label: "Connected", tone: "status-tone-success" },
  connected_stale: { label: "Connected · stale", tone: "status-tone-warning" },
  needs_reauth: { label: "Needs re-authorization", tone: "status-tone-danger" },
  disconnected: { label: "Disconnected", tone: "status-tone-warning" },
  not_connected: { label: "Not connected", tone: "status-tone-warning" },
  not_configured: { label: "Not configured", tone: "status-tone-warning" },
};

function summaryState(summary: PlatformSummary) {
  if (summary.state === "connected" && summary.freshness === "stale") {
    return ACCOUNT_STATE_LABELS.connected_stale;
  }
  return (
    ACCOUNT_STATE_LABELS[summary.state] ?? {
      label: summary.state,
      tone: "status-tone-warning",
    }
  );
}

function PlatformCard({ summary }: { summary: PlatformSummary }) {
  const state = summaryState(summary);
  return (
    <article className="panel platform-card" data-platform={summary.platform}>
      <div className="platform-card-head">
        <PlatformBadge platform={summary.platform} />
        <span className={`status ${state.tone}`}>{state.label}</span>
      </div>
      {summary.state === "not_configured" ? (
        <div className="platform-card-empty">
          <p>
            Add <code>RUNWAY_{summary.platform.toUpperCase()}_*</code> to{" "}
            <code>.env</code> to connect this platform.
          </p>
          <Link className="text-link" href="/accounts">
            Open Accounts
          </Link>
        </div>
      ) : summary.state === "not_connected" ? (
        <div className="platform-card-empty">
          <p>No account connected yet.</p>
          <Link className="text-link" href="/accounts">
            Connect {summary.platform}
          </Link>
        </div>
      ) : (
        <>
          <div className="platform-card-identity">
            <AvatarImage
              src={summary.avatar_url}
              label={summary.display_name ?? summary.handle ?? "?"}
              size={34}
            />
            <div>
              <strong>{summary.display_name ?? summary.handle ?? "Account"}</strong>
              {formatHandle(summary.handle) ? (
                <small>{formatHandle(summary.handle)}</small>
              ) : null}
            </div>
          </div>
          <dl className="platform-card-metrics">
            <div>
              <dt>{summary.followers_label}</dt>
              <dd>{formatCompactNumber(summary.followers)}</dd>
            </div>
            <div>
              <dt>Content</dt>
              <dd>{formatCompactNumber(summary.content_count)}</dd>
            </div>
            {summary.views_28d !== null ? (
              <div>
                <dt>Views 28d</dt>
                <dd>{formatCompactNumber(summary.views_28d)}</dd>
              </div>
            ) : null}
          </dl>
          <small className="platform-card-sync">
            {summary.last_sync_at ? (
              <>
                Synced{" "}
                <time title={summary.last_sync_at}>
                  {formatRelativeTime(summary.last_sync_at)}
                </time>
              </>
            ) : (
              "Never synced"
            )}
          </small>
        </>
      )}
    </article>
  );
}

export function DashboardOverview({ overview }: { overview: OverviewResponse }) {
  const connectedAccounts = overview.accounts.filter(
    (account) => account.status !== "disconnected",
  ).length;
  const scheduledDrafts = Object.values(overview.drafts.counts).reduce(
    (total, count) => total + count,
    0,
  );
  const hasAccounts = connectedAccounts > 0;
  const topGroups = overview.platform_summaries.filter(
    (summary) => summary.top_content.length > 0,
  );

  return (
    <>
      <header className="page-header">
        <div>
          <p className="eyebrow">Overview</p>
          <h1>Dashboard</h1>
          <p className="lede">
            What is happening across your creator accounts.
          </p>
        </div>
        <div className="conveyor-stats" role="list">
          <span role="listitem">
            <strong>{formatInteger(connectedAccounts)}</strong>connected accounts
          </span>
          <span role="listitem">
            <strong>{formatInteger(overview.inbox.unanswered_total)}</strong>
            unanswered
          </span>
          <span role="listitem">
            <strong>{formatInteger(overview.lineup.in_lineup)}</strong>in Lineup
          </span>
          <span role="listitem">
            <strong>{formatInteger(scheduledDrafts)}</strong>scheduled drafts
          </span>
        </div>
      </header>

      <div className="dashboard-grid">
        <section className="dashboard-row dashboard-cards">
          {overview.platform_summaries.map((summary, index) => (
            <PlatformCard
              key={`${summary.platform}-${summary.account_id ?? index}`}
              summary={summary}
            />
          ))}
        </section>

        <div className="dashboard-row dashboard-middle">
          <section className="panel dashboard-attention">
            <div className="section-heading">
              <h2>Needs attention</h2>
              <span className="platform-card-sync">
                {overview.inbox.unanswered_total} unanswered
                {Object.entries(overview.inbox.by_platform).map(
                  ([platform, count]) => (
                    <span key={platform}>
                      {" · "}
                      {platformLabel(platform)} {count}
                    </span>
                  ),
                )}
              </span>
            </div>
            {overview.inbox.unanswered_total === 0 ? (
              <StatePanel
                variant="empty"
                title={hasAccounts ? "All caught up" : "No connected accounts"}
                body={
                  hasAccounts
                    ? "Every comment has a reply or has been triaged."
                    : "Connect a platform account to see comments that need attention."
                }
                action={hasAccounts ? undefined : { href: "/accounts" }}
                actionLabel="Open Accounts"
              />
            ) : (
              <ul className="dashboard-inbox-list">
                {overview.inbox.latest.map((comment) => (
                  <li key={comment.id}>
                    <Link href={`/inbox?id=${comment.id}&unanswered=0`}>
                      <PlatformBadge platform={comment.platform} size="sm" />
                      <span className="dashboard-inbox-text">
                        <strong>{comment.author_name ?? "Viewer"}</strong>
                        <small>{comment.text}</small>
                      </span>
                      <time title={comment.published_at ?? undefined}>
                        {formatRelativeTime(comment.published_at)}
                      </time>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="panel dashboard-next">
            {overview.warnings.length > 0 ? (
              <ul className="dashboard-warnings" role="status">
                {overview.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            ) : null}
            <div className="section-heading">
              <h2>Up next</h2>
            </div>
            {overview.lineup.next.length === 0 &&
            overview.drafts.scheduled_next.length === 0 ? (
              <StatePanel
                variant="empty"
                title="Nothing scheduled"
                body="Approve a post in Review or schedule a draft to see it here."
                action={{ href: "/review" }}
                actionLabel="Open Review"
              />
            ) : (
              <ul className="dashboard-upnext">
                {overview.lineup.next.map((item) => (
                  <li key={`lineup-${item.id}`}>
                    <Link href="/lineup">
                      {item.image_url ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={item.image_url} alt="" width={40} height={40} />
                      ) : (
                        <span className="dashboard-thumb-empty" aria-hidden="true" />
                      )}
                      <span className="dashboard-inbox-text">
                        <strong>{item.final_caption ?? "Community post"}</strong>
                        <small>
                          {formatAbsoluteTime(item.scheduled_publish_at)} ·{" "}
                          {item.status}
                        </small>
                      </span>
                    </Link>
                  </li>
                ))}
                {overview.drafts.scheduled_next.map((draft) => (
                  <li key={`draft-${draft.id}`}>
                    <Link href={`/drafts#draft-${draft.id}`}>
                      <span className="dashboard-thumb-empty" aria-hidden="true" />
                      <span className="dashboard-inbox-text">
                        <strong>{draft.title}</strong>
                        <small>
                          {draft.scheduled_for
                            ? formatAbsoluteTime(draft.scheduled_for)
                            : "Unscheduled"}{" "}
                          · draft
                        </small>
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>

        <div className="dashboard-row dashboard-bottom">
          <section className="panel">
            <div className="section-heading">
              <h2>Recent performance</h2>
            </div>
            {topGroups.length === 0 ? (
              <StatePanel
                variant="empty"
                title="No synced content yet"
                body="Run a sync from Accounts to pull recent content performance."
                action={{ href: "/accounts" }}
                actionLabel="Open Accounts"
              />
            ) : (
              topGroups.map((summary) => (
                <div className="dashboard-top-group" key={summary.platform}>
                  <div className="dashboard-top-heading">
                    <PlatformBadge platform={summary.platform} size="sm" />
                    {formatHandle(summary.handle) ? (
                      <small>{formatHandle(summary.handle)}</small>
                    ) : null}
                  </div>
                  <table className="dashboard-table">
                    <thead>
                      <tr>
                        <th>Title</th>
                        <th>Type</th>
                        <th>Views</th>
                        <th>Likes</th>
                        <th>Comments</th>
                        <th>Published</th>
                      </tr>
                    </thead>
                    <tbody>
                      {summary.top_content.map((item) => {
                        const label =
                          item.title ??
                          excerpt(item.caption, 60) ??
                          "(no caption)";
                        return (
                          <tr key={item.id}>
                            <td className="dashboard-cell-title" title={label}>
                              {item.permalink ? (
                                <a
                                  href={item.permalink}
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  {label}
                                </a>
                              ) : (
                                label
                              )}
                            </td>
                            <td>{item.content_type}</td>
                            <td className="num">{formatCompactNumber(item.view_count)}</td>
                            <td className="num">{formatCompactNumber(item.like_count)}</td>
                            <td className="num">{formatCompactNumber(item.comment_count)}</td>
                            <td className="num">{formatAbsoluteTime(item.published_at)}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ))
            )}
          </section>

          <section className="panel">
            <div className="section-heading">
              <h2>Sync activity</h2>
            </div>
            {overview.recent_sync_runs.length === 0 ? (
              <StatePanel
                variant="empty"
                title="No syncs yet"
                body="Start a sync from Accounts to populate this list."
              />
            ) : (
              <ul className="dashboard-sync-list">
                {overview.recent_sync_runs.map((run) => (
                  <li key={run.id}>
                    <PlatformBadge platform={run.platform} size="sm" />
                    <span className="dashboard-inbox-text">
                      <strong>
                        {formatHandle(run.handle) || `Account ${run.account_id}`}
                      </strong>
                      <small>
                        {formatInteger(run.items_fetched)} items ·{" "}
                        {formatRelativeTime(run.started_at)}
                        {run.error_summary ? ` · ${run.error_summary}` : ""}
                      </small>
                    </span>
                    <span
                      className={`status ${
                        run.status === "completed"
                          ? "status-tone-success"
                          : run.status === "failed"
                            ? "status-tone-danger"
                            : "status-tone-warning"
                      }`}
                    >
                      {run.status}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </div>

      <p className="dashboard-footer">
        Generated{" "}
        <time title={overview.generated_at}>
          {formatAbsoluteTime(overview.generated_at)}
        </time>{" "}
        · sources shown per panel
      </p>
    </>
  );
}
