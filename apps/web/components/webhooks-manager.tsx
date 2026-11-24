"use client";

/** Settings > outbound webhooks: endpoints, their event subscriptions,
 * the create-time signing secret (shown once), state and the delivery log
 * with per-attempt outcomes.
 */

import { useCallback, useEffect, useState } from "react";

import { EVENT_TYPES } from "@/lib/event-stream";
import { actionError } from "@/lib/client-api";
import { formatAbsoluteTime } from "@/lib/format";
import { V1 } from "@/lib/v1-client";
import type { DeliveryView, WebhookView } from "@/lib/v1-guards";

export function WebhooksManager() {
  const [webhooks, setWebhooks] = useState<WebhookView[] | null>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setWebhooks((await V1.webhooks.list()).items);
      setLoadError(null);
    } catch (err) {
      setLoadError(actionError(err, "The webhooks could not be loaded."));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    V1.webhooks
      .list()
      .then((page) => {
        if (cancelled) return;
        setWebhooks(page.items);
        setLoadError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(actionError(err, "The webhooks could not be loaded."));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section className="panel webhooks" aria-label="Outbound webhooks">
      <h2 className="section-heading">Outbound webhooks</h2>
      <p className="field-hint">
        Runway signs and POSTs workspace events to these endpoints. Deliveries
        retry with backoff; an endpoint that keeps failing pauses itself.
      </p>
      {loadError ? (
        <p className="notice error-notice" role="alert">{loadError}</p>
      ) : null}
      {error ? (
        <p className="notice error-notice" role="alert">{error}</p>
      ) : null}
      {secret ? (
        <div className="notice success-notice" role="status">
          <p>
            <strong>Endpoint created.</strong> The signing secret is shown once —
            store it with the receiver.
          </p>
          <code className="release-hash">{secret}</code>
          <button type="button" className="text-link" onClick={() => setSecret(null)}>
            I saved it — dismiss
          </button>
        </div>
      ) : null}
      <CreateWebhookForm
        onCreated={(created) => {
          setSecret(created.secret);
          void refresh();
        }}
        onError={setError}
      />
      {webhooks === null ? (
        <p className="field-hint">Loading endpoints…</p>
      ) : webhooks.length === 0 ? (
        <p className="field-hint">No webhook endpoints yet.</p>
      ) : (
        <ul className="webhook-list clean-list">
          {webhooks.map((endpoint) => (
            <WebhookRow key={endpoint.id} endpoint={endpoint} onChanged={refresh} onError={setError} />
          ))}
        </ul>
      )}
    </section>
  );
}

function CreateWebhookForm({
  onCreated,
  onError,
}: {
  onCreated: (created: WebhookView & { secret: string }) => void;
  onError: (message: string | null) => void;
}) {
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");
  const [eventTypes, setEventTypes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const create = async () => {
    if (!url.trim()) return;
    setBusy(true);
    onError(null);
    try {
      const created = await V1.webhooks.create({
        url: url.trim(),
        description: description.trim() || null,
        event_types: eventTypes,
      });
      onCreated(created);
      setUrl("");
      setDescription("");
      setEventTypes([]);
    } catch (err) {
      onError(actionError(err, "The endpoint could not be created."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="create-webhook">
      <summary className="button secondary">New endpoint</summary>
      <div className="field">
        <label htmlFor="webhook-url">Endpoint URL</label>
        <input
          id="webhook-url"
          type="url"
          value={url}
          placeholder="https://example.test/runway-events"
          onChange={(event) => setUrl(event.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="webhook-description">Description (optional)</label>
        <input
          id="webhook-description"
          type="text"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
      </div>
      <fieldset>
        <legend>Event types (empty = all)</legend>
        {EVENT_TYPES.filter((type) => type !== "resync").map((type) => (
          <label key={type} className="switch-label">
            <input
              type="checkbox"
              checked={eventTypes.includes(type)}
              onChange={(event) =>
                setEventTypes((current) =>
                  event.target.checked
                    ? [...current, type]
                    : current.filter((item) => item !== type),
                )
              }
            />
            {type}
          </label>
        ))}
      </fieldset>
      <button type="button" className="button" onClick={create} disabled={busy || !url.trim()}>
        {busy ? "Creating…" : "Create endpoint"}
      </button>
    </details>
  );
}

function WebhookRow({
  endpoint,
  onChanged,
  onError,
}: {
  endpoint: WebhookView;
  onChanged: () => void;
  onError: (message: string | null) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [deliveries, setDeliveries] = useState<DeliveryView[] | null>(null);
  const [expanded, setExpanded] = useState(false);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    onError(null);
    try {
      await action();
      onChanged();
    } catch (err) {
      onError(actionError(err, "The change could not be saved."));
      setBusy(false);
    }
  };

  const toggleDeliveries = async () => {
    if (expanded) {
      setExpanded(false);
      return;
    }
    setExpanded(true);
    if (deliveries === null) {
      try {
        setDeliveries((await V1.webhooks.deliveries(endpoint.id)).items);
      } catch {
        setDeliveries([]);
      }
    }
  };

  const enabled = endpoint.state === "active";
  return (
    <li className="webhook-row" data-testid={`webhook-${endpoint.id}`}>
      <div className="webhook-row-head">
        <strong>{endpoint.description || endpoint.url}</strong>
        <span className={`status ${enabled ? "status-tone-success" : "status-tone-warning"}`}>
          {endpoint.state}
        </span>
      </div>
      <code className="webhook-url">{endpoint.url}</code>
      <div className="account-scopes">
        {endpoint.event_types.length
          ? endpoint.event_types.map((type) => (
              <span className="pill" key={type}>{type}</span>
            ))
          : <span className="pill">all events</span>}
      </div>
      <p className="field-hint">
        {endpoint.last_success_at
          ? `Last delivered ${formatAbsoluteTime(endpoint.last_success_at)}`
          : "No successful delivery yet"}
        {endpoint.consecutive_failures > 0
          ? ` · ${endpoint.consecutive_failures} failures in a row`
          : ""}
        {endpoint.last_error ? ` · ${endpoint.last_error}` : ""}
      </p>
      <div className="form-actions">
        <button
          type="button"
          className="button secondary"
          disabled={busy}
          onClick={() =>
            void run(() =>
              V1.webhooks.update(endpoint.id, {
                state: enabled ? "disabled" : "active",
              }),
            )
          }
        >
          {enabled ? "Disable" : "Enable"}
        </button>
        <button type="button" className="text-link" onClick={toggleDeliveries} aria-expanded={expanded}>
          {expanded ? "Hide deliveries" : "Deliveries"}
        </button>
        <button
          type="button"
          className="text-link text-danger"
          disabled={busy}
          onClick={() => void run(() => V1.webhooks.remove(endpoint.id))}
        >
          Delete
        </button>
      </div>
      {expanded ? (
        deliveries === null ? (
          <p className="field-hint">Loading deliveries…</p>
        ) : deliveries.length === 0 ? (
          <p className="field-hint">No deliveries yet.</p>
        ) : (
          <ol className="delivery-list">
            {deliveries.map((delivery) => (
              <li key={delivery.id} className="delivery-row">
                <span>{delivery.event_type}</span>
                <span className="attempt-meta">
                  {delivery.state} · {delivery.attempts} attempt{delivery.attempts === 1 ? "" : "s"}
                  {delivery.last_status_code ? ` · HTTP ${delivery.last_status_code}` : ""}
                </span>
                <small>{formatAbsoluteTime(delivery.created_at)}</small>
                {delivery.last_error ? (
                  <small className="text-danger">{delivery.last_error}</small>
                ) : null}
              </li>
            ))}
          </ol>
        )
      ) : null}
    </li>
  );
}
