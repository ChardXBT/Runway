"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { RunwayLogo } from "@/components/runway-logo";

const primaryLinks = [
  ["Runway", "/review"],
  ["Lineup", "/lineup"],
] as const;

const secondaryLinks = [
  ["Archive", "/catalogue", "Past Qlob posts and source records"],
  ["Profile", "/profile", "What RunWay has learned"],
  ["Activity", "/activity", "Decision and publishing history"],
  ["Settings", "/settings", "Model, channel, and safeguards"],
] as const;

export function Nav({ publishingEnabled = false }: { publishingEnabled?: boolean }) {
  const pathname = usePathname();

  return (
    <header className="topbar">
      <div className="topbar-inner">
        <Link href="/review" className="brand" aria-label="RunWay editorial desk">
          <RunwayLogo className="brand-mark" />
          <span className="brand-copy">
            <strong>RunWay</strong>
            <small>Qlob private desk</small>
          </span>
        </Link>
        <nav aria-label="Primary navigation">
          {primaryLinks.map(([label, href]) => {
            const active =
              pathname.startsWith(href) ||
              (href === "/lineup" && pathname.startsWith("/queue"));
            return (
              <Link
                key={href}
                href={href}
                className={active ? "nav-link active" : "nav-link"}
                aria-current={active ? "page" : undefined}
              >
                {label}
              </Link>
            );
          })}
        </nav>
        <details className="nav-menu">
          <summary aria-label="Open RunWay menu">
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span aria-hidden="true" />
          </summary>
          <div className="nav-menu-popover">
            <div className="nav-menu-status">
              <span
                className={publishingEnabled ? "status-dot armed" : "status-dot"}
                aria-hidden="true"
              />
              <span>
                <strong>
                  {publishingEnabled ? "YouTube actions enabled" : "YouTube scheduling off"}
                </strong>
                <small>
                  {publishingEnabled
                    ? "Account sign-in is checked on use"
                    : "One RunWay post per day"}
                </small>
              </span>
            </div>
            <nav aria-label="RunWay menu">
              {secondaryLinks.map(([label, href, description]) => (
                <Link
                  href={href}
                  key={href}
                  className={pathname.startsWith(href) ? "active" : ""}
                  aria-current={pathname.startsWith(href) ? "page" : undefined}
                >
                  <strong>{label}</strong>
                  <small>{description}</small>
                </Link>
              ))}
            </nav>
          </div>
        </details>
      </div>
    </header>
  );
}
