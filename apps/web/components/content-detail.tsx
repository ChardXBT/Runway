"use client";

import Link from "next/link";
import { useState } from "react";

import { ContentThumb } from "@/components/content-thumb";
import { MetricTile } from "@/components/metric-tile";
import { PlatformBadge, platformLabel } from "@/components/platform-badge";
import { TrendChart } from "@/components/trend-chart";
import { API_URL } from "@/lib/api";
import { actionError, readApiJson } from "@/lib/client-api";
import {
  excerpt,
  formatAbsoluteTime,
  formatRelativeTime,
} from "@/lib/format";
import { isContentListResponse } from "@/lib/guards";
import type { ContentDetail, ContentItem } from "@/lib/types";

export function ContentDetailView({ detail }: { detail: ContentDetail }) {
  const routerTitle =
    detail.title ?? excerpt(detail.caption, 80) ?? "(no caption)";
  const platform = detail.platform;
  const [group, setGroup] = useState(detail.group);
  const [linking, setLinking] = useState(false);
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<ContentItem[]>([]);
  const [linkError, setLinkError] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [linkBusy, setLinkBusy] = useState(false);

  const viewPoints = detail.metric_history
    .map((entry) => ({
      captured_at: entry.captured_at,
      value: entry.metrics.view_count ?? entry.metrics.views,
    }))
    .filter(
      (point): point is { captured_at: string; value: number } =>
        typeof point.value === "number",
    );

  const searchCandidates = async () => {
    setSearching(true);
    setLinkError(null);
    try {
      const params = new URLSearchParams({ q: query, limit: "8" });
      const response = await fetch(`${API_URL}/api/content?${params}`, {
        cache: "no-store",
      });
      const payload = await readApiJson(response, {
        validate: isContentListResponse,
        failureMessage: "Could not search content.",
      });
      const groupIds = new Set<number>([
        detail.id,
        ...(group?.members.map((member) => member.id) ?? []),
      ]);
      setMatches(payload.items.filter((item) => !groupIds.has(item.id)));
    } catch (err) {
      setLinkError(actionError(err, "Could not search content."));
    } finally {
      setSearching(false);
    }
  };

  const linkCandidate = async (candidate: ContentItem) => {
    if (linkBusy) return;
    setLinkBusy(true);
    setLinkError(null);
    try {
      if (group === null) {
        const response = await fetch(`${API_URL}/api/content/groups`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: routerTitle,
            content_ids: [detail.id, candidate.id],
          }),
        });
        const created = (await readApiJson(response, {
          validate: (value): value is { id: number; title: string | null } =>
            typeof value === "object" &&
            value !== null &&
            typeof (value as { id?: unknown }).id === "number",
          failureMessage: "Could not create the version group.",
        })) as { id: number; title: string | null };
        setGroup({
          id: created.id,
          title: created.title,
          members: [
            {
              id: candidate.id,
              external_id: candidate.external_id,
              title: candidate.title,
              platform: candidate.platform,
            },
          ],
        });
      } else {
        const response = await fetch(
          `${API_URL}/api/content/${candidate.id}/group`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ group_id: group.id }),
          },
        );
        await readApiJson(response, {
          validate: (value): value is { id: number } =>
            typeof value === "object" &&
            value !== null &&
            typeof (value as { id?: unknown }).id === "number",
          failureMessage: "Could not link the version.",
        });
        setGroup({
          ...group,
          members: [
            ...group.members,
            {
              id: candidate.id,
              external_id: candidate.external_id,
              title: candidate.title,
              platform: candidate.platform,
            },
          ],
        });
      }
      setMatches((current) => current.filter((item) => item.id !== candidate.id));
    } catch (err) {
      setLinkError(actionError(err, "Could not link the version."));
    } finally {
      setLinkBusy(false);
    }
  };

  const unlinkMember = async (memberId: number) => {
    if (linkBusy) return;
    setLinkBusy(true);
    setLinkError(null);
    try {
      const response = await fetch(`${API_URL}/api/content/${memberId}/group`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ group_id: null }),
      });
      await readApiJson(response, {
        validate: (value): value is { id: number } =>
          typeof value === "object" &&
          value !== null &&
          typeof (value as { id?: unknown }).id === "number",
        failureMessage: "Could not unlink the version.",
      });
      if (group !== null) {
        const members = group.members.filter((member) => member.id !== memberId);
        setGroup(members.length === 0 ? null : { ...group, members });
      }
    } catch (err) {
      setLinkError(actionError(err, "Could not unlink the version."));
    } finally {
      setLinkBusy(false);
    }
  };

  return (
    <div className="content-detail-page">
      <header className="page-header">
        <div className="page-header-copy">
          <p className="eyebrow">
            <PlatformBadge platform={platform} size="sm" /> {detail.content_type}
          </p>
          <h1>{routerTitle}</h1>
          <p className="lede" suppressHydrationWarning>
            {detail.published_at
              ? `Published ${formatAbsoluteTime(detail.published_at)}`
              : "Publish date unknown"}
            {detail.metrics_fetched_at
              ? ` · metrics from ${formatRelativeTime(detail.metrics_fetched_at)}`
              : ""}
          </p>
        </div>
        {detail.permalink !== null ? (
          <div className="conveyor-stats">
            <a
              className="button"
              href={detail.permalink}
              target="_blank"
              rel="noreferrer"
            >
              Open on {platformLabel(platform)}
            </a>
          </div>
        ) : null}
      </header>

      <section className="content-detail-hero">
        <ContentThumb
          src={detail.thumbnail_url}
          platform={platform}
          width={240}
          height={135}
        />
        <div className="metric-grid">
          <MetricTile
            label="Views"
            value={detail.view_count}
            asOf={detail.metrics_fetched_at}
            source="Platform"
          />
          <MetricTile label="Likes" value={detail.like_count} />
          <MetricTile label="Comments" value={detail.comment_count} />
          <MetricTile label="Shares" value={detail.share_count} />
        </div>
      </section>

      <section className="panel">
        <div className="section-heading">
          <h2>Views over syncs</h2>
        </div>
        {viewPoints.length >= 2 ? (
          <TrendChart
            compact
            label="Views over syncs"
            points={viewPoints.map((point) => ({
              label: formatAbsoluteTime(point.captured_at),
              value: point.value,
            }))}
          />
        ) : (
          <p className="panel-hint">History builds up with each sync.</p>
        )}
      </section>

      {detail.caption !== null ? (
        <section className="panel">
          <div className="section-heading">
            <h2>Caption</h2>
          </div>
          <p className="content-caption">{detail.caption}</p>
        </section>
      ) : null}

      <section className="panel">
        <div className="section-heading">
          <h2>Comments</h2>
          <span className="panel-hint">
            {detail.comment_count_local} imported
          </span>
        </div>
        <Link
          className="button secondary"
          href={`/inbox?unanswered=0&id=&platform=${platform}`}
        >
          Open in Inbox
        </Link>
      </section>

      <section className="panel">
        <div className="section-heading">
          <h2>Published versions</h2>
          {group !== null ? (
            <span className="panel-hint">
              {group.title ?? "Untitled group"} · {group.members.length + 1}{" "}
              versions
            </span>
          ) : (
            <span className="panel-hint">This is the only imported version</span>
          )}
        </div>
        {group !== null ? (
          <ul className="version-list">
            <li className="version-row version-self">
              <PlatformBadge platform={platform} size="sm" />
              <span className="version-title">{routerTitle} (this item)</span>
            </li>
            {group.members.map((member) => (
              <li key={member.id} className="version-row">
                <PlatformBadge platform={member.platform} size="sm" />
                <Link
                  href={`/content/${member.id}`}
                  className="version-title"
                >
                  {member.title ?? `External ID ${member.external_id}`}
                </Link>
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => unlinkMember(member.id)}
                  disabled={linkBusy}
                >
                  Unlink
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        {linking ? (
          <div className="version-link-box">
            <div className="version-link-controls">
              <input
                type="search"
                value={query}
                placeholder="Search imported content…"
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    void searchCandidates();
                  }
                }}
              />
              <button
                type="button"
                className="button secondary"
                onClick={searchCandidates}
                disabled={searching || query.trim() === ""}
              >
                {searching ? "Searching…" : "Search"}
              </button>
              <button
                type="button"
                className="button secondary"
                onClick={() => {
                  setLinking(false);
                  setMatches([]);
                }}
              >
                Done
              </button>
            </div>
            {matches.length > 0 ? (
              <ul className="version-matches">
                {matches.slice(0, 8).map((candidate) => (
                  <li key={candidate.id} className="version-row">
                    <PlatformBadge platform={candidate.platform} size="sm" />
                    <span className="version-title">
                      {candidate.title ??
                        excerpt(candidate.caption, 60) ??
                        "(no caption)"}
                    </span>
                    <button
                      type="button"
                      className="button secondary"
                      onClick={() => linkCandidate(candidate)}
                      disabled={linkBusy}
                    >
                      Link
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : (
          <button
            type="button"
            className="button secondary"
            onClick={() => setLinking(true)}
          >
            Link another version
          </button>
        )}
        {linkError !== null ? (
          <p className="notice error-notice" role="alert">
            {linkError}
          </p>
        ) : null}
      </section>
    </div>
  );
}
