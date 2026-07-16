import { SettingsForm } from "@/components/settings-form";
import { apiGet } from "@/lib/api";

type Settings = Parameters<typeof SettingsForm>[0]["initial"];

const fallback: Settings = { channel_name: "Qlob", channel_handle: "Qlob", timezone: "America/Toronto", default_post_time: "10:00", planning_horizon_days: 10, duplicate_window_days: 180, agent_runtime: "mock", openai_configured: false, browser_search_enabled: false, publishing_enabled: false, blocked_sources: [] };

export default async function SettingsPage() { const settings = await apiGet<Settings>("/api/settings/full", fallback); return <><header className="header-row"><div><p className="eyebrow">Local configuration</p><h1>Settings with guardrails.</h1><p className="lede">Secret values never appear here. Browser and network integrations remain disabled until explicitly configured.</p></div></header><SettingsForm initial={settings} /></>; }
