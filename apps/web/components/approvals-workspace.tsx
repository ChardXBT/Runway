"use client";

/** `/approvals`: targets an agent prepared that wait for a human release
 * or a rejection with a reason (spec 06 section 4).
 *
 * Release goes through the same per-target release dialog as the post editor:
 * it fetches the post, shows the exact payload + hash + consent, and sends
 * `payload_hash` echo. Reject needs a reason; the person's caption edits can
 * be attached as structured before/after pairs.
 */

import Link from "next/link";
import { useCallback, useState } from "react";

import { PlatformBadge } from "@/components/platform-badge";
import { ReleaseDialog } from "@/components/release-dialog";
import { StatePanel } from "@/components/state-panel";
import { actionError } from "@/lib/client-api";
import { useEventStream } from "@/lib/event-stream";
import { formatAbsoluteTime, formatRelativeTime } from "@/lib/format";
import { postTypeLabel } from "@/lib/post-copy";
import { V1, findTarget } from "@/lib/v1-client";
import type { ApprovalItem, ApprovalPage, PostOut, TargetOut } from "@/lib/v1-guards";

export function ApprovalsWorkspace({ initialPage }: { initialPage: ApprovalPage }) {
  const [page, setPage] = useState(initialPage);
  const [release, setRelease] = useState<{ post: PostOut; target: TargetOut } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setPage(await V1.approvals.list());
    } catch {
      // The next event or poll retries.
    }
  }, []);

  useEventStream({
    onEvent: (event) => {
      if (
        event.type === "target.awaiting_release" ||
        event.type === "target.rejected" ||
        event.type === "target.state_changed"
      ) {
        void refresh();
      }
    },
    onResync: () => void refresh(),
    onPoll: () => void refresh(),
  });

  const openRelease = async (item: ApprovalItem) => {
    setError(null);
    try {
      const post = await V1.posts.get(item.post_id);
      const target = findTarget(post, item.target_id);
      if (!target) {
        setError("That target is no longer on the post — the queue refreshed.");
        void refresh();
        return;
      }
      setRelease({ post, target });
    } catch (err) {
      setError(actionError(err, "The post could not be loaded."));
    }
  };

  return (
    <div className="approvals-workspace">
      <header className="page-header">
        <p className="eyebrow">Agent interface</p>
        <h1>Approvals.</h1>
        <p className="lede">
          Targets an agent prepared. Nothing here publishes until you release it,
          one target at a time.
        </p>
      </header>
      {error ? (
        <p className="notice error-notice" role="alert">
          {error}
        </p>
      ) : null}
      {page.items.length === 0 ? (
        <StatePanel
          variant="empty"
          title="Nothing waiting"
          body="When an agent prepares a post target it lands here for your release."
        />
      ) : (
        <div className="approvals-index">
          {page.items.map((item) => (
            <ApprovalRow key={item.target_id} item={item} onRelease={openRelease} onChanged={refresh} />
          ))}
        </div>
      )}
      {release ? (
        <ReleaseDialog
          post={release.post}
          target={release.target}
          accountLabel={`account ${release.target.account_id ?? ""}`}
          onClose={() => setRelease(null)}
          onReleased={() => {
            setRelease(null);
            void refresh();
          }}
        />
      ) : null}
    </div>
  );
}

function ApprovalRow({
  item,
  onRelease,
  onChanged,
}: {
  item: ApprovalItem;
  onRelease: (item: ApprovalItem) => void;
  onChanged: () => void;
}) {
  const [rejectOpen, setRejectOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [editedCaption, setEditedCaption] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reject = async () => {
    if (!reason.trim()) return;
    setBusy(true);
    setError(null);
    const edits =
      editedCaption !== null && editedCaption !== captionOf(item)
        ? [{ field: "caption", before: captionOf(item), after: editedCaption }]
        : [];
    try {
      await V1.approvals.reject(item.target_id, { reason: reason.trim(), edits });
      onChanged();
    } catch (err) {
      setError(actionError(err, "The rejection could not be sent."));
      setBusy(false);
    }
  };

  return (
    <article className="panel approval-row" data-testid={`approval-${item.target_id}`}>
      <div className="approval-row-head">
        <PlatformBadge platform={item.platform as "youtube" | "instagram" | "tiktok"} />
        <strong>{postTypeLabel(item.post_type)}</strong>
        {item.ai_generated ? <span className="pill">agent-drafted</span> : null}
        {item.planned_at ? (
          <time>{formatRelativeTime(item.planned_at)}</time>
        ) : (
          <span className="field-hint">no time set</span>
        )}
      </div>
      <p className="approval-title">{item.title || "Untitled post"}</p>
      {captionOf(item) ? <p className="approval-caption">{captionOf(item)}</p> : null}
      {item.payload_hash ? (
        <code className="release-hash">hash {item.payload_hash.slice(0, 16)}…</code>
      ) : null}
      <div className="form-actions">
        <button type="button" className="button" onClick={() => void onRelease(item)}>
          Review &amp; release
        </button>
        <Link className="button secondary" href={`/posts/${item.post_id}`}>
          Open post
        </Link>
        <button
          type="button"
          className="button secondary"
          aria-expanded={rejectOpen}
          onClick={() => setRejectOpen((open) => !open)}
        >
          Reject
        </button>
      </div>
      {rejectOpen ? (
        <div className="approval-reject">
          <div className="field">
            <label htmlFor={`reject-reason-${item.target_id}`}>Why are you rejecting it?</label>
            <input
              id={`reject-reason-${item.target_id}`}
              type="text"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="A short reason the agent can learn from"
            />
          </div>
          {captionOf(item) ? (
            <div className="field">
              <label htmlFor={`reject-caption-${item.target_id}`}>Your corrected caption (optional)</label>
              <textarea
                id={`reject-caption-${item.target_id}`}
                rows={3}
                defaultValue={captionOf(item) ?? ""}
                onChange={(event) => setEditedCaption(event.target.value)}
              />
            </div>
          ) : null}
          {error ? (
            <p className="dialog-error" role="alert">
              {error}
            </p>
          ) : null}
          <div className="form-actions">
            <button type="button" className="button reject" onClick={reject} disabled={busy || !reason.trim()}>
              {busy ? "Rejecting…" : "Reject this target"}
            </button>
          </div>
        </div>
      ) : null}
      <p className="field-hint">Prepared {formatAbsoluteTime(item.planned_at)}</p>
    </article>
  );
}

function captionOf(item: ApprovalItem): string | null {
  const payload = item.payload;
  if (!payload) return null;
  const caption = payload.caption ?? payload.text ?? payload.title;
  return typeof caption === "string" ? caption : null;
}
