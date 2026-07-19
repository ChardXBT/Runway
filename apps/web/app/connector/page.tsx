import type { Metadata } from "next";

import { ConnectorSetup } from "@/components/connector-setup";
import { apiGet } from "@/lib/api";
import { isRecord } from "@/lib/guards";

export const metadata: Metadata = {
  title: "Connector",
  description: "Invite Runway to your YouTube channel and verify Editor access.",
};

type ConnectorSettings = {
  connector_account_email: string;
  publisher_channel_id: string;
  publisher_browser_channel: string;
};

const fallback: ConnectorSettings = {
  connector_account_email: "tryrunwaytoday@gmail.com",
  publisher_channel_id: "",
  publisher_browser_channel: "chrome",
};

function isConnectorSettings(value: unknown): value is ConnectorSettings {
  return (
    isRecord(value) &&
    typeof value.connector_account_email === "string" &&
    typeof value.publisher_channel_id === "string" &&
    typeof value.publisher_browser_channel === "string"
  );
}

export default async function ConnectorPage() {
  const settings = await apiGet<ConnectorSettings>(
    "/api/settings",
    fallback,
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
    />
  );
}
