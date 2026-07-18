"use client";

import { FormEvent, useState } from "react";

import { API_URL } from "@/lib/api";

type Settings = {
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

export function SettingsForm({ initial }: { initial: Settings }) {
  const [settings, setSettings] = useState(initial);
  const [message, setMessage] = useState("");

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setMessage("Saving…");
    const fields = {
      name: String(data.get("name")),
      timezone: String(data.get("timezone")),
      default_post_time: String(data.get("default_post_time")),
      duplicate_window_days: Number(data.get("duplicate_window_days")),
    };
    const response = await fetch(`${API_URL}/api/settings/full`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fields }),
    });
    const payload = await response.json();
    if (response.ok) setSettings(payload as Settings);
    setMessage(response.ok ? "Saved." : payload.detail);
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
      <form className="panel settings-form" onSubmit={save}>
        <label>
          <span>Channel name</span>
          <input className="field" name="name" defaultValue={settings.channel_name} />
        </label>
        <label>
          <span>Handle</span>
          <input className="field" value={settings.channel_handle} disabled />
        </label>
        <div className="two-fields">
          <label>
            <span>Timezone</span>
            <input className="field" name="timezone" defaultValue={settings.timezone} />
          </label>
          <label>
            <span>Default post time</span>
            <input
              className="field"
              name="default_post_time"
              type="time"
              defaultValue={settings.default_post_time}
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
            defaultValue={settings.duplicate_window_days}
          />
        </label>
        <button className="button" type="submit">
          Save settings
        </button>
        <small role="status">{message}</small>
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
