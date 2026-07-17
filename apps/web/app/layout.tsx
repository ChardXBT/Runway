import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Nav } from "@/components/nav";
import "./studio.css";

export const metadata: Metadata = {
  title: "LeeWay — Qlob editorial desk",
  description: "Local Qlob Community-post intelligence, review, and planning",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <div className="app-shell">
          <Nav />
          <main className="main">
            <div className="page-frame">{children}</div>
          </main>
        </div>
      </body>
    </html>
  );
}
