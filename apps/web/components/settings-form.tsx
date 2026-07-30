"use client";

import { FormEvent, useMemo, useRef, useState } from "react";

import { API_URL } from "@/lib/api";
import {
  actionError,
  hasUncertainOutcome,
  readApiJson,
} from "@/lib/client-api";
import { isValidTimeZone } from "@/lib/datetime";
import { isSettings, type Settings } from "@/lib/settings";

export type { Settings } from "@/lib/settings";

type FormStatus = {
  kind: "idle" | "saving" | "checking" | "success" | "error";
  message: string;
};

type SettingsFields = {
  timezone: string;
  default_post_time: string;
  duplicate_window_days: number;
};

function settingsMatchFields(settings: Settings, fields: SettingsFields) {
  return (
    settings.timezone === fields.timezone &&
    settings.default_post_time === fields.default_post_time &&
    settings.duplicate_window_days === fields.duplicate_window_days
  );
}

export function SettingsForm({ initial }: { initial: Settings }) {
  const [settings, setSettings] = useState(initial);
  const [draft, setDraft] = useState({
    timezone: initial.timezone,
    defaultPostTime: initial.default_post_time,
    duplicateWindowDays: String(initial.duplicate_window_days),
  });
  const [status, setStatus] = useState<FormStatus>({
    kind: "idle",
    message: "",
  });
  const actionLock = useRef(false);
  const lastAttempt = useRef<SettingsFields | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const actionRunning =
    status.kind === "saving" || status.kind === "checking";
  const timezoneInvalid = !isValidTimeZone(draft.timezone.trim());
  const timeInvalid = !/^([01]\d|2[0-3]):[0-5]\d$/.test(
    draft.defaultPostTime,
  );
  const duplicateWindow = Number(draft.duplicateWindowDays);
  const duplicateWindowInvalid =
    !Number.isInteger(duplicateWindow) || duplicateWindow < 1;
  const timezonePreview = useMemo(() => {
    if (timezoneInvalid || timeInvalid) return null;
    const [hour, minute] = draft.defaultPostTime.split(":").map(Number);
    const readableTime = new Intl.DateTimeFormat("en-US", {
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
      timeZone: "UTC",
    }).format(new Date(Date.UTC(2026, 0, 1, hour, minute)));
    const zoneName =
      new Intl.DateTimeFormat("en-US", {
        timeZone: draft.timezone.trim(),
        timeZoneName: "long",
      })
        .formatToParts(new Date())
        .find((part) => part.type === "timeZoneName")?.value ??
      draft.timezone.trim();
    return `${readableTime} ${zoneName} (${draft.timezone.trim()})`;
  }, [
    draft.defaultPostTime,
    draft.timezone,
    timeInvalid,
    timezoneInvalid,
  ]);

  function updateDraft(
    key: keyof typeof draft,
    value: string,
  ) {
    setDraft((current) => ({ ...current, [key]: value }));
    if (!actionRunning) {
      setStatus({ kind: "idle", message: "" });
    }
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (actionLock.current || uncertain) return;

    const duplicateWindowDays = Number(draft.duplicateWindowDays);
    if (timezoneInvalid) {
      setStatus({
        kind: "error",
        message: "Enter a valid IANA timezone, such as America/Toronto.",
      });
      return;
    }
    if (timeInvalid) {
      setStatus({ kind: "error", message: "Choose a valid default post time." });
      return;
    }
    if (duplicateWindowInvalid) {
      setStatus({
        kind: "error",
        message: "Duplicate window must be a whole number of at least 1 day.",
      });
      return;
    }

    actionLock.current = true;
    setStatus({ kind: "saving", message: "Saving settings…" });
    const fields: SettingsFields = {
      timezone: draft.timezone.trim(),
      default_post_time: draft.defaultPostTime,
      duplicate_window_days: duplicateWindowDays,
    };
    lastAttempt.current = fields;
    setUncertain(false);
    try {
      const response = await fetch(`${API_URL}/api/settings/full`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fields }),
      });
      const payload = await readApiJson(response, {
        validate: isSettings,
        failureMessage: "Settings could not be saved.",
      });
      setSettings(payload);
      setDraft({
        timezone: payload.timezone,
        defaultPostTime: payload.default_post_time,
        duplicateWindowDays: String(payload.duplicate_window_days),
      });
      setStatus({ kind: "success", message: "Settings saved." });
    } catch (error) {
      setUncertain(hasUncertainOutcome(error));
      setStatus({
        kind: "error",
        message: actionError(
          error,
          "Settings could not be saved. Check the local service and try again.",
          "The save response could not be verified. Your entries are preserved; reload before trying again.",
        ),
      });
    } finally {
      actionLock.current = false;
    }
  }

  async function reconcile() {
    if (actionLock.current || !lastAttempt.current) return;
    actionLock.current = true;
    setStatus({
      kind: "checking",
      message: "Checking the saved settings…",
    });
    try {
      const response = await fetch(`${API_URL}/api/settings/full`, {
        cache: "no-store",
      });
      const payload = await readApiJson(response, {
        validate: isSettings,
        failureMessage: "Saved settings could not be checked.",
        malformedMessage:
          "The settings check returned unreadable data. Your entries are still preserved.",
      });
      setSettings(payload);
      setUncertain(false);
      if (settingsMatchFields(payload, lastAttempt.current)) {
        setDraft({
          timezone: payload.timezone,
          defaultPostTime: payload.default_post_time,
          duplicateWindowDays: String(payload.duplicate_window_days),
        });
        setStatus({ kind: "success", message: "Settings save verified." });
      } else {
        setStatus({
          kind: "error",
          message:
            "The save did not take effect. Your unsaved entries are still here; review them and try again.",
        });
      }
    } catch (error) {
      setStatus({
        kind: "error",
        message: actionError(
          error,
          "Saved settings could not be checked. Your entries are still preserved.",
        ),
      });
    } finally {
      actionLock.current = false;
    }
  }

  const modelDescription =
    settings.agent_runtime === "codex"
      ? `${settings.codex_model} · ${settings.codex_reasoning_effort} reasoning · ChatGPT sign-in only. Paid API fallback is disabled.`
      : settings.agent_runtime === "openai"
        ? settings.openai_configured
          ? "Explicit API runtime configured; secret hidden."
          : "API runtime selected but incomplete."
        : "Deterministic offline mock runtime.";

  return (
    <>
      <div
        className={
          settings.authorized_browser_ready
            ? "publishing-banner armed"
            : "publishing-banner"
        }
      >
        <strong>
          {settings.publishing_mode === "assisted"
            ? "Assisted publishing is the active mode."
            : settings.authorized_browser_ready
              ? "Authorized browser publishing is armed."
              : "Authorized browser publishing is locked."}
        </strong>
        <span>
          {settings.publishing_mode === "assisted"
            ? "The explicit Lineup action prepares exact native-YouTube instructions. Accept and calendar edits remain local-only."
            : settings.authorized_browser_ready
              ? `Channel ${settings.publisher_channel_id} · explicit Lineup confirmation · serialized visible browser.`
              : "All authorized-browser interlocks must be configured together. No browser queue work can start."}
        </span>
      </div>
      <form
        className="panel settings-form"
        onSubmit={save}
        aria-busy={actionRunning}
        noValidate
      >
        <label>
          <span>Channel name</span>
          <input
            className="field"
            name="name"
            value={settings.channel_name}
            disabled
          />
          <small>Runway is currently pinned to this channel identity.</small>
        </label>
        <label>
          <span>Handle</span>
          <input className="field" value={settings.channel_handle} disabled />
        </label>
        <div className="two-fields">
          <label>
            <span>Timezone</span>
            <input
              className="field"
              name="timezone"
              aria-label="Timezone"
              value={draft.timezone}
              onChange={(event) => updateDraft("timezone", event.target.value)}
              required
              aria-describedby="timezone-hint"
              aria-invalid={
                status.kind === "error" && timezoneInvalid ? "true" : undefined
              }
              disabled={actionRunning || uncertain}
            />
            <small id="timezone-hint">IANA format, for example America/Toronto</small>
          </label>
          <label>
            <span>Default post time</span>
            <input
              className="field"
              name="default_post_time"
              type="time"
              value={draft.defaultPostTime}
              onChange={(event) =>
                updateDraft("defaultPostTime", event.target.value)
              }
              required
              aria-invalid={
                status.kind === "error" && timeInvalid ? "true" : undefined
              }
              disabled={actionRunning || uncertain}
            />
          </label>
        </div>
        <p className="settings-timezone-preview" role="status">
          <strong>Schedule preview</strong>
          <span>
            {timezonePreview
              ? `New posts start at ${timezonePreview}; each Lineup item can use a different time.`
              : "Enter a valid timezone and time to preview the daily slot."}
          </span>
        </p>
        <label>
          <span>Duplicate window</span>
          <input
            className="field"
            name="duplicate_window_days"
            type="number"
            min="1"
            step="1"
            value={draft.duplicateWindowDays}
            onChange={(event) =>
              updateDraft("duplicateWindowDays", event.target.value)
            }
            required
            aria-invalid={
              status.kind === "error" && duplicateWindowInvalid
                ? "true"
                : undefined
            }
            disabled={actionRunning || uncertain}
          />
        </label>
        <div className="form-actions">
          <button
            className="button"
            type="submit"
            disabled={actionRunning || uncertain}
          >
            {status.kind === "saving" ? "Saving settings…" : "Save settings"}
          </button>
          {uncertain && (
            <button
              className="text-link"
              type="button"
              onClick={reconcile}
              disabled={actionRunning}
            >
              {status.kind === "checking"
                ? "Checking saved state…"
                : "Check saved state"}
            </button>
          )}
        </div>
        <small
          className={`form-message form-message-${status.kind}`}
          id="settings-form-message"
          role={status.kind === "error" ? "alert" : "status"}
          aria-live={status.kind === "error" ? "assertive" : "polite"}
        >
          {status.message}
        </small>
      </form>
      <section className="profile-columns">
        <article className="panel">
          <p className="eyebrow">Model provider</p>
          <h2>{settings.agent_runtime}</h2>
          <p>{modelDescription}</p>
        </article>
        <article className="panel">
          <p className="eyebrow">Browser discovery</p>
          <h2>{settings.browser_search_enabled ? "Enabled" : "Disabled"}</h2>
          <p>Always headed, explicit, and challenge-aware.</p>
        </article>
        <article className="panel">
          <p className="eyebrow">Caption learning</p>
          <h2>{settings.caption_question_first ? "Question first" : "Balanced"}</h2>
          <p>
            Edits, accepts, selections, and rejections become retrieval evidence for the next
            caption pass.
          </p>
        </article>
        <article className="panel">
          <p className="eyebrow">Scheduling policy</p>
          <h2>One bot post daily</h2>
          <p>
            Accepted looks take the next open {settings.default_post_time}{" "}
            {settings.timezone} day. The horizon is uncapped and manual channel posts do
            not consume Runway’s daily slot.
          </p>
        </article>
      </section>
      <section className="panel">
        <p className="eyebrow">Blocked sources</p>
        {settings.blocked_sources.length ? (
          <ul className="clean-list">
            {settings.blocked_sources.map((item) => (
              <li key={item.id}>
                {item.type}: {item.value}
              </li>
            ))}
          </ul>
        ) : (
          <p>No sources are blocked.</p>
        )}
      </section>
    </>
  );
}
