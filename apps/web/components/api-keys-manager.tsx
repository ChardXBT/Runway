"use client";

/** Settings > API keys: create keys (secret shown once), revoke, per-key
 * release grants, and the workspace `agent_release` policy (spec 06 section 5).
 * All of it is human-only (`workspace:admin`).
 */

import { useCallback, useEffect, useState } from "react";

import { actionError } from "@/lib/client-api";
import { formatAbsoluteTime } from "@/lib/format";
import { V1 } from "@/lib/v1-client";
import type {
  ApiKeyView,
  CreatedApiKey,
  PolicyResponse,
  ReleaseGrantInput,
} from "@/lib/v1-guards";

const SCOPE_LABELS: Record<string, string> = {
  "accounts:read": "Read accounts",
  "accounts:manage": "Manage accounts",
  "content:read": "Read content",
  "inbox:read": "Read inbox",
  "inbox:draft": "Draft replies",
  "inbox:reply": "Send replies",
  "media:write": "Upload media",
  "posts:read": "Read posts",
  "posts:write": "Create and edit posts",
  "posts:schedule": "Schedule posts",
  "posts:release": "Release posts",
  "workspace:admin": "Workspace admin",
};

const PLATFORMS = ["youtube", "instagram", "tiktok"] as const;

function sameScopes(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((scope, index) => scope === b[index]);
}

export function ApiKeysManager() {
  const [keys, setKeys] = useState<ApiKeyView[] | null>(null);
  const [policy, setPolicy] = useState<PolicyResponse | null>(null);
  const [created, setCreated] = useState<CreatedApiKey | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [keyPage, policyView] = await Promise.all([
        V1.keys.list(),
        V1.keys.policy(),
      ]);
      setKeys(keyPage.items);
      setPolicy(policyView);
      setLoadError(null);
    } catch (err) {
      setLoadError(actionError(err, "The API keys could not be loaded."));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void Promise.all([V1.keys.list(), V1.keys.policy()])
      .then(([keyPage, policyView]) => {
        if (cancelled) return;
        setKeys(keyPage.items);
        setPolicy(policyView);
        setLoadError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(actionError(err, "The API keys could not be loaded."));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (loadError) {
    return (
      <section className="panel api-keys" aria-label="API keys">
        <h2 className="section-heading">API keys</h2>
        <p className="notice error-notice" role="alert">{loadError}</p>
      </section>
    );
  }

  return (
    <section className="panel api-keys" aria-label="API keys">
      <h2 className="section-heading">API keys</h2>
      <p className="field-hint">
        Keys let agents read and prepare posts. Releasing always needs either a
        person or an explicit per-key grant.
      </p>
      {policy ? <PolicyEditor policy={policy} onChanged={setPolicy} /> : null}
      {error ? (
        <p className="notice error-notice" role="alert">{error}</p>
      ) : null}
      {created ? (
        <div className="notice success-notice" role="status">
          <p>
            <strong>Key “{created.key.name}” created.</strong> This is the only
            time the secret is shown — copy it now.
          </p>
          <code className="release-hash">{created.secret}</code>
          <button type="button" className="text-link" onClick={() => setCreated(null)}>
            I saved it — dismiss
          </button>
        </div>
      ) : null}
      {policy ? (
        <CreateKeyForm
          defaultScopes={policy.default_agent_scopes}
          perKeyAllowed={policy.agent_release === "per_key"}
          onCreated={(key) => {
            setCreated(key);
            void refresh();
          }}
          onError={setError}
        />
      ) : null}
      {keys === null ? (
        <p className="field-hint">Loading keys…</p>
      ) : keys.length === 0 ? (
        <p className="field-hint">No API keys yet.</p>
      ) : (
        <ul className="key-list clean-list">
          {keys.map((key) => (
            <KeyRow
              key={key.id}
              apiKey={key}
              perKeyAllowed={policy?.agent_release === "per_key"}
              onChanged={refresh}
              onError={setError}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function PolicyEditor({
  policy,
  onChanged,
}: {
  policy: PolicyResponse;
  onChanged: (policy: PolicyResponse) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toggle = async (next: "never" | "per_key") => {
    setBusy(true);
    setError(null);
    try {
      onChanged(await V1.keys.setPolicy({ agent_release: next }));
    } catch (err) {
      setError(actionError(err, "The policy could not be saved."));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="policy-editor">
      <p>
        <strong>Agent releases:</strong>{" "}
        {policy.agent_release === "never"
          ? "keys can never release — every release is human"
          : "keys release only where a per-key grant allows it"}
      </p>
      <div className="segmented" role="group" aria-label="Agent release policy">
        <button
          type="button"
          className={`segmented-option${policy.agent_release === "never" ? " selected" : ""}`}
          onClick={() => void toggle("never")}
          disabled={busy || policy.agent_release === "never"}
        >
          Human only
        </button>
        <button
          type="button"
          className={`segmented-option${policy.agent_release === "per_key" ? " selected" : ""}`}
          onClick={() => void toggle("per_key")}
          disabled={busy || policy.agent_release === "per_key"}
        >
          Per-key grants
        </button>
      </div>
      {error ? (
        <p className="dialog-error" role="alert">{error}</p>
      ) : null}
    </div>
  );
}

function CreateKeyForm({
  defaultScopes,
  perKeyAllowed,
  onCreated,
  onError,
}: {
  defaultScopes: string[];
  perKeyAllowed: boolean;
  onCreated: (key: CreatedApiKey) => void;
  onError: (message: string | null) => void;
}) {
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(defaultScopes);
  const [grants, setGrants] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [seededDefaults, setSeededDefaults] = useState<string[]>(defaultScopes);
  // The defaults can arrive (or change) after the form is mounted: re-seed the
  // checked scopes when the list itself changes.
  if (!sameScopes(seededDefaults, defaultScopes)) {
    setSeededDefaults(defaultScopes);
    setScopes(defaultScopes);
  }
  const scopeOptions = Object.keys(SCOPE_LABELS).filter(
    (scope) => scope !== "workspace:admin" && scope !== "posts:release",
  );

  const create = async () => {
    if (!name.trim()) return;
    setBusy(true);
    onError(null);
    try {
      const release_grants: ReleaseGrantInput[] = grants.map((platform) => ({
        platform: platform as ReleaseGrantInput["platform"],
      }));
      const created = await V1.keys.create({
        name: name.trim(),
        scopes,
        release_grants: perKeyAllowed ? release_grants : [],
      });
      onCreated(created);
      setName("");
      setGrants([]);
    } catch (err) {
      onError(actionError(err, "The key could not be created."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="create-key">
      <summary className="button secondary">New API key</summary>
      <div className="field">
        <label htmlFor="key-name">Name</label>
        <input
          id="key-name"
          type="text"
          value={name}
          placeholder="e.g. nightly planner"
          onChange={(event) => setName(event.target.value)}
        />
      </div>
      <fieldset>
        <legend>Scopes</legend>
        {scopeOptions.map((scope) => (
          <label key={scope} className="switch-label">
            <input
              type="checkbox"
              checked={scopes.includes(scope)}
              onChange={(event) =>
                setScopes((current) =>
                  event.target.checked
                    ? [...current, scope]
                    : current.filter((item) => item !== scope),
                )
              }
            />
            {SCOPE_LABELS[scope]} <small>({scope})</small>
          </label>
        ))}
      </fieldset>
      {perKeyAllowed ? (
        <fieldset>
          <legend>Release grants (per platform)</legend>
          {PLATFORMS.map((platform) => (
            <label key={platform} className="switch-label">
              <input
                type="checkbox"
                checked={grants.includes(platform)}
                onChange={(event) =>
                  setGrants((current) =>
                    event.target.checked
                      ? [...current, platform]
                      : current.filter((item) => item !== platform),
                  )
                }
              />
              May release to {platform}
            </label>
          ))}
        </fieldset>
      ) : null}
      <button type="button" className="button" onClick={create} disabled={busy || !name.trim()}>
        {busy ? "Creating…" : "Create key"}
      </button>
    </details>
  );
}

function KeyRow({
  apiKey,
  perKeyAllowed,
  onChanged,
  onError,
}: {
  apiKey: ApiKeyView;
  perKeyAllowed: boolean;
  onChanged: () => void;
  onError: (message: string | null) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [grants, setGrants] = useState<string[]>(
    apiKey.release_grants.map((grant) => grant.platform),
  );
  const granted = new Set(grants);

  const save = async (next: string[]) => {
    setBusy(true);
    onError(null);
    try {
      await V1.keys.setReleaseGrants(apiKey.id, {
        grants: next.map((platform) => ({
          platform: platform as ReleaseGrantInput["platform"],
        })),
      });
      onChanged();
    } catch (err) {
      onError(actionError(err, "The grants could not be saved."));
      setBusy(false);
    }
  };

  const revoke = async () => {
    setBusy(true);
    onError(null);
    try {
      await V1.keys.revoke(apiKey.id);
      onChanged();
    } catch (err) {
      onError(actionError(err, "The key could not be revoked."));
      setBusy(false);
    }
  };

  return (
    <li className="key-row" data-testid={`key-${apiKey.id}`}>
      <div className="key-row-head">
        <strong>{apiKey.name}</strong>
        <code>{apiKey.prefix}…</code>
        <span
          className={`status ${apiKey.state === "active" ? "status-tone-success" : "status-tone-danger"}`}
        >
          {apiKey.state}
        </span>
      </div>
      <div className="account-scopes">
        {apiKey.scopes.map((scope) => (
          <span className="pill" key={scope}>{scope}</span>
        ))}
      </div>
      <p className="field-hint">
        Created {formatAbsoluteTime(apiKey.created_at)}
        {apiKey.last_used_at ? ` · last used ${formatAbsoluteTime(apiKey.last_used_at)}` : " · never used"}
        {apiKey.expires_at ? ` · expires ${formatAbsoluteTime(apiKey.expires_at)}` : ""}
      </p>
      {perKeyAllowed && apiKey.state === "active" ? (
        <div className="key-grants">
          <span className="field-hint">Release grants:</span>
          {PLATFORMS.map((platform) => (
            <label key={platform} className="switch-label inline">
              <input
                type="checkbox"
                checked={granted.has(platform)}
                disabled={busy}
                onChange={(event) => {
                  const next = event.target.checked
                    ? [...grants, platform]
                    : grants.filter((item) => item !== platform);
                  setGrants(next);
                  void save(next);
                }}
              />
              {platform}
            </label>
          ))}
        </div>
      ) : null}
      {apiKey.state === "active" ? (
        <button type="button" className="text-link text-danger" onClick={revoke} disabled={busy}>
          Revoke
        </button>
      ) : null}
    </li>
  );
}
