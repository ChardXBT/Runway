"use client";

import { FormEvent, useState } from "react";

import { API_URL } from "@/lib/api";

type Settings = { channel_name: string; channel_handle: string; timezone: string; default_post_time: string; planning_horizon_days: number; duplicate_window_days: number; agent_runtime: string; openai_configured: boolean; browser_search_enabled: boolean; publishing_enabled: boolean; blocked_sources: { id: number; type: string; value: string }[] };

export function SettingsForm({ initial }: { initial: Settings }) {
  const [settings, setSettings] = useState(initial);
  const [message, setMessage] = useState("");
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget); setMessage("Saving…");
    const fields = { name: String(data.get("name")), timezone: String(data.get("timezone")), default_post_time: String(data.get("default_post_time")), planning_horizon_days: Number(data.get("planning_horizon_days")), duplicate_window_days: Number(data.get("duplicate_window_days")) };
    const response = await fetch(`${API_URL}/api/settings/full`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fields }) });
    const payload = await response.json(); if (response.ok) setSettings(payload as Settings); setMessage(response.ok ? "Saved." : payload.detail);
  }
  return <><div className="publishing-banner"><strong>Live YouTube publishing is disabled.</strong><span>Only the local internal schedule state is available.</span></div><form className="panel settings-form" onSubmit={save}><label><span>Channel name</span><input className="field" name="name" defaultValue={settings.channel_name} /></label><label><span>Handle</span><input className="field" value={settings.channel_handle} disabled /></label><div className="two-fields"><label><span>Timezone</span><input className="field" name="timezone" defaultValue={settings.timezone} /></label><label><span>Default post time</span><input className="field" name="default_post_time" type="time" defaultValue={settings.default_post_time} /></label></div><div className="two-fields"><label><span>Planning horizon</span><input className="field" name="planning_horizon_days" type="number" min="1" max="30" defaultValue={settings.planning_horizon_days} /></label><label><span>Duplicate window</span><input className="field" name="duplicate_window_days" type="number" min="1" defaultValue={settings.duplicate_window_days} /></label></div><button className="button" type="submit">Save settings</button><small role="status">{message}</small></form><section className="profile-columns"><article className="panel"><p className="eyebrow">Model provider</p><h2>{settings.agent_runtime}</h2><p>{settings.openai_configured ? "Configured; secret hidden." : "Offline mock runtime active."}</p></article><article className="panel"><p className="eyebrow">Browser discovery</p><h2>{settings.browser_search_enabled ? "Enabled" : "Disabled"}</h2><p>Always headed, explicit, and challenge-aware.</p></article></section><section className="panel"><p className="eyebrow">Blocked sources</p>{settings.blocked_sources.length ? <ul className="clean-list">{settings.blocked_sources.map((item) => <li key={item.id}>{item.type}: {item.value}</li>)}</ul> : <p>No sources are blocked.</p>}</section></>;
}
