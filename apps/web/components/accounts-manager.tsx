"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { API_URL } from "@/lib/api";
import { ApiError, actionError, readApiJson } from "@/lib/client-api";
import { AvatarImage } from "@/components/account-avatar";
import { PlatformBadge, platformLabel } from "@/components/platform-badge";
import { StatePanel } from "@/components/state-panel";
import { isPlatformsResponse, isSyncRunView } from "@/lib/guards";
import { formatAbsoluteTime, formatHandle, formatRelativeTime } from "@/lib/format";
import type {
  AccountView,
  PlatformCapabilityView,
  PlatformEntry,
  PlatformKind,
  PlatformsResponse,
  SyncRunView,
} from "@/lib/types";

const INSTAGRAM_SCOPE_OPTIONS = [
  "instagram_business_basic",
  "instagram_business_manage_insights",
  "instagram_business_manage_comments",
  "instagram_business_manage_messages",
  "instagram_business_content_publish",
] as const;

const ERROR_COPY: Record<string, string> = {
  invalid_state: "The sign-in link expired or was already used. Start the connection again.",
  expired_state: "The sign-in took too long and expired. Start the connection again.",
  denied: "Access was declined on the provider side. Nothing was connected.",
  exchange_failed: "Runway could not finish the token exchange. Check the client credentials and try again.",
  missing_code: "The provider did not return an authorization code. Try connecting again.",
  unknown_platform: "The callback referenced an unknown platform.",
  provider_error: "The platform returned an error during sign-in. Try again.",
};

const CAPABILITY_CHIP: Record<string, string> = {
  implemented: "Ready",
  supported_not_implemented: "Not implemented",
  approval_required: "Approval required",
  unsupported: "Unsupported",
  unverified: "Unverified",
};

const SETUP_DOCS_URL =
  "https://github.com/ChardXBT/Runway/blob/main/docs/CREATOR_OS_SETUP.md";

function capabilityChip(
  entry: PlatformCapabilityView,
  hasConnectedAccount: boolean,
) {
  if (entry.state === "unsupported") return "Unsupported";
  if (entry.state === "approval_required") return "Approval required";
  if (entry.state === "supported_not_implemented") return "Not implemented";
  if (entry.state === "implemented") {
    if (entry.ready === true) return "Ready";
    if (entry.blocked_by_setting === true) return "Off by setting";
    if (hasConnectedAccount && (entry.missing_scopes?.length ?? 0) > 0) {
      return "Needs permission";
    }
    return "Needs connection";
  }
  return CAPABILITY_CHIP[entry.state] ?? entry.state;
}

function capabilityTone(entry: PlatformCapabilityView, label: string) {
  if (label === "Ready") return "status-tone-success";
  if (label === "Unsupported" || label === "Off by setting") {
    return "status-tone-warning";
  }
  if (label === "Approval required" || label === "Not implemented") {
    return "status-tone-danger";
  }
  return "status-tone-warning";
}

async function postJson<T>(
  path: string,
  body: unknown,
  validate: (value: unknown) => value is T,
  failureMessage: string,
): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return readApiJson(response, { validate, failureMessage });
}

function isAuthorizationUrl(value: unknown): value is { authorization_url: string } {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { authorization_url?: unknown }).authorization_url ===
      "string"
  );
}

function isRunId(value: unknown): value is { run_id: number } {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { run_id?: unknown }).run_id === "number"
  );
}

function isRunList(value: unknown): value is SyncRunView[] {
  return Array.isArray(value) && value.every(isSyncRunView);
}

function AccountNotice({
  connected,
  errorCode,
  onDismiss,
}: {
  connected: string | null;
  errorCode: string | null;
  onDismiss: () => void;
}) {
  if (connected) {
    return (
      <div className="accounts-notice status-tone-success" role="status">
        <span>
          {platformLabel(connected as PlatformKind)} connected. Run a sync to
          pull the latest numbers.
        </span>
        <button type="button" className="accounts-notice-dismiss" onClick={onDismiss} aria-label="Dismiss">
          ×
        </button>
      </div>
    );
  }
  if (errorCode) {
    return (
      <div className="accounts-notice status-tone-danger" role="alert">
        <span>{ERROR_COPY[errorCode] ?? `Connection failed (${errorCode}).`}</span>
        <button type="button" className="accounts-notice-dismiss" onClick={onDismiss} aria-label="Dismiss">
          ×
        </button>
      </div>
    );
  }
  return null;
}

function SyncHistory({ accountId }: { accountId: number }) {
  const [runs, setRuns] = useState<SyncRunView[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      const response = await fetch(
        `${API_URL}/api/platforms/accounts/${accountId}/sync-runs?limit=5`,
        { cache: "no-store" },
      );
      const payload = await readApiJson(response, {
        validate: isRunList,
        failureMessage: "Could not load sync history.",
      });
      setRuns(payload);
      setError(null);
    } catch (err) {
      setError(actionError(err, "Could not load sync history."));
    }
  };

  return (
    <details
      className="account-sync-history"
      onToggle={(event) => {
        if ((event.target as HTMLDetailsElement).open && runs === null) {
          void load();
        }
      }}
    >
      <summary>Sync history</summary>
      {error ? <p className="status-tone-danger">{error}</p> : null}
      {runs === null && !error ? <p>Loading…</p> : null}
      {runs !== null && runs.length === 0 ? <p>No syncs recorded yet.</p> : null}
      {runs !== null && runs.length > 0 ? (
        <ul>
          {runs.map((run) => (
            <li key={run.id}>
              <div className="sync-run-head">
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
                <span suppressHydrationWarning>
                  {run.trigger} · {run.items_fetched} items ·{" "}
                  {formatRelativeTime(run.started_at)}
                </span>
              </div>
              {run.error_summary ? (
                <p className="status-tone-danger">{run.error_summary}</p>
              ) : null}
              <ul className="sync-step-list">
                {run.steps.map((step, index) => (
                  <li key={`${step.step}-${index}`}>
                    <strong>{step.step}</strong>
                    <span>
                      {step.status} · {step.items} items
                      {step.error ? ` — ${step.error}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      ) : null}
    </details>
  );
}

function AccountCard({
  account,
  providerMode,
  onChanged,
}: {
  account: AccountView;
  providerMode: "live" | "mock";
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [messageTone, setMessageTone] = useState<"status-tone-success" | "status-tone-danger" | "status-tone-warning">("status-tone-success");
  const [run, setRun] = useState<SyncRunView | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => stopPolling, [stopPolling]);

  const pollRun = useCallback(
    (runId: number) => {
      stopPolling();
      pollRef.current = setInterval(async () => {
        try {
          const response = await fetch(
            `${API_URL}/api/platforms/sync-runs/${runId}`,
            { cache: "no-store" },
          );
          const payload = await readApiJson(response, {
            validate: isSyncRunView,
            failureMessage: "Could not read sync progress.",
          });
          setRun(payload);
          if (payload.status !== "running" && payload.status !== "queued") {
            stopPolling();
            setBusy(null);
            setMessageTone(
              payload.status === "completed"
                ? "status-tone-success"
                : payload.status === "partial"
                  ? "status-tone-warning"
                  : "status-tone-danger",
            );
            setMessage(
              payload.status === "completed"
                ? `Sync completed — ${payload.items_fetched} items fetched.`
                : `Sync ${payload.status}${payload.error_summary ? ` — ${payload.error_summary}` : ""}.`,
            );
            onChanged();
          }
        } catch {
          stopPolling();
          setBusy(null);
          setMessageTone("status-tone-danger");
          setMessage("Lost track of the sync run. Refresh to see its result.");
        }
      }, 1500);
    },
    [onChanged, stopPolling],
  );

  const startSync = async () => {
    setBusy("sync");
    setMessage(null);
    try {
      const payload = await postJson(
        `/api/platforms/accounts/${account.id}/sync`,
        {},
        isRunId,
        "Could not start the sync.",
      );
      setRun(null);
      pollRun(payload.run_id);
    } catch (err) {
      setBusy(null);
      if (err instanceof ApiError && err.message.includes("already")) {
        setMessageTone("status-tone-warning");
      } else {
        setMessageTone("status-tone-danger");
      }
      setMessage(
        err instanceof ApiError && err.message.includes("running")
          ? "A sync is already running for this account."
          : actionError(err, "Could not start the sync."),
      );
    }
  };

  const reconnect = async () => {
    setBusy("reconnect");
    setMessage(null);
    try {
      const payload = await postJson(
        `/api/platforms/accounts/${account.id}/reconnect`,
        {},
        isAuthorizationUrl,
        "Could not start reconnection.",
      );
      window.location.assign(payload.authorization_url);
    } catch (err) {
      setBusy(null);
      setMessageTone("status-tone-danger");
      setMessage(actionError(err, "Could not start reconnection."));
    }
  };

  const disconnect = async () => {
    if (!confirmDisconnect) {
      setConfirmDisconnect(true);
      return;
    }
    setBusy("disconnect");
    setMessage(null);
    try {
      await postJson(
        `/api/platforms/accounts/${account.id}/disconnect`,
        { confirmed: true },
        (value): value is { account_id: number } =>
          typeof value === "object" &&
          value !== null &&
          typeof (value as { account_id?: unknown }).account_id === "number",
        "Could not disconnect the account.",
      );
      setMessageTone("status-tone-success");
      setMessage("Account disconnected. Historical content was kept.");
      onChanged();
    } catch (err) {
      setMessageTone("status-tone-danger");
      setMessage(actionError(err, "Could not disconnect the account."));
    } finally {
      setBusy(null);
      setConfirmDisconnect(false);
    }
  };

  const disconnected = account.status === "disconnected";
  const needsReauth = account.status === "needs_reauth";

  return (
    <article className="panel account-card" data-testid={`account-${account.id}`}>
      <div className="account-card-head">
        <AvatarImage
          src={account.avatar_url}
          label={account.display_name ?? account.handle ?? "?"}
          size={44}
        />
        <div className="account-card-identity">
          <strong>{account.display_name ?? account.handle ?? "Connected account"}</strong>
          {formatHandle(account.handle) ? (
            <small>{formatHandle(account.handle)}</small>
          ) : null}
        </div>
        <span
          className={`status ${
            needsReauth
              ? "status-tone-danger"
              : disconnected
                ? "status-tone-warning"
                : "status-tone-success"
          }`}
        >
          {needsReauth
            ? "Needs re-authorization"
            : disconnected
              ? "Disconnected"
              : account.freshness === "stale"
                ? "Connected · stale"
                : "Connected"}
        </span>
      </div>

      {providerMode === "mock" ? (
        <p className="account-demo-note">Demo account — fictional data.</p>
      ) : null}

      <div className="account-scopes">
        {account.granted_scopes.map((scope) => (
          <span className="pill" key={scope}>{scope}</span>
        ))}
        {account.scopes_source === "user_declared" ? (
          <small>scopes declared by you</small>
        ) : null}
      </div>

      <dl className="account-times">
        <div>
          <dt>Connected</dt>
          <dd>{formatAbsoluteTime(account.connected_at) || "—"}</dd>
        </div>
        <div>
          <dt>Verified</dt>
          <dd>{formatAbsoluteTime(account.last_verified_at) || "—"}</dd>
        </div>
        <div>
          <dt>Synced</dt>
          <dd suppressHydrationWarning>
            {account.last_sync_at
              ? formatRelativeTime(account.last_sync_at)
              : "never"}
          </dd>
        </div>
      </dl>

      {account.last_error ? (
        <p className="status-tone-danger account-last-error">
          {account.last_error}
        </p>
      ) : null}

      {message ? <p className={messageTone} role="status">{message}</p> : null}

      {run && run.status === "running" ? (
        <ol className="sync-progress" aria-label="Sync progress">
          {run.steps.map((step, index) => (
            <li key={`${step.step}-${index}`} data-status={step.status}>
              {step.step}: {step.status} ({step.items})
            </li>
          ))}
        </ol>
      ) : null}

      <div className="account-actions">
        <button
          type="button"
          className="button sync"
          onClick={startSync}
          disabled={busy !== null || disconnected}
          aria-busy={busy === "sync"}
        >
          Sync now
        </button>
        {providerMode === "live" && !disconnected ? (
          <button
            type="button"
            className="button secondary"
            onClick={reconnect}
            disabled={busy !== null}
          >
            Reconnect
          </button>
        ) : null}
        {account.profile_url ? (
          <a className="text-link" href={account.profile_url} target="_blank" rel="noreferrer">
            Open profile
          </a>
        ) : null}
        {!disconnected ? (
          <button
            type="button"
            className={`button ${confirmDisconnect ? "reject" : "secondary"}`}
            onClick={disconnect}
            disabled={busy !== null}
          >
            {confirmDisconnect ? "Confirm disconnect" : "Disconnect"}
          </button>
        ) : null}
      </div>

      <SyncHistory accountId={account.id} />
    </article>
  );
}

function ImportTokenForm({ onImported }: { onImported: () => void }) {
  const [token, setToken] = useState("");
  const [scopes, setScopes] = useState<string[]>(["instagram_business_basic"]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [tone, setTone] = useState<"status-tone-success" | "status-tone-danger">(
    "status-tone-success",
  );

  const toggleScope = (scope: string) => {
    setScopes((current) =>
      current.includes(scope)
        ? current.filter((item) => item !== scope)
        : [...current, scope],
    );
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      await postJson(
        "/api/platforms/instagram/import-token",
        { access_token: token.trim(), granted_scopes: scopes },
        (value): value is { account_id: number } =>
          typeof value === "object" &&
          value !== null &&
          typeof (value as { account_id?: unknown }).account_id === "number",
        "Could not import the token.",
      );
      setToken("");
      setTone("status-tone-success");
      setMessage("Token verified and account connected.");
      onImported();
    } catch (err) {
      setTone("status-tone-danger");
      setMessage(actionError(err, "Could not import the token."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="import-token">
      <summary>Import a long-lived token</summary>
      <form onSubmit={submit}>
        <label>
          <span>Access token</span>
          <input
            type="password"
            autoComplete="off"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            minLength={8}
            required
          />
        </label>
        <fieldset className="import-token-scopes">
          <legend>Granted scopes</legend>
          {INSTAGRAM_SCOPE_OPTIONS.map((scope) => (
            <label key={scope}>
              <input
                type="checkbox"
                checked={scope === "instagram_business_basic" || scopes.includes(scope)}
                disabled={scope === "instagram_business_basic"}
                onChange={() => toggleScope(scope)}
              />
              <span>{scope}</span>
            </label>
          ))}
        </fieldset>
        {message ? <p className={tone} role="status">{message}</p> : null}
        <button type="submit" className="button" disabled={busy || token.trim().length < 8} aria-busy={busy}>
          Import token
        </button>
      </form>
    </details>
  );
}

export function AccountsManager({
  data,
  connected,
  errorCode,
}: {
  data: PlatformsResponse;
  connected: string | null;
  errorCode: string | null;
}) {
  const [dismissedNotice, setDismissedNotice] = useState(false);
  const [connectError, setConnectError] = useState<Record<string, string>>({});
  const [connectBusy, setConnectBusy] = useState<string | null>(null);
  const [liveData, setLiveData] = useState(data);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${API_URL}/api/platforms`, { cache: "no-store" });
      const payload = await readApiJson(response, {
        validate: isPlatformsResponse,
        failureMessage: "Could not refresh accounts.",
      });
      setLiveData(payload);
    } catch {
      // Keep the server-rendered snapshot on refresh failures.
    }
  }, []);

  const connect = async (platform: PlatformKind) => {
    setConnectBusy(platform);
    setConnectError((current) => ({ ...current, [platform]: "" }));
    try {
      const payload = await postJson(
        `/api/platforms/${platform}/connect`,
        {},
        isAuthorizationUrl,
        "Could not start the connection.",
      );
      window.location.assign(payload.authorization_url);
    } catch (err) {
      setConnectBusy(null);
      setConnectError((current) => ({
        ...current,
        [platform]: actionError(err, "Could not start the connection."),
      }));
    }
  };

  return (
    <>
      <header className="page-header">
        <div>
          <p className="eyebrow">Workspace</p>
          <h1>Accounts</h1>
          <p className="lede">
            Connected platforms, what Runway may do with them, and how fresh
            their data is.
          </p>
        </div>
      </header>

      {!dismissedNotice ? (
        <AccountNotice
          connected={connected}
          errorCode={errorCode}
          onDismiss={() => setDismissedNotice(true)}
        />
      ) : null}

      <div className="accounts-sections">
        {liveData.platforms.map((entry) => (
          <PlatformSection
            key={entry.platform}
            entry={entry}
            providerMode={liveData.provider_mode}
            connectBusy={connectBusy === entry.platform}
            connectError={connectError[entry.platform] ?? null}
            onConnect={() => connect(entry.platform)}
            onChanged={() => {
              void refresh();
            }}
          />
        ))}
      </div>
    </>
  );
}

function PlatformSection({
  entry,
  providerMode,
  connectBusy,
  connectError,
  onConnect,
  onChanged,
}: {
  entry: PlatformEntry;
  providerMode: "live" | "mock";
  connectBusy: boolean;
  connectError: string | null;
  onConnect: () => void;
  onChanged: () => void;
}) {
  const hasConnectedAccount = entry.accounts.some(
    (account) => account.status === "connected",
  );
  const accountCapabilities = new Map(
    entry.accounts.flatMap((account) =>
      account.capabilities.map((capability) => [capability.capability, capability]),
    ),
  );
  const renderedCapabilities = entry.capabilities.map((capability) => {
    const accountEntry = accountCapabilities.get(capability.capability);
    return accountEntry ?? capability;
  });
  return (
    <section className="platform-section" data-platform={entry.platform}>
      <div className="section-heading">
        <h2>
          <PlatformBadge platform={entry.platform} />
        </h2>
        <span
          className={`status ${
            entry.configured ? "status-tone-success" : "status-tone-warning"
          }`}
        >
          {entry.configured ? "Configured" : "Not configured"}
        </span>
      </div>

      <div className="capability-list" role="list">
        {renderedCapabilities.map((capability) => {
          const chip = capabilityChip(capability, hasConnectedAccount);
          return (
            <div className="capability-row" key={capability.capability} role="listitem">
              <span className="capability-name">
                {capability.capability.replace(/_/g, " ")}
              </span>
              <span className={`status ${capabilityTone(capability, chip)}`}>
                {chip}
              </span>
              <small>{capability.detail}</small>
            </div>
          );
        })}
      </div>

      {entry.accounts.length > 0 ? (
        entry.accounts.map((account) => (
          <AccountCard
            key={account.id}
            account={account}
            providerMode={providerMode}
            onChanged={onChanged}
          />
        ))
      ) : entry.configured && providerMode === "live" ? (
        <div className="panel platform-connect">
          <p>Connect {platformLabel(entry.platform)} to sync content and comments.</p>
          {connectError ? <p className="status-tone-danger">{connectError}</p> : null}
          <button
            type="button"
            className="button"
            onClick={onConnect}
            disabled={connectBusy}
            aria-busy={connectBusy}
          >
            Connect {platformLabel(entry.platform)}
          </button>
        </div>
      ) : !entry.configured ? (
        <StatePanel
          variant="not_configured"
          title={`${platformLabel(entry.platform)} is not configured`}
          body={
            <>
              Add{" "}
              {entry.required_env.map((name, index) => (
                <span key={name}>
                  {index > 0 ? " and " : ""}
                  <code>{name}</code>
                </span>
              ))}{" "}
              to <code>.env</code>, then restart the API.
            </>
          }
          action={{ href: SETUP_DOCS_URL }}
          actionLabel="Open the setup guide"
        />
      ) : null}

      {entry.platform === "instagram" && entry.configured && providerMode === "live" ? (
        <ImportTokenForm onImported={onChanged} />
      ) : null}
    </section>
  );
}
