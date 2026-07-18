"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { RunwayLogo } from "@/components/runway-logo";

const primaryLinks = [
  ["Generator", "/review"],
  ["Lineup", "/lineup"],
] as const;

const secondaryLinks = [
  ["Archive", "/catalogue", "Past Qlob posts and source records"],
  ["Profile", "/profile", "What RunWay has learned"],
  ["Activity", "/activity", "Decision and publishing history"],
  ["Settings", "/settings", "Model, channel, and safeguards"],
  ["Platform connection", "/settings#platform-connection", "YouTube access and queue recovery"],
] as const;

export function Nav({ publishingEnabled = false }: { publishingEnabled?: boolean }) {
  const pathname = usePathname();
  const menuRef = useRef<HTMLDetailsElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    function closeMenu(event: PointerEvent) {
      const menu = menuRef.current;
      if (
        menu?.open &&
        event.target instanceof Node &&
        !menu.contains(event.target)
      ) {
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
        <details
          ref={menuRef}
          className="nav-menu"
          onToggle={(event) => setMenuOpen(event.currentTarget.open)}
        >
          <summary
            aria-label={menuOpen ? "Close RunWay menu" : "Open RunWay menu"}
          >
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
                  onClick={dismissMenu}
                  className={
                    pathname.startsWith(href.split("#")[0]) ? "active" : ""
                  }
                  aria-current={
                    pathname.startsWith(href.split("#")[0]) ? "page" : undefined
                  }
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
