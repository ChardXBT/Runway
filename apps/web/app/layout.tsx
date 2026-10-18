import type { Metadata } from "next";
import { Instrument_Serif, Work_Sans } from "next/font/google";
import type { ReactNode } from "react";

import { DemoBanner } from "@/components/demo-banner";
import { Nav } from "@/components/nav";
import { apiGet } from "@/lib/api";
import { isRecord } from "@/lib/guards";
import "./runway-next.css";

const workSans = Work_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-ui",
});

const instrumentSerif = Instrument_Serif({
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
  variable: "--font-serif",
});

export const metadata: Metadata = {
  title: {
    default: "Runway · Creator desk",
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
    platform_provider: "live" | "mock";
  }>(
    "/api/settings",
    {
      publishing_mode: "assisted",
      authorized_browser_ready: false,
      platform_provider: "live",
    },
    (value): value is {
      publishing_mode: "assisted" | "authorized_browser";
      authorized_browser_ready: boolean;
      platform_provider: "live" | "mock";
    } =>
      isRecord(value) &&
      (value.publishing_mode === "assisted" ||
        value.publishing_mode === "authorized_browser") &&
      typeof value.authorized_browser_ready === "boolean" &&
      (value.platform_provider === "live" ||
        value.platform_provider === "mock"),
  );
  return (
    <html lang="en">
      <body className={`${workSans.variable} ${instrumentSerif.variable}`}>
        <a className="skip-link" href="#main-content">
          Skip to content
        </a>
        <div className="app-shell">
          <Nav
            publishingEnabled={settings.authorized_browser_ready}
            publishingMode={settings.publishing_mode}
          />
          <main id="main-content" className="main" tabIndex={-1}>
            {settings.platform_provider === "mock" ? <DemoBanner /> : null}
            <div className="page-frame">{children}</div>
          </main>
        </div>
      </body>
    </html>
  );
}
