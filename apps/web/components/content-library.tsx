"use client";

import Link from "next/link";
import { useState } from "react";

import { ContentThumb } from "@/components/content-thumb";
import { PlatformBadge, platformLabel } from "@/components/platform-badge";
import { StatePanel } from "@/components/state-panel";
import {
  excerpt,
  formatAbsoluteTime,
  formatCompactNumber,
  formatHandle,
  formatRelativeTime,
} from "@/lib/format";
import type {
  AccountView,
  ContentListResponse,
  PlatformKind,
} from "@/lib/types";

const SORT_COLUMNS: { key: string; label: string }[] = [
  { key: "published_at", label: "Published" },
  { key: "view_count", label: "Views" },
  { key: "like_count", label: "Likes" },
  { key: "comment_count", label: "Comments" },
];

export function ContentLibrary({
  accounts,
  list,
  filters,
  pageSize,
}: {
  accounts: AccountView[];
  list: ContentListResponse;
  filters: {
    platform: string;
    type: string;
    q: string;
    account: string;
    sort: string;
    order: string;
    offset: number;
  };
  pageSize: number;
}) {
  const [qInput, setQInput] = useState(filters.q);

  const href = (overrides: Record<string, string | number>) => {
    const merged: Record<string, string | number> = { ...filters, ...overrides };
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(merged)) {
      if (value !== "" && value !== 0) query.set(key, String(value));
    }
    const text = query.toString();
    return `/content${text ? `?${text}` : ""}`;
  };

  const sortHeader = (key: string, label: string) => {
    const active = filters.sort === key;
    const nextOrder = active && filters.order === "desc" ? "asc" : "desc";
    return (
      <Link href={href({ sort: key, order: nextOrder, offset: 0 })}>
        {label}
        {active ? (filters.order === "desc" ? " ↓" : " ↑") : ""}
      </Link>
    );
  };

  const page = Math.floor(filters.offset / pageSize) + 1;
  const pageCount = Math.max(1, Math.ceil(list.total / pageSize));

  return (
    <div className="content-library">
      <header className="page-header">
        <div className="page-header-copy">
          <p className="eyebrow">Audience</p>
          <h1>Content</h1>
        </div>
        <div className="conveyor-stats">
          <span>
            <strong>{list.total}</strong> items
          </span>
          {Object.entries(list.counts.by_platform).map(([platform, count]) => (
            <span key={platform}>
              <strong>{count}</strong> {platformLabel(platform as PlatformKind)}
            </span>
          ))}
        </div>
      </header>

      <form className="content-filters" action="/content" method="get">
        <label>
          Platform
          <select name="platform" defaultValue={filters.platform}>
            <option value="">All platforms</option>
            {(["youtube", "instagram", "tiktok"] as PlatformKind[]).map(
              (platform) => (
                <option key={platform} value={platform}>
                  {platformLabel(platform)}
                </option>
              ),
            )}
          </select>
        </label>
        <label>
          Type
          <select name="type" defaultValue={filters.type}>
            <option value="">All types</option>
            {Object.keys(list.counts.by_type)
              .sort()
              .map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
          </select>
        </label>
        <label>
          Account
          <select name="account" defaultValue={filters.account}>
            <option value="">All accounts</option>
            {accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {formatHandle(account.handle) ||
                  account.display_name ||
                  `account ${account.id}`}
              </option>
            ))}
          </select>
        </label>
        <input type="hidden" name="sort" value={filters.sort} />
        <input type="hidden" name="order" value={filters.order} />
        <label className="content-search">
          Search
          <input
            type="search"
            name="q"
            value={qInput}
            placeholder="Title or caption"
            onChange={(event) => setQInput(event.target.value)}
          />
        </label>
        <button type="submit" className="button">
          Apply
        </button>
      </form>

      {list.items.length === 0 ? (
        <StatePanel
          variant="empty"
          title="No content imported yet"
          body="Sync a connected account to import its posts, videos, and reels."
          action={{ href: "/accounts" }}
          actionLabel="Open Accounts"
        />
      ) : (
        <section className="panel">
          <table className="data-table content-table">
            <thead>
              <tr>
                <th>Content</th>
                <th>Type</th>
                <th>Platform</th>
                <th>{sortHeader("published_at", "Published")}</th>
                <th className="numeric">{sortHeader("view_count", "Views")}</th>
                <th className="numeric">{sortHeader("like_count", "Likes")}</th>
                <th className="numeric">
                  {sortHeader("comment_count", "Comments")}
                </th>
                <th className="numeric">Shares</th>
                <th>Metrics as of</th>
              </tr>
            </thead>
            <tbody>
              {list.items.map((item) => {
                const label =
                  item.title ?? excerpt(item.caption, 80) ?? "(no caption)";
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
                    <td>
                      <PlatformBadge platform={item.platform} size="sm" label={false} />
                    </td>
                    <td
                      className="nowrap"
                      title={item.published_at ?? undefined}
                    >
                      {item.published_at
                        ? formatAbsoluteTime(item.published_at)
                        : "—"}
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
                    <td className="numeric">
                      {formatCompactNumber(item.share_count)}
                    </td>
                    <td className="nowrap" suppressHydrationWarning>
                      {formatRelativeTime(item.metrics_fetched_at)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="content-pagination">
            {filters.offset > 0 ? (
              <Link
                className="button secondary"
                href={href({ offset: Math.max(0, filters.offset - pageSize) })}
              >
                ← Previous
              </Link>
            ) : (
              <span />
            )}
            <span>
              Page {page} of {pageCount}
            </span>
            {filters.offset + pageSize < list.total ? (
              <Link
                className="button secondary"
                href={href({ offset: filters.offset + pageSize })}
              >
                Next →
              </Link>
            ) : (
              <span />
            )}
          </div>
        </section>
      )}
    </div>
  );
}
