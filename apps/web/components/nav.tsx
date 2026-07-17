"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const links = [
  ["Review", "/review"],
  ["Schedule", "/queue"],
  ["Archive", "/catalogue"],
  ["Settings", "/settings"],
] as const;

export function Nav({ publishingEnabled = false }: { publishingEnabled?: boolean }) {
  const pathname = usePathname();

  return (
    <header className="topbar">
      <div className="topbar-inner">
        <Link href="/review" className="brand" aria-label="LeeWay editorial desk">
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
            const active = pathname.startsWith(href);
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
        <div
          className={publishingEnabled ? "publishing-lock armed" : "publishing-lock"}
          title={
            publishingEnabled
              ? "Approved posts enter the visible-browser Qlob scheduling queue"
              : "LeeWay cannot publish to YouTube"
          }
        >
          <span className="lock-signal" aria-hidden="true" />
          <span>
            <strong>{publishingEnabled ? "Auto-schedule on" : "Publishing disabled"}</strong>
            <small>
              {publishingEnabled ? "One bot post daily" : "Local planning only"}
            </small>
          </span>
        </div>
      </div>
    </header>
  );
}
