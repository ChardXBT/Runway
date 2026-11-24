"use client";

/** Per-account freshness + publishing capability (spec 08 section 5).
 *
 * Inside each account card: when each scope last refreshed and how it got
 * there (push/poll), the next sync, today's quota, push status, and every
 * post type's capability in words. "Enable publishing" starts the incremental
 * consent and follows its authorization URL.
 */

import { useCallback, useState } from "react";

import { actionError, ApiError } from "@/lib/client-api";
import { useEventStream } from "@/lib/event-stream";
import { formatAbsoluteTime, formatRelativeTime } from "@/lib/format";
import { capabilityStateCopy, postTypeLabel } from "@/lib/post-copy";
import { V1, V1ApiError } from "@/lib/v1-client";
import type { AccountCapabilities, SyncStatusView } from "@/lib/v1-guards";
import type { AccountView } from "@/lib/types";

const SCOPE_LABELS: Record<string, string> = {
  content: "Content",
  inbox: "Inbox",
  analytics: "Analytics",
  audience: "Audience",
};

function quotaSummary(quota: Record<string, unknown>): string | null {
  const entries = Object.entries(quota)
    .filter(([, value]) => typeof value === "number")
    .map(([key, value]) => `${key}: ${String(value)}`);
  return entries.length ? entries.join(" · ") : null;
}

export function AccountFreshness({ account }: { account: AccountView }) {
  const [status, setStatus] = useState<SyncStatusView | null>(null);
  const [capabilities, setCapabilities] = useState<AccountCapabilities | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [syncResult, capabilityResult] = await Promise.allSettled([
      V1.accounts.syncStatus(account.id),
      V1.accounts.publishingCapabilities(account.id),
    ]);
    if (syncResult.status === "fulfilled") {
      setStatus(syncResult.value);
      setError(null);
    } else {
      // An older API without the route: say so instead of hiding the panel.
      setError(
        actionError(
          syncResult.reason,
          "Sync status is unavailable on this API version.",
        ),
      );
    }
    if (capabilityResult.status === "fulfilled") {
      setCapabilities(capabilityResult.value);
    }
  }, [account.id]);

  useEventStream({
    onEvent: (event) => {
      if (
        (event.type === "sync.started" ||
          event.type === "sync.finished" ||
          event.type === "account.updated" ||
          event.type === "quota.low") &&
        String(event.data.subject_id ?? "") === String(account.id)
      ) {
        void refresh();
      }
    },
    onResync: () => void refresh(),
    onPoll: () => void refresh(),
  });

  const toggle = () => {
    const opening = !expanded;
    setExpanded(opening);
    if (opening && status === null) void refresh();
  };

  const enablePublishing = async () => {
    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      const result = await V1.accounts.enablePublishing(account.id);
      if (result.status === "already_enabled") {
        setMessage("Publishing is already enabled for this account.");
        await refresh();
      } else if (result.authorization_url) {
        window.location.assign(result.authorization_url);
      } else {
        setMessage("Publishing consent was requested.");
        await refresh();
      }
    } catch (err) {
      // Instagram's route arrives later; surface its validation message.
      setError(
        err instanceof ApiError
          ? actionError(err, "Publishing could not be enabled for this account.")
          : "Publishing could not be enabled for this account.",
      );
      setBusy(false);
    }
  };

  const needsScope = (capabilities?.items ?? []).some((item) => item.state === "needs_scope");
  return (
    <div className="account-freshness">
      <button
        type="button"
        className="text-link"
        aria-expanded={expanded}
        onClick={toggle}
      >
        {expanded ? "Hide freshness" : "Freshness & publishing"}
      </button>
      {!expanded ? null : (
        <div className="account-freshness-body">
          {error ? (
            <p className="notice error-notice" role="alert">{error}</p>
          ) : null}
          {message ? (
            <p className="form-message form-message-success" role="status">{message}</p>
          ) : null}
          {status === null ? (
            <p className="field-hint">Loading sync status…</p>
          ) : (
            <>
              <p className="field-hint">
                Push updates {status.push_enabled ? "on" : "off"}
                {status.adaptive_sync_enabled ? " · adaptive sync on" : ""}
                {quotaSummary(status.quota) ? ` · quota today: ${quotaSummary(status.quota)}` : ""}
              </p>
              <ul className="freshness-scopes clean-list">
                {status.scopes.map((scope) => (
                  <li key={scope.scope} className="freshness-scope">
                    <strong>{SCOPE_LABELS[scope.scope] ?? scope.scope}</strong>
                    <span>
                      {scope.last_run_at
                        ? `refreshed ${formatRelativeTime(scope.last_run_at)}`
                        : "never refreshed"}
                      {scope.source ? ` (${scope.source})` : ""}
                      {scope.last_status && scope.last_status !== "success" && scope.last_status !== "ok"
                        ? ` — ${scope.last_status}`
                        : ""}
                    </span>
                    {scope.next_run_at ? (
                      <small>next {formatRelativeTime(scope.next_run_at)}</small>
                    ) : null}
                    {scope.last_error ? (
                      <small className="text-danger">{scope.last_error}</small>
                    ) : null}
                  </li>
                ))}
              </ul>
            </>
          )}
          {capabilities === null ? null : (
            <ul className="capability-list">
              {capabilities.items.map((item) => {
                const copy = capabilityStateCopy(item.state);
                return (
                  <li key={item.post_type} className="capability-row">
                    <span className={`status status-tone-${copy.tone}`}>{copy.label}</span>
                    <div>
                      <strong>{postTypeLabel(item.post_type)}</strong>
                      <p className="field-hint">{item.detail}</p>
                      {item.missing_scopes.length ? (
                        <p className="field-hint">
                          missing: {item.missing_scopes.join(", ")}
                        </p>
                      ) : null}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
          {account.status === "connected" && needsScope ? (
            <button
              type="button"
              className="button secondary"
              onClick={enablePublishing}
              disabled={busy}
              aria-busy={busy}
            >
              {busy ? "Opening consent…" : "Enable publishing"}
            </button>
          ) : null}
          {account.status === "connected" && capabilities !== null && !needsScope ? (
            <p className="field-hint">Publishing permissions are in place.</p>
          ) : null}
        </div>
      )}
    </div>
  );
}
