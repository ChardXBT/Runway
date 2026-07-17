import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Nav } from "@/components/nav";
import { apiGet } from "@/lib/api";
import "./studio.css";

export const metadata: Metadata = {
  title: "LeeWay — Qlob editorial desk",
  description: "Local Qlob Community-post intelligence, review, and planning",
};

export default async function RootLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  const settings = await apiGet<{ publishing_enabled: boolean }>(
    "/api/settings",
    { publishing_enabled: false },
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
