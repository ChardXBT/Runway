"use client";

/** The per-target release dialog (spec 06/08).
 *
 * Shows the exact payload being approved, its `payload_hash`, the privacy in
 * words and the platform consent line; one dialog, one target, one release.
 * The button stays inert until the required consent text is echoed.
 */

import { useEffect, useRef, useState } from "react";

import { actionError } from "@/lib/client-api";
import { postTypeLabel } from "@/lib/post-copy";
import { V1 } from "@/lib/v1-client";
import type { PostOut, TargetOut } from "@/lib/v1-guards";

export function ReleaseDialog({
  post,
  target,
  accountLabel,
  onClose,
  onReleased,
}: {
  post: PostOut;
  target: TargetOut;
  accountLabel: string;
  onClose: () => void;
  onReleased: (post: PostOut) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    dialogRef.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [busy, onClose]);

  const notice = target.release_notice;
  const release = async () => {
    setBusy(true);
    setError(null);
    try {
      const updated = await V1.posts.releaseTarget(post.id, target.id, {
        payload_hash: target.payload_hash ?? "",
        confirmed: true,
        consent: notice ?? undefined,
      });
      onReleased(updated);
    } catch (err) {
      setError(
        actionError(
          err,
          "The release could not be sent.",
          "The release may not have reached Runway — refresh before retrying.",
        ),
      );
      setBusy(false);
    }
  };

  return (
    <div className="dialog-backdrop" role="presentation">
      <div
        className="lineup-dialog release-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="release-dialog-title"
        ref={dialogRef}
      >
        <h2 id="release-dialog-title">
          Release {postTypeLabel(target.post_type)} to {accountLabel}?
        </h2>
        <p className="dialog-intro">
          This approves exactly the payload below. Any later edit needs its own
          release.
        </p>
        {post.ai_generated ? (
          <p className="notice warning-notice">This post was drafted by an agent.</p>
        ) : null}
        <dl className="release-facts">
          <div>
            <dt>Who can see it</dt>
            <dd>{target.privacy_in_words ?? "Not chosen yet"}</dd>
          </div>
          <div>
            <dt>Payload hash</dt>
            <dd>
              <code className="release-hash">{target.payload_hash}</code>
            </dd>
          </div>
        </dl>
        <details className="release-payload">
          <summary>The exact payload</summary>
          <pre>{JSON.stringify(target.payload, null, 2)}</pre>
        </details>
        {notice ? <p className="notice info-notice release-notice">{notice}</p> : null}
        {error ? (
          <p className="dialog-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="dialog-actions">
          <button type="button" className="button secondary" onClick={onClose} disabled={busy}>
            Not yet
          </button>
          <button
            type="button"
            className="button"
            onClick={release}
            disabled={busy || !target.releasable || !target.payload_hash}
            data-autofocus
            aria-busy={busy}
          >
            {busy ? "Releasing…" : "Release this exact payload"}
          </button>
        </div>
        {!target.releasable ? (
          <p className="field-hint">This target is not releasable — fix the issues shown on the target first.</p>
        ) : null}
      </div>
    </div>
  );
}
