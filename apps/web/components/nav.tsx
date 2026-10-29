"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { RunwayLogo } from "./runway-logo";
import { StatusGlyph } from "./status-glyph";

type NavLinkDef = { label: string; href: string; glyph: GlyphName; hint?: string };
type NavGroup = { label: string; links: NavLinkDef[] };

const NAV_GROUPS: NavGroup[] = [
  {
    label: "Overview",
    links: [{ label: "Dashboard", href: "/dashboard", glyph: "dashboard" }],
  },
  {
    label: "Create and schedule",
    links: [
      { label: "Review", href: "/review", glyph: "generator" },
      { label: "Calendar", href: "/lineup", glyph: "lineup" },
      { label: "Drafts", href: "/drafts", glyph: "drafts" },
    ],
  },
  {
    label: "Audience",
    links: [
      { label: "Analytics", href: "/analytics", glyph: "analytics" },
      { label: "Inbox", href: "/inbox", glyph: "inbox" },
      { label: "Content", href: "/content", glyph: "content" },
    ],
  },
  {
    label: "Workspace",
    links: [
      { label: "Accounts", href: "/accounts", glyph: "accounts" },
      { label: "Connector", href: "/connector", glyph: "connector" },
      {
        label: "Archive",
        href: "/archive",
        glyph: "catalogue",
        hint: "Captured Community history",
      },
      { label: "Editorial voice", href: "/profile", glyph: "profile" },
      { label: "Activity", href: "/activity", glyph: "activity" },
      { label: "Settings", href: "/settings", glyph: "settings" },
    ],
  },
];

const HREF_ALIASES: Record<string, string> = {
  "/queue": "/lineup",
  "/catalogue": "/archive",
};

type GlyphName =
  | "accounts"
  | "activity"
  | "analytics"
  | "catalogue"
  | "connector"
  | "content"
  | "dashboard"
  | "drafts"
  | "generator"
  | "inbox"
  | "lineup"
  | "profile"
  | "settings";

function NavGlyph({ name }: { name: GlyphName }) {
  switch (name) {
    case "dashboard":
      return (
        <svg className="nav-glyph" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="3" y="3" width="6" height="6" rx="1.5" />
          <rect x="11" y="3" width="6" height="6" rx="1.5" />
          <rect x="3" y="11" width="6" height="6" rx="1.5" />
          <rect x="11" y="11" width="6" height="6" rx="1.5" />
        </svg>
      );
    case "generator":
      return (
        <svg className="nav-glyph" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="2.75" y="4" width="14.5" height="12" rx="2.5" />
          <path d="M6 8.5h8" />
          <path d="M6 11.5h5" />
        </svg>
      );
    case "lineup":
      return (
        <svg className="nav-glyph" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="3" y="4" width="14" height="13" rx="2" />
          <path d="M3 8h14" />
          <path d="M7 2.75v2.5" />
          <path d="M13 2.75v2.5" />
          <circle cx="10" cy="12.5" r="1.6" />
        </svg>
      );
    case "drafts":
      return (
        <svg className="nav-glyph" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M4 15.5V6a2 2 0 0 1 2-2h6.5L16 7.5v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z" />
          <path d="M12.5 4v3.5H16" />
          <path d="M7 12.5h6" />
        </svg>
      );
    case "analytics":
      return (
        <svg className="nav-glyph" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M3.5 16.5h13" />
          <path d="M6 16.5v-5" />
          <path d="M10 16.5v-8" />
          <path d="M14 16.5v-3" />
        </svg>
      );
    case "inbox":
      return (
        <svg className="nav-glyph" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M3.5 12v3a1.5 1.5 0 0 0 1.5 1.5h10a1.5 1.5 0 0 0 1.5-1.5v-3" />
          <path d="M3.5 12l2-6.5A1.5 1.5 0 0 1 6.93 4.5h6.14a1.5 1.5 0 0 1 1.43 1L16.5 12" />
          <path d="M3.5 12h3.7l1 1.5h3.6l1-1.5h3.7" />
        </svg>
      );
    case "content":
      return (
        <svg className="nav-glyph" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="3" y="4" width="14" height="12" rx="2" />
          <path d="M3 8h14" />
          <path d="M7 4v4" />
          <path d="M13 4v4" />
          <path d="M9 10.75l3 1.75-3 1.75z" />
        </svg>
      );
    case "accounts":
      return (
        <svg className="nav-glyph" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="10" cy="7" r="3" />
          <path d="M4.5 16.5c.8-2.6 2.9-4 5.5-4s4.7 1.4 5.5 4" />
        </svg>
      );
    case "connector":
      return (
        <svg className="nav-glyph" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M7.5 7.5H6a3.5 3.5 0 0 0 0 7h1.5" />
          <path d="M12.5 7.5H14a3.5 3.5 0 0 1 0 7h-1.5" />
          <path d="M7.5 11h5" />
        </svg>
      );
    case "catalogue":
      return (
        <svg className="nav-glyph" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="4" y="4" width="12" height="12" rx="2" />
          <path d="M4 9h12" />
          <path d="M9 9v7" />
        </svg>
      );
    case "profile":
      return (
        <svg className="nav-glyph" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="10" cy="10" r="6.5" />
          <path d="M10 6.5V10l2.4 1.6" />
        </svg>
      );
    case "activity":
      return (
        <svg className="nav-glyph" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M3 10h3l2-5 4 10 2-5h3" />
        </svg>
      );
    case "settings":
      return (
        <svg className="nav-glyph" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="10" cy="10" r="2.6" />
          <path d="M10 3.2v2M10 14.8v2M3.2 10h2M14.8 10h2M5.2 5.2l1.4 1.4M13.4 13.4l1.4 1.4M14.8 5.2l-1.4 1.4M6.6 13.4l-1.4 1.4" />
        </svg>
      );
  }
}

export function Nav({
  publishingEnabled = false,
  publishingMode = "assisted",
}: {
  publishingEnabled?: boolean;
  publishingMode?: "assisted" | "authorized_browser";
}) {
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);
  const [prevPathname, setPrevPathname] = useState(pathname);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  if (prevPathname !== pathname) {
    setPrevPathname(pathname);
    setMenuOpen(false);
  }

  useEffect(() => {
    if (!menuOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
        triggerRef.current?.focus();
      }
    };
    const onPointerDown = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onPointerDown);
    };
  }, [menuOpen]);

  const isActive = (href: string) => {
    const aliasKey = Object.keys(HREF_ALIASES).find(
      (key) => pathname === key || pathname.startsWith(`${key}/`),
    );
    const current = aliasKey ? HREF_ALIASES[aliasKey] : pathname;
    return current === href || current.startsWith(`${href}/`);
  };

  const publishingReady =
    publishingEnabled && publishingMode === "authorized_browser";

  return (
    <div className="topbar-inner">
      <Link className="brand" href="/" aria-label="Runway home">
        <RunwayLogo className="brand-mark" />
        <span className="brand-copy">
          <strong>Runway</strong>
          <small>Creator desk</small>
        </span>
      </Link>
      <div className={`nav-menu${menuOpen ? " open" : ""}`} ref={menuRef}>
        <button
          className="nav-menu-trigger"
          type="button"
          aria-expanded={menuOpen}
          aria-controls="runway-nav-drawer"
          aria-label={menuOpen ? "Close Runway menu" : "Open Runway menu"}
          onClick={() => setMenuOpen((open) => !open)}
          ref={triggerRef}
        >
          <span aria-hidden="true" />
          <span aria-hidden="true" />
          <span aria-hidden="true" />
        </button>
        <div className="nav-menu-popover" id="runway-nav-drawer">
          {NAV_GROUPS.map((group) => (
            <div className="nav-group" key={group.label}>
              <p className="nav-section-label">{group.label}</p>
              <nav aria-label={group.label}>
                {group.links.map((link) => (
                  <Link
                    key={link.href}
                    href={link.href}
                    className="nav-link"
                    aria-current={isActive(link.href) ? "page" : undefined}
                  >
                    <NavGlyph name={link.glyph} />
                    <span>
                      <strong>{link.label}</strong>
                      {link.hint ? <small>{link.hint}</small> : null}
                    </span>
                  </Link>
                ))}
              </nav>
            </div>
          ))}
          <div className="nav-menu-status">
            <StatusGlyph tone={publishingReady ? "success" : "info"} />
            <div>
              <strong>
                {publishingReady
                  ? "Browser publishing ready"
                  : "Assisted publishing"}
              </strong>
              <small>
                {publishingReady
                  ? "Actions start from Calendar"
                  : "No browser actions are queued"}
              </small>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
