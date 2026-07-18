"use client";

import { FormEvent, useRef, useState } from "react";

import { API_URL } from "@/lib/api";
import { actionError, readApiJson } from "@/lib/client-api";
import { isValidTimeZone } from "@/lib/datetime";
import { isRecord } from "@/lib/guards";

export type Settings = {
  channel_name: string;
  channel_handle: string;
  timezone: string;
  default_post_time: string;
  duplicate_window_days: number;
  agent_runtime: string;
  codex_model: string;
  codex_reasoning_effort: string;
  codex_chatgpt_auth_required: boolean;
  paid_api_fallback_enabled: boolean;
  openai_configured: boolean;
  browser_search_enabled: boolean;
  publishing_enabled: boolean;
  caption_question_first: boolean;
  publisher_channel_id: string;
  publisher_browser_channel: string;
  blocked_sources: { id: number; type: string; value: string }[];
};

type FormStatus = {
  kind: "idle" | "saving" | "success" | "error";
  message: string;
};

export function isSettings(value: unknown): value is Settings {
  if (!isRecord(value)) return false;
  return (
    typeof value.channel_name === "string" &&
    typeof value.channel_handle === "string" &&
    typeof value.timezone === "string" &&
    isValidTimeZone(value.timezone) &&
    typeof value.default_post_time === "string" &&
    /^([01]\d|2[0-3]):[0-5]\d$/.test(value.default_post_time) &&
    typeof value.duplicate_window_days === "number" &&
    Number.isInteger(value.duplicate_window_days) &&
    value.duplicate_window_days >= 1 &&
    typeof value.agent_runtime === "string" &&
    typeof value.codex_model === "string" &&
    typeof value.codex_reasoning_effort === "string" &&
    typeof value.codex_chatgpt_auth_required === "boolean" &&
    typeof value.paid_api_fallback_enabled === "boolean" &&
    typeof value.openai_configured === "boolean" &&
    typeof value.browser_search_enabled === "boolean" &&
    typeof value.publishing_enabled === "boolean" &&
    typeof value.caption_question_first === "boolean" &&
    typeof value.publisher_channel_id === "string" &&
    typeof value.publisher_browser_channel === "string" &&
    Array.isArray(value.blocked_sources) &&
    value.blocked_sources.every(
      (item) =>
        isRecord(item) &&
        typeof item.id === "number" &&
        typeof item.type === "string" &&
        typeof item.value === "string",
    )
  );
}

export function SettingsForm({ initial }: { initial: Settings }) {
  const [settings, setSettings] = useState(initial);
  const [draft, setDraft] = useState({
    channelName: initial.channel_name,
    timezone: initial.timezone,
    defaultPostTime: initial.default_post_time,
    duplicateWindowDays: String(initial.duplicate_window_days),
  });
  const [status, setStatus] = useState<FormStatus>({
    kind: "idle",
    message: "",
  });
  const actionLock = useRef(false);
  const channelNameInvalid = !draft.channelName.trim();
  const timezoneInvalid = !isValidTimeZone(draft.timezone.trim());
  const timeInvalid = !/^([01]\d|2[0-3]):[0-5]\d$/.test(
    draft.defaultPostTime,
  );
  const duplicateWindow = Number(draft.duplicateWindowDays);
  const duplicateWindowInvalid =
    !Number.isInteger(duplicateWindow) || duplicateWindow < 1;

  function updateDraft(
    key: keyof typeof draft,
    value: string,
  ) {
    setDraft((current) => ({ ...current, [key]: value }));
    if (status.kind !== "saving") {
      setStatus({ kind: "idle", message: "" });
    }
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (actionLock.current) return;

    const channelName = draft.channelName.trim();
    const duplicateWindowDays = Number(draft.duplicateWindowDays);
    if (channelNameInvalid) {
      setStatus({ kind: "error", message: "Channel name is required." });
      return;
    }
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
    const fields = {
      name: channelName,
      timezone: draft.timezone.trim(),
      default_post_time: draft.defaultPostTime,
      duplicate_window_days: duplicateWindowDays,
    };
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
        channelName: payload.channel_name,
        timezone: payload.timezone,
        defaultPostTime: payload.default_post_time,
        duplicateWindowDays: String(payload.duplicate_window_days),
      });
      setStatus({ kind: "success", message: "Settings saved." });
    } catch (error) {
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
      <div className={settings.publishing_enabled ? "publishing-banner armed" : "publishing-banner"}>
        <strong>
          {settings.publishing_enabled
            ? "The guarded YouTube publisher is armed."
            : "Live YouTube publishing is disabled."}
        </strong>
        <span>
          {settings.publishing_enabled
            ? `Qlob channel ${settings.publisher_channel_id} · visible browser · accepted posts enter Lineup automatically.`
            : "Only local decisions and internal Lineup scheduling are available."}
        </span>
      </div>
      <form
        className="panel settings-form"
        onSubmit={save}
        aria-busy={status.kind === "saving"}
        noValidate
      >
        <label>
          <span>Channel name</span>
          <input
            className="field"
            name="name"
            value={draft.channelName}
            onChange={(event) => updateDraft("channelName", event.target.value)}
            required
            maxLength={120}
            aria-invalid={
              status.kind === "error" && channelNameInvalid ? "true" : undefined
            }
            disabled={status.kind === "saving"}
          />
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
              disabled={status.kind === "saving"}
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
              disabled={status.kind === "saving"}
            />
          </label>
        </div>
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
            disabled={status.kind === "saving"}
          />
        </label>
        <button
          className="button"
          type="submit"
          disabled={status.kind === "saving"}
        >
          {status.kind === "saving" ? "Saving settings…" : "Save settings"}
        </button>
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
            not consume RunWay’s daily slot.
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
