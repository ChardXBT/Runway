import type { Metadata } from "next";

import { ConnectorSetup } from "@/components/connector-setup";
import { apiGetRequired } from "@/lib/api";
import { isRecord } from "@/lib/guards";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Connector",
  description:
    "Invite Runway to your YouTube channel and verify observed Community posting capability.",
};

type ConnectorSettings = {
  connector_account_email: string;
  publisher_channel_id: string;
  publisher_browser_channel: string;
  publishing_mode: "assisted" | "authorized_browser";
  authorized_browser_ready: boolean;
};

function isConnectorSettings(value: unknown): value is ConnectorSettings {
  return (
    isRecord(value) &&
    typeof value.connector_account_email === "string" &&
    typeof value.publisher_channel_id === "string" &&
    typeof value.publisher_browser_channel === "string" &&
    (value.publishing_mode === "assisted" ||
      value.publishing_mode === "authorized_browser") &&
    typeof value.authorized_browser_ready === "boolean"
  );
}

export default async function ConnectorPage() {
  const settings = await apiGetRequired<ConnectorSettings>(
    "/api/settings/full",
    isConnectorSettings,
  );

  return (
    <ConnectorSetup
      connectorEmail={settings.connector_account_email}
      configuredChannelId={settings.publisher_channel_id}
      browserName={
        settings.publisher_browser_channel === "chrome"
          ? "Google Chrome"
          : settings.publisher_browser_channel
      }
      publishingMode={settings.publishing_mode}
      publishingEnabled={settings.authorized_browser_ready}
    />
  );
}
