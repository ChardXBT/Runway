"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { RunwayLogo } from "@/components/runway-logo";
import { StatusGlyph } from "@/components/status-glyph";

type GlyphName =
  | "generator"
  | "lineup"
  | "connector"
  | "archive"
  | "profile"
  | "activity"
  | "settings";

const primaryLinks = [
  ["Generator", "/review", "generator"],
  ["Lineup", "/lineup", "lineup"],
  ["Connector", "/connector", "connector"],
] as const;

const secondaryLinks = [
  ["Archive", "/catalogue", "Past Qlob posts and source records", "archive"],
  ["Profile", "/profile", "What Runway has learned", "profile"],
  ["Activity", "/activity", "Decision and publishing history", "activity"],
  ["Settings", "/settings", "Channel, schedule, and safeguards", "settings"],
] as const;

function NavGlyph({ name }: { name: GlyphName }) {
  const paths: Record<GlyphName, ReactNode> = {
    generator: <path d="M4 15.5 7.8 5l2.1 6 2.2-3.3 3.9 7.8H4Z" />,
    lineup: (
      <>
        <path d="M4 5.5h12v10H4z" />
        <path d="M4 9h12M8 3.8v3.4m4-3.4v3.4" />
      </>
    ),
    connector: (
      <>
        <path d="M7.2 12.8 5 15a2.8 2.8 0 0 1-4-4l2.2-2.2" />
        <path d="m12.8 7.2 2.2-2.2a2.8 2.8 0 0 1 4 4l-2.2 2.2M6.8 13.2l6.4-6.4" />
      </>
    ),
    archive: (
      <>
        <path d="M3.5 6.5h13v10h-13z" />
        <path d="M2.5 3.5h15v3h-15zM8 10h4" />
      </>
    ),
    profile: (
      <>
        <path d="M10 10a3.2 3.2 0 1 0 0-6.4A3.2 3.2 0 0 0 10 10Z" />
        <path d="M4 17c.7-3 2.7-4.5 6-4.5s5.3 1.5 6 4.5" />
      </>
    ),
    activity: <path d="M3 10h3l1.8-4.5 3.3 9 2-4.5H17" />,
    settings: (
      <>
        <path d="M10 12.7a2.7 2.7 0 1 0 0-5.4 2.7 2.7 0 0 0 0 5.4Z" />
        <path d="m10 2 .8 1.9 2 .8 1.9-.8 1.4 1.4-.8 2 .8 1.9-.8 1.9.8 2-1.4 1.4-1.9-.8-2 .8-.8 1.9H8l-.8-1.9-2-.8-1.9.8-1.4-1.4.8-2-.8-1.9.8-1.9-.8-2 1.4-1.4 1.9.8 2-.8L8 2h2Z" />
      </>
    ),
  };
  return (
    <svg
      className="nav-glyph"
      viewBox="0 0 20 20"
      aria-hidden="true"
      focusable="false"
    >
      {paths[name]}
    </svg>
  );
}

export function Nav({
  publishingEnabled = false,
  publishingMode = "assisted",
}: {
  publishingEnabled?: boolean;
  publishingMode?: "assisted" | "authorized_browser";
}) {
  const pathname = usePathname();
  const menuRef = useRef<HTMLDetailsElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    function closeMenu(event: PointerEvent) {
      const menu = menuRef.current;
      if (menu?.open && event.target instanceof Node && !menu.contains(event.target)) {
        menu.open = false;
        setMenuOpen(false);
      }
    }

    function closeMenuWithKeyboard(event: KeyboardEvent) {
      if (event.key !== "Escape" || !menuRef.current?.open) return;
      menuRef.current.open = false;
      setMenuOpen(false);
      menuRef.current.querySelector("summary")?.focus();
    }

    document.addEventListener("pointerdown", closeMenu);
    document.addEventListener("keydown", closeMenuWithKeyboard);
    return () => {
      document.removeEventListener("pointerdown", closeMenu);
      document.removeEventListener("keydown", closeMenuWithKeyboard);
    };
  }, []);

  function dismissMenu() {
    if (!menuRef.current) return;
    menuRef.current.open = false;
    setMenuOpen(false);
  }

  const browserReady = publishingMode === "authorized_browser" && publishingEnabled;
  const stateTitle = browserReady ? "Browser publishing ready" : "Assisted publishing";
  const stateDetail = browserReady
    ? "Actions start from Lineup"
    : "No browser actions are queued";

  return (
    <header className="topbar">
      <div className="topbar-inner">
        <Link href="/review" className="brand" aria-label="Runway editorial desk">
          <RunwayLogo className="brand-mark" />
          <span className="brand-copy">
            <strong>Runway</strong>
            <small>Qlob editorial desk</small>
          </span>
        </Link>

        <span className="nav-section-label">Create and schedule</span>
        <nav className="primary-navigation" aria-label="Primary navigation">
          {primaryLinks.map(([label, href, icon]) => {
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
                <NavGlyph name={icon} />
                <span>{label}</span>
              </Link>
            );
          })}
        </nav>

        <details
          ref={menuRef}
          className="nav-menu"
          onToggle={(event) => setMenuOpen(event.currentTarget.open)}
        >
          <summary aria-label={menuOpen ? "Close Runway menu" : "Open Runway menu"}>
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span aria-hidden="true" />
          </summary>
          <div className="nav-menu-popover">
            <span className="nav-section-label">Workspace</span>
            <nav aria-label="Runway menu">
              {secondaryLinks.map(([label, href, description, icon]) => {
                const active = pathname.startsWith(href);
                return (
                  <Link
                    href={href}
                    key={href}
                    onClick={dismissMenu}
                    className={active ? "active" : ""}
                    aria-current={active ? "page" : undefined}
                  >
                    <NavGlyph name={icon} />
                    <span>
                      <strong>{label}</strong>
                      <small>{description}</small>
                    </span>
                  </Link>
                );
              })}
            </nav>
            <div className="nav-menu-status">
              <StatusGlyph tone={browserReady ? "success" : "info"} />
              <span>
                <strong>{stateTitle}</strong>
                <small>{stateDetail}</small>
              </span>
            </div>
          </div>
        </details>
      </div>
    </header>
  );
}
