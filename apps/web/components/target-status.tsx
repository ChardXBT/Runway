"use client";

/** A target's delivery status in plain language (spec 08).
 *
 * The attempts ledger is expandable. `uncertain` shows what the platform was
 * asked ("what we checked") plus the reconcile actions; `needs_user` explains
 * the TikTok-inbox step.
 */

import { useEffect, useState } from "react";

import { actionError } from "@/lib/client-api";
import { formatAbsoluteTime } from "@/lib/format";
import { targetStateCopy } from "@/lib/post-copy";
import { V1 } from "@/lib/v1-client";
import type { AttemptOut, PostOut, TargetOut } from "@/lib/v1-guards";

const OUTCOME_LABELS: Record<string, string> = {
  success: "Sent",
  published: "Published",
  verified: "Verified",
  failed: "Failed",
  uncertain: "Unclear",
  retryable: "Will retry",
};

function checkedList(attempt: AttemptOut | null): string[] {
  const checked = attempt?.response?.checked;
  return Array.isArray(checked)
    ? checked.filter((item): item is string => typeof item === "string")
    : [];
}

export function TargetStatus({
  post,
  target,
  onChanged,
}: {
  post: PostOut;
  target: TargetOut;
  onChanged: (post: PostOut) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [attempts, setAttempts] = useState<AttemptOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [markUrl, setMarkUrl] = useState("");

  useEffect(() => {
    if (!expanded || attempts !== null) return;
    V1.posts
      .listAttempts(post.id, target.id)
      .then((page) => setAttempts(page.items))
      .catch(() => setAttempts([]));
  }, [expanded, attempts, post.id, target.id]);

  const copy = targetStateCopy(target.state);
  const checked = checkedList(target.last_attempt);
  const run = async (name: string, action: () => Promise<PostOut>) => {
    setBusy(name);
    setError(null);
    try {
      onChanged(await action());
    } catch (err) {
      setError(actionError(err, "The action could not be completed."));
    } finally {
      setBusy(null);
    }
  };

  const publishedUrl = target.published_url;
  // Only http(s) URLs become links — anything else renders as plain text.
  const safeUrl =
    publishedUrl && /^https?:\/\//i.test(publishedUrl) ? publishedUrl : null;
  const platformLink = safeUrl ? (
    <p>
      <a className="text-link" href={safeUrl} target="_blank" rel="noreferrer">
        Open on the platform
      </a>
    </p>
  ) : publishedUrl ? (
    <p className="field-hint">{publishedUrl}</p>
  ) : null;
  return (
    <div className="target-status" data-testid={`target-status-${target.id}`}>
      <div className="target-status-head">
        <span className={`status status-${target.state} status-tone-${copy.tone}`}>
          {copy.label}
        </span>
        <div className="target-status-copy">
          <p>{copy.detail}</p>
          {target.state_reason ? <p className="field-hint">{target.state_reason}</p> : null}
          {/* An uncertain target shows the link below, where it is decided. */}
          {target.state !== "uncertain" ? platformLink : null}
        </div>
      </div>

      {target.state === "uncertain" ? (
        <div className="target-uncertain">
          <p>
            <strong>What we checked</strong>
          </p>
          {checked.length ? (
            <ul className="clean-list">
              {checked.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : (
            <p className="field-hint">
              The platform gave no clear answer; nothing more was confirmed.
            </p>
          )}
          {platformLink}
          <div className="form-actions">
            <button
              type="button"
              className="button secondary"
              disabled={busy !== null}
              onClick={() =>
                run("check", () =>
                  V1.posts.reconcileTarget(post.id, target.id, {
                    resolution: "check",
                    confirmed: true,
                  }),
                )
              }
            >
              {busy === "check" ? "Checking…" : "Check again"}
            </button>
            <button
              type="button"
              className="button secondary"
              disabled={busy !== null}
              onClick={() =>
                run("not_published", () =>
                  V1.posts.reconcileTarget(post.id, target.id, {
                    resolution: "not_published",
                    confirmed: true,
                  }),
                )
              }
            >
              It did not publish
            </button>
          </div>
          <form
            className="target-mark-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (!markUrl.trim()) return;
              void run("mark", () =>
                V1.posts.markPublished(post.id, target.id, {
                  published_url: markUrl.trim(),
                }),
              );
            }}
          >
            <label htmlFor={`mark-url-${target.id}`} className="visually-hidden">
              Published URL
            </label>
            <input
              id={`mark-url-${target.id}`}
              type="url"
              placeholder="Paste the live URL to mark it published"
              value={markUrl}
              onChange={(event) => setMarkUrl(event.target.value)}
            />
            <button type="submit" className="button secondary" disabled={busy !== null || !markUrl.trim()}>
              Mark published
            </button>
          </form>
        </div>
      ) : null}

      {target.state === "needs_user" ? (
        <div className="notice warning-notice">
          This target waits on a step in the TikTok app: the draft is in the
          account&apos;s inbox — open TikTok, finish the post there, then come
          back and mark it published here.
          {target.post_type === "tiktok_draft" ? (
            <form
              className="target-mark-form"
              onSubmit={(event) => {
                event.preventDefault();
                if (!markUrl.trim()) return;
                void run("mark", () =>
                  V1.posts.markPublished(post.id, target.id, {
                    published_url: markUrl.trim(),
                  }),
                );
              }}
            >
              <input
                type="url"
                placeholder="Paste the live TikTok URL"
                value={markUrl}
                onChange={(event) => setMarkUrl(event.target.value)}
              />
              <button type="submit" className="button secondary" disabled={busy !== null || !markUrl.trim()}>
                Mark published
              </button>
            </form>
          ) : null}
        </div>
      ) : null}

      {error ? (
        <p className="dialog-error" role="alert">
          {error}
        </p>
      ) : null}

      <button
        type="button"
        className="text-link"
        aria-expanded={expanded}
        onClick={() => setExpanded((open) => !open)}
      >
        {expanded ? "Hide attempts" : "Show attempts"}
      </button>
      {expanded ? (
        attempts === null ? (
          <p className="field-hint">Loading attempts…</p>
        ) : attempts.length === 0 ? (
          <p className="field-hint">No send attempts yet.</p>
        ) : (
          <ol className="attempt-list">
            {attempts.map((attempt) => (
              <li key={attempt.id} className="attempt-row">
                <span className="attempt-outcome">
                  {OUTCOME_LABELS[attempt.outcome] ?? attempt.outcome}
                </span>
                <span className="attempt-meta">
                  {attempt.adapter} · {attempt.kind} ·{" "}
                  <time>{formatAbsoluteTime(attempt.started_at)}</time>
                </span>
                {attempt.error_detail ? (
                  <small className="text-danger">{attempt.error_detail}</small>
                ) : null}
              </li>
            ))}
          </ol>
        )
      ) : null}
    </div>
  );
}
