import type { Metadata } from "next";
import { IBM_Plex_Sans } from "next/font/google";
import type { ReactNode } from "react";

import { Nav } from "@/components/nav";
import { apiGet } from "@/lib/api";
import { isRecord } from "@/lib/guards";
import "./runway-next.css";

const plexSans = IBM_Plex_Sans({
  variable: "--font-runway",
  subsets: ["latin"],
  weight: "variable",
  display: "swap",
});

export const metadata: Metadata = {
  title: {
    default: "Runway · Qlob editorial desk",
    template: "%s · Runway",
  },
  description:
    "Choose, refine, and schedule Qlob Community posts from one local editorial desk.",
};

export default async function RootLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  const settings = await apiGet<{
    publishing_mode: "assisted" | "authorized_browser";
    authorized_browser_ready: boolean;
  }>(
    "/api/settings",
    { publishing_mode: "assisted", authorized_browser_ready: false },
    (value): value is {
      publishing_mode: "assisted" | "authorized_browser";
      authorized_browser_ready: boolean;
    } =>
      isRecord(value) &&
      (value.publishing_mode === "assisted" ||
        value.publishing_mode === "authorized_browser") &&
      typeof value.authorized_browser_ready === "boolean",
  );
  return (
    <html lang="en">
      <body className={plexSans.variable}>
        <a className="skip-link" href="#main-content">
          Skip to content
        </a>
        <div className="app-shell">
          <Nav
            publishingEnabled={settings.authorized_browser_ready}
            publishingMode={settings.publishing_mode}
          />
          <main id="main-content" className="main" tabIndex={-1}>
            <div className="page-frame">{children}</div>
          </main>
        </div>
      </body>
    </html>
  );
}
