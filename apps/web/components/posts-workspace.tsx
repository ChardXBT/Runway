"use client";

/** `/posts`: every post with its per-target states, newest first. */

import Link from "next/link";
import { useCallback, useState } from "react";

import { PlatformBadge } from "@/components/platform-badge";
import { StatePanel } from "@/components/state-panel";
import { actionError } from "@/lib/client-api";
import { POST_EVENT_TYPES, useEventStream } from "@/lib/event-stream";
import { formatAbsoluteTime, formatRelativeTime } from "@/lib/format";
import { postStatusCopy, postTypeLabel, targetStateCopy } from "@/lib/post-copy";
import { V1 } from "@/lib/v1-client";
import type { PostPage } from "@/lib/v1-guards";

export function PostsWorkspace({ initialPage }: { initialPage: PostPage }) {
  const [page, setPage] = useState(initialPage);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setPage(await V1.posts.list());
    } catch {
      // The next event or the 60s poll retries.
    }
  }, []);

  useEventStream({
    onEvent: (event) => {
      if (POST_EVENT_TYPES.has(event.type)) void refresh();
    },
    onResync: () => void refresh(),
    onPoll: () => void refresh(),
  });

  const loadMore = async () => {
    if (!page.next_cursor) return;
    setLoadingMore(true);
    setLoadMoreError(null);
    try {
      const next = await V1.posts.list(page.next_cursor);
      setPage((current) => ({
        items: [...current.items, ...next.items],
        next_cursor: next.next_cursor,
      }));
    } catch (err) {
      setLoadMoreError(actionError(err, "More posts could not be loaded."));
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <div className="posts-workspace">
      <header className="page-header header-row">
        <div>
          <p className="eyebrow">Publishing</p>
          <h1>Posts.</h1>
          <p className="lede">
            One post, many accounts — each with its own format, settings and time.
          </p>
        </div>
        <Link className="button" href="/posts/new">
          New post
        </Link>
      </header>
      {page.items.length === 0 ? (
        <StatePanel
          variant="empty"
          title="No posts yet"
          body="Create a post to publish to your connected accounts."
        />
      ) : (
        <div className="posts-index">
          {page.items.map((post) => {
            const status = postStatusCopy(post.status);
            const soonest = post.targets
              .map((target) => target.planned_at)
              .filter((value): value is string => value !== null)
              .sort()[0];
            return (
              <Link
                key={post.id}
                href={`/posts/${post.id}`}
                className="panel post-row"
                data-testid={`post-${post.id}`}
              >
                <div className="post-row-head">
                  <strong>{post.title || "Untitled post"}</strong>
                  <span className={`status status-tone-${status.tone}`}>{status.label}</span>
                </div>
                <div className="post-row-targets">
                  {post.targets.map((target) => {
                    const copy = targetStateCopy(target.state);
                    return (
                      <span
                        key={target.id}
                        className={`pill post-target-pill status-tone-${copy.tone}`}
                      >
                        <PlatformBadge platform={target.platform as "youtube" | "instagram" | "tiktok"} size="sm" label={false} />
                        {postTypeLabel(target.post_type)}
                        <em>{copy.label}</em>
                      </span>
                    );
                  })}
                </div>
                <div className="post-row-meta">
                  {soonest ? (
                    <span>
                      Next: <time>{formatRelativeTime(soonest)}</time>
                    </span>
                  ) : (
                    <span>No time set</span>
                  )}
                  <span>Updated {formatAbsoluteTime(post.updated_at)}</span>
                  {post.ai_generated ? <span className="pill">agent-drafted</span> : null}
                </div>
              </Link>
            );
          })}
        </div>
      )}
      {loadMoreError ? (
        <p className="notice error-notice" role="alert">
          {loadMoreError}
        </p>
      ) : null}
      {page.next_cursor ? (
        <button type="button" className="button secondary" onClick={loadMore} disabled={loadingMore}>
          {loadingMore ? "Loading…" : "Load more"}
        </button>
      ) : null}
    </div>
  );
}
