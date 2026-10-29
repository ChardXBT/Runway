"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { ContentThumb } from "@/components/content-thumb";
import { MetricTile } from "@/components/metric-tile";
import { PlatformBadge } from "@/components/platform-badge";
import { StatePanel } from "@/components/state-panel";
import { TrendChart, type TrendPoint } from "@/components/trend-chart";
import { API_URL } from "@/lib/api";
import { actionError, readApiJson } from "@/lib/client-api";
import {
  excerpt,
  formatAbsoluteTime,
  formatCompactNumber,
  formatHandle,
  parseApiDate,
} from "@/lib/format";
import { isSyncRunView } from "@/lib/guards";
import type { AccountAnalytics, AccountView } from "@/lib/types";

const RANGES = ["7", "28", "90"] as const;

const KPI_ORDER = [
  "views",
  "estimatedMinutesWatched",
  "subscribersGained",
  "subscribersLost",
  "likes",
  "comments",
  "shares",
  "reach",
  "follower_count",
];

const METRIC_LABELS: Record<string, string> = {
  views: "Views",
  estimatedMinutesWatched: "Watch time (min)",
  estimated_minutes_watched: "Watch time (min)",
  averageViewDuration: "Avg. view duration (s)",
  average_view_duration: "Avg. view duration (s)",
  subscribersGained: "Subscribers gained",
  subscribers_gained: "Subscribers gained",
  subscribersLost: "Subscribers lost",
  subscribers_lost: "Subscribers lost",
  likes: "Likes",
  comments: "Comments",
  shares: "Shares",
  reach: "Reach",
  follower_count: "Followers",
  followers: "Followers",
  total_views: "Total views",
  total_likes: "Total likes",
  content_count: "Content",
};

const SOURCE_LABELS: Record<string, string> = {
  youtube_analytics_api: "YouTube Analytics",
  instagram_insights_api: "Instagram Insights",
  runway_sync_snapshots: "Runway sync snapshots",
  mock_fixtures: "Demo fixtures",
};

function metricLabel(key: string, fallback?: string | null): string {
  return METRIC_LABELS[key] ?? fallback ?? key;
}

function sourceLabel(source: string | null | undefined): string | undefined {
  if (!source) return undefined;
  return SOURCE_LABELS[source] ?? source;
}

function SeriesBody({
  label,
  points,
  compact = false,
}: {
  label: string;
  points: TrendPoint[];
  compact?: boolean;
}) {
  if (points.length < 2) {
    return (
      <div className="chart-single">
        <strong>
          {points.length === 1 ? formatCompactNumber(points[0].value) : "—"}
        </strong>
        <p className="panel-hint">History builds up with each sync.</p>
      </div>
    );
  }
  return <TrendChart compact={compact} label={label} points={points} />;
}

const SHORT_MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

function isDayOnly(value: string) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value);
}

function shortDay(value: string) {
  const dayMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (dayMatch) {
    const month = SHORT_MONTHS[Number(dayMatch[2]) - 1];
    if (month !== undefined) return `${month} ${Number(dayMatch[3])}`;
    return value;
  }
  const parsed = parseApiDate(value);
  if (parsed === null) return value.slice(5, 10);
  return parsed.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

type SortKey = "view_count" | "like_count" | "comment_count";

export function AnalyticsWorkspace({
  accounts,
  account,
  analytics,
  analyticsFailed,
  range,
}: {
  accounts: AccountView[];
  account: AccountView | null;
  analytics: AccountAnalytics | null;
  analyticsFailed: boolean;
  range: string;
}) {
  const router = useRouter();
  const [sortKey, setSortKey] = useState<SortKey>("view_count");
  const [syncMessage, setSyncMessage] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const syncPoll = useRef<{ cancelled: boolean } | null>(null);

  useEffect(() => {
    return () => {
      if (syncPoll.current !== null) syncPoll.current.cancelled = true;
    };
  }, []);

  const series = analytics?.series ?? [];
  const snapshotSeries = analytics?.snapshot_series ?? [];

  const primaryMetric =
    account?.platform === "youtube"
      ? "views"
      : account?.platform === "instagram"
        ? "reach"
        : "followers";
  const primaryDaily = series.find((entry) => entry.metric === primaryMetric);
  const followerSnapshots = snapshotSeries.find(
    (entry) => entry.metric === "followers",
  );
  const primarySeries = primaryDaily ?? followerSnapshots ?? null;
  const smallMultiples = series.filter((entry) => entry !== primaryDaily);

  const sortedContent = useMemo(() => {
    const rows = [...(analytics?.top_content ?? [])];
    rows.sort((a, b) => (b[sortKey] ?? -1) - (a[sortKey] ?? -1));
    return rows;
  }, [analytics, sortKey]);

  if (accounts.length === 0) {
    return (
      <div className="analytics-workspace">
        <header className="page-header">
          <div className="page-header-copy">
            <p className="eyebrow">Audience</p>
            <h1>Analytics</h1>
            <p className="lede">
              Per-platform performance for one account at a time. Numbers are
              never blended across platforms.
            </p>
          </div>
        </header>
        <StatePanel
          variant="not_connected"
          title="No accounts connected"
          body="Connect a platform account to see per-platform analytics here."
          action={{ href: "/accounts" }}
          actionLabel="Open Accounts"
        />
      </div>
    );
  }

  const accountHref = (accountId: number, nextRange: string) =>
    `/analytics?account=${accountId}&range=${nextRange}`;

  const runSync = async () => {
    if (account === null || syncing) return;
    setSyncing(true);
    setSyncMessage(null);
    const poll = { cancelled: false };
    syncPoll.current = poll;
    try {
      const start = await fetch(
        `${API_URL}/api/platforms/accounts/${account.id}/sync`,
        { method: "POST" },
      );
      const run = await readApiJson(start, {
        validate: isSyncRunView,
        failureMessage: "Could not start the sync.",
      });
      let status = run.status;
      let attempts = 0;
      while (
        (status === "running" || status === "queued") &&
        attempts < 120 &&
        !poll.cancelled
      ) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        if (poll.cancelled) return;
        const check = await fetch(
          `${API_URL}/api/platforms/sync-runs/${run.id}`,
          { cache: "no-store" },
        );
        const updated = await readApiJson(check, {
          validate: isSyncRunView,
          failureMessage: "Could not read sync progress.",
        });
        status = updated.status;
        attempts += 1;
      }
      if (poll.cancelled) return;
      setSyncMessage(
        status === "completed"
          ? "Sync complete. Refreshing analytics."
          : status === "running" || status === "queued"
            ? "Sync is still in progress. Refresh shortly to see updated analytics."
            : `Sync finished with status ${status}. Refreshing analytics.`,
      );
      router.refresh();
    } catch (err) {
      if (!poll.cancelled) {
        setSyncMessage(actionError(err, "Could not start the sync."));
      }
    } finally {
      if (!poll.cancelled) setSyncing(false);
    }
  };

  const latestPoint = (() => {
    const dated: string[] = [];
    for (const entry of series) {
      const last = entry.points[entry.points.length - 1];
      if (last) dated.push(last.day);
    }
    for (const entry of snapshotSeries) {
      const last = entry.points[entry.points.length - 1];
      if (last) dated.push(last.captured_at);
    }
    dated.sort();
    return dated[dated.length - 1] ?? null;
  })();

  const latestPointLabel =
    latestPoint === null
      ? null
      : isDayOnly(latestPoint)
        ? shortDay(latestPoint)
        : formatAbsoluteTime(latestPoint);

  const hasData = series.length > 0 || snapshotSeries.length > 0;

  return (
    <div className="analytics-workspace">
      <header className="page-header">
        <div className="page-header-copy">
          <p className="eyebrow">Audience</p>
          <h1>Analytics</h1>
          <p className="lede">
            Per-platform performance for one account at a time. Numbers are
            never blended across platforms.
          </p>
        </div>
        {latestPointLabel !== null ? (
          <div className="conveyor-stats">
            <span>
              Data through {latestPointLabel}
            </span>
          </div>
        ) : null}
      </header>

      <div className="analytics-controls">
        <div className="segmented" role="group" aria-label="Account">
          {accounts.map((entry) => (
            <Link
              key={entry.id}
              href={accountHref(entry.id, range)}
              className={`segmented-option${entry.id === account?.id ? " active" : ""}`}
            >
              <PlatformBadge platform={entry.platform} size="sm" label={false} />
              <span>
                {formatHandle(entry.handle) || entry.display_name || "account"}
              </span>
            </Link>
          ))}
        </div>
        <div className="segmented" role="group" aria-label="Range">
          {RANGES.map((entry) => (
            <Link
              key={entry}
              href={accountHref(account?.id ?? 0, entry)}
              className={`segmented-option${entry === range ? " active" : ""}`}
            >
              {entry}d
            </Link>
          ))}
        </div>
      </div>

      {analyticsFailed ? (
        <StatePanel
          variant="error"
          title="Analytics could not be loaded"
          body="The analytics API did not answer. Refresh to try again."
        />
      ) : analytics === null || !hasData ? (
        <StatePanel
          variant="empty"
          title="No data yet"
          body="This account has no daily metrics or snapshots in the range. Run a sync to import analytics."
          action={{ onClick: runSync }}
          actionLabel={syncing ? "Syncing…" : "Sync now"}
        />
      ) : (
        <>
          <section className="analytics-kpis" aria-label="Key metrics">
            <div className="metric-grid">
              {[...analytics.kpis]
                .sort((a, b) => {
                  const ai = KPI_ORDER.indexOf(a.key);
                  const bi = KPI_ORDER.indexOf(b.key);
                  return (
                    (ai === -1 ? KPI_ORDER.length : ai) -
                    (bi === -1 ? KPI_ORDER.length : bi)
                  );
                })
                .slice(0, 8)
                .map((kpi) => (
                  <MetricTile
                    key={kpi.key}
                    label={metricLabel(kpi.key, kpi.label)}
                    value={kpi.value}
                    delta={kpi.delta}
                    source={sourceLabel(kpi.source)}
                    asOf={kpi.as_of}
                  />
                ))}
            </div>
          </section>

          {primarySeries !== null && primarySeries.points.length > 0 ? (
            <section className="analytics-primary panel">
              <div className="section-heading">
                <h2>
                  {metricLabel(primarySeries.metric, primarySeries.label)}
                  {primarySeries === followerSnapshots ? (
                    <span className="chart-source"> · Runway sync snapshots</span>
                  ) : null}
                </h2>
              </div>
              <SeriesBody
                label={metricLabel(primarySeries.metric, primarySeries.label)}
                points={primarySeries.points.map(
                  (point): TrendPoint => ({
                    label: shortDay(
                      "day" in point ? point.day : point.captured_at,
                    ),
                    value: point.value,
                  }),
                )}
              />
            </section>
          ) : null}

          {smallMultiples.some((entry) => entry.points.length > 0) ? (
            <section className="analytics-multiples">
              <div className="chart-grid">
                {smallMultiples
                  .filter((entry) => entry.points.length > 0)
                  .map((entry) => (
                    <div key={entry.metric} className="panel chart-card">
                      <div className="section-heading">
                        <h3>{metricLabel(entry.metric, entry.label)}</h3>
                      </div>
                      <SeriesBody
                        compact
                        label={metricLabel(entry.metric, entry.label)}
                        points={entry.points.map((point) => ({
                          label: shortDay(point.day),
                          value: point.value,
                        }))}
                      />
                    </div>
                  ))}
              </div>
            </section>
          ) : null}

          {followerSnapshots !== undefined &&
          primarySeries !== followerSnapshots &&
          followerSnapshots.points.length > 0 ? (
            <section className="analytics-audience panel">
              <div className="section-heading">
                <h2>
                  {metricLabel(
                    followerSnapshots.metric,
                    followerSnapshots.label,
                  )}
                  <span className="chart-source"> · Runway sync snapshots</span>
                </h2>
              </div>
              <SeriesBody
                label={metricLabel(
                  followerSnapshots.metric,
                  followerSnapshots.label,
                )}
                points={followerSnapshots.points.map((point) => ({
                  label: shortDay(point.captured_at),
                  value: point.value,
                }))}
              />
            </section>
          ) : null}

          {sortedContent.length > 0 ? (
            <section className="analytics-top panel">
              <div className="section-heading">
                <h2>Top content</h2>
                <span className="panel-hint">
                  Sorted by{" "}
                  {sortKey === "view_count"
                    ? "views"
                    : sortKey === "like_count"
                      ? "likes"
                      : "comments"}
                </span>
              </div>
              <table className="data-table analytics-table">
                <thead>
                  <tr>
                    <th>Content</th>
                    <th>Type</th>
                    <th className="numeric">
                      <button type="button" onClick={() => setSortKey("view_count")}>
                        Views{sortKey === "view_count" ? " ↓" : ""}
                      </button>
                    </th>
                    <th className="numeric">
                      <button type="button" onClick={() => setSortKey("like_count")}>
                        Likes{sortKey === "like_count" ? " ↓" : ""}
                      </button>
                    </th>
                    <th className="numeric">
                      <button
                        type="button"
                        onClick={() => setSortKey("comment_count")}
                      >
                        Comments{sortKey === "comment_count" ? " ↓" : ""}
                      </button>
                    </th>
                    <th>Published</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedContent.map((item) => {
                    const label =
                      item.title ?? excerpt(item.caption, 60) ?? "(no caption)";
                    return (
                      <tr key={item.id}>
                        <td className="content-cell">
                          <Link
                            href={`/content/${item.id}`}
                            className="content-cell-link"
                          >
                            <ContentThumb src={item.thumbnail_url} platform={item.platform} size={40} />
                            <span className="content-cell-title" title={label}>
                              {label}
                            </span>
                          </Link>
                        </td>
                        <td>
                          <span className="status">{item.content_type}</span>
                        </td>
                        <td className="numeric">
                          {formatCompactNumber(item.view_count)}
                        </td>
                        <td className="numeric">
                          {formatCompactNumber(item.like_count)}
                        </td>
                        <td className="numeric">
                          {formatCompactNumber(item.comment_count)}
                        </td>
                        <td className="nowrap">
                          {item.published_at
                            ? formatAbsoluteTime(item.published_at)
                            : "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </section>
          ) : null}

          <footer className="analytics-footer">
            {analytics.notes.length > 0 ? (
              <ul className="analytics-notes">
                {analytics.notes.map((note, index) => (
                  <li key={index}>{note}</li>
                ))}
              </ul>
            ) : null}
            {analytics.data_sources.length > 0 ? (
              <p className="analytics-sources">
                Sources:{" "}
                {analytics.data_sources.map((source, index) => (
                  <span key={index}>
                    {index > 0 ? " · " : ""}
                    <a href={source.url} target="_blank" rel="noreferrer">
                      {source.label}
                    </a>
                  </span>
                ))}
              </p>
            ) : null}
          </footer>
        </>
      )}
      {syncMessage !== null ? (
        <p className="notice info-notice" role="status">
          {syncMessage}
        </p>
      ) : null}
    </div>
  );
}
