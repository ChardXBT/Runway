"use client";

/** "Also post to…" on an accepted proposal (spec 03 section 7).
 *
 * Builds a cross-post target list from the connected accounts and sends
 * `POST /api/v1/proposals/{id}/cross-post`, then opens the created post in the
 * editor so each target gets its own settings, caption and time.
 */

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { actionError } from "@/lib/client-api";
import { platformLabel } from "@/components/platform-badge";
import { postTypeLabel, postTypesForPlatform } from "@/lib/post-copy";
import { apiGetRequired } from "@/lib/api";
import { isPlatformsResponse } from "@/lib/guards";
import { V1 } from "@/lib/v1-client";
import type { AccountView, PlatformsResponse } from "@/lib/types";
import type { TargetInput } from "@/lib/v1-guards";

export function CrossPostDialog({
  proposalId,
  proposalTitle,
  onClose,
}: {
  proposalId: number;
  proposalTitle: string;
  onClose: () => void;
}) {
  const router = useRouter();
  const [accounts, setAccounts] = useState<AccountView[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [picks, setPicks] = useState<Map<number, string>>(new Map());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    dialogRef.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
    let cancelled = false;
    apiGetRequired<PlatformsResponse>("/api/platforms", isPlatformsResponse)
      .then((response) => {
        if (cancelled) return;
        setAccounts(
          response.platforms.flatMap((platform) => platform.accounts),
        );
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(actionError(err, "The connected accounts could not be loaded."));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [busy, onClose]);

  const toggle = (account: AccountView, postType: string | null) => {
    setPicks((current) => {
      const next = new Map(current);
      if (postType === null || next.get(account.id) === postType) {
        next.delete(account.id);
      } else {
        next.set(account.id, postType);
      }
      return next;
    });
  };

  const submit = async () => {
    if (accounts === null || picks.size === 0) return;
    setBusy(true);
    setError(null);
    const targets: TargetInput[] = [...picks.entries()].map(([accountId, postType]) => {
      const account = accounts.find((item) => item.id === accountId);
      return {
        platform: (account?.platform ?? "youtube") as TargetInput["platform"],
        account_id: accountId,
        post_type: postType,
      };
    });
    try {
      const post = await V1.proposals.crossPost(proposalId, { targets });
      router.push(`/posts/${post.id}`);
    } catch (err) {
      setError(
        actionError(
          err,
          "The cross-post could not be created.",
          "The cross-post may or may not have been created — check Posts before retrying.",
        ),
      );
      setBusy(false);
    }
  };

  return (
    <div className="dialog-backdrop" role="presentation">
      <div
        className="lineup-dialog cross-post-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cross-post-title"
        ref={dialogRef}
      >
        <h2 id="cross-post-title">Also post to…</h2>
        <p className="dialog-intro">
          Turn “{proposalTitle}” into a multi-account post. Each account gets
          its own format — you set the details in the post editor next.
        </p>
        {loadError ? (
          <p className="notice error-notice" role="alert">{loadError}</p>
        ) : accounts === null ? (
          <p className="field-hint">Loading accounts…</p>
        ) : accounts.length === 0 ? (
          <p className="field-hint">No connected accounts yet — connect one under Accounts.</p>
        ) : (
          <ul className="cross-post-accounts clean-list">
            {accounts.map((account) => (
              <li key={account.id} className="cross-post-account">
                <strong>
                  {account.display_name ?? account.handle ?? `account ${account.id}`}
                </strong>
                <small>{platformLabel(account.platform)}</small>
                <div className="cross-post-types">
                  {postTypesForPlatform(account.platform).map((postType) => (
                    <button
                      key={postType}
                      type="button"
                      className={`segmented-option${picks.get(account.id) === postType ? " selected" : ""}`}
                      disabled={account.status !== "connected"}
                      onClick={() => toggle(account, postType)}
                    >
                      {postTypeLabel(postType)}
                    </button>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        )}
        {error ? (
          <p className="dialog-error" role="alert">{error}</p>
        ) : null}
        <div className="dialog-actions">
          <button type="button" className="button secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="button"
            onClick={submit}
            disabled={busy || picks.size === 0}
            data-autofocus
            aria-busy={busy}
          >
            {busy ? "Creating…" : `Create post for ${picks.size} account${picks.size === 1 ? "" : "s"}`}
          </button>
        </div>
      </div>
    </div>
  );
}
