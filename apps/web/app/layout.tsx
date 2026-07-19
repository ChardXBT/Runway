import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Nav } from "@/components/nav";
import { apiGet } from "@/lib/api";
import { isRecord } from "@/lib/guards";
import "./runway.css";

export const metadata: Metadata = {
  title: {
    default: "Runway — Your fans can't wait",
    template: "%s · Runway",
  },
  description:
    "Runway helps channels choose, refine, connect, and line up Community posts.",
};

export default async function RootLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  const settings = await apiGet<{ publishing_enabled: boolean }>(
    "/api/settings",
    { publishing_enabled: false },
    (value): value is { publishing_enabled: boolean } =>
      isRecord(value) && typeof value.publishing_enabled === "boolean",
  );
  return (
    <html lang="en">
      <body>
        <div className="app-shell">
          <Nav publishingEnabled={settings.publishing_enabled} />
          <main className="main">
            <div className="page-frame">{children}</div>
          </main>
        </div>
      </body>
    </html>
  );
}
