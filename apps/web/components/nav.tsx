"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const links = [
  ["Desk", "/"],
  ["Review", "/review"],
  ["Queue", "/queue"],
  ["Archive", "/catalogue"],
  ["Profile", "/profile"],
  ["Settings", "/settings"],
  ["Activity", "/activity"],
] as const;

export function Nav() {
  const pathname = usePathname();

  return (
    <header className="topbar">
      <div className="topbar-inner">
        <Link href="/" className="brand" aria-label="LeeWay editorial desk">
          <span className="brand-mark" aria-hidden="true">
            <i>L</i>
            <i>W</i>
          </span>
          <span className="brand-copy">
            <strong>LeeWay</strong>
            <small>Qlob editorial desk</small>
          </span>
        </Link>
        <nav aria-label="Primary navigation">
          {links.map(([label, href]) => {
            const active = href === "/" ? pathname === href : pathname.startsWith(href);
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
        <div className="publishing-lock" title="LeeWay cannot publish to YouTube">
          <span className="lock-signal" aria-hidden="true" />
          <span>
            <strong>Publishing disabled</strong>
            <small>Local planning only</small>
          </span>
        </div>
      </div>
    </header>
  );
}
