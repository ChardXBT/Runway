import Link from "next/link";
import type { ReactNode } from "react";

export type StatePanelVariant =
  | "loading"
  | "empty"
  | "disconnected"
  | "not_configured"
  | "not_connected"
  | "unsupported"
  | "error"
  | "stale";

const VARIANT_TITLES: Record<StatePanelVariant, string> = {
  loading: "Loading…",
  empty: "Nothing here yet",
  disconnected: "Account disconnected",
  not_configured: "Platform not configured",
  not_connected: "No account connected",
  unsupported: "Not supported here",
  error: "Something went wrong",
  stale: "Data may be stale",
};

export function StatePanel({
  variant,
  title,
  body,
  action,
  actionLabel,
}: {
  variant: StatePanelVariant;
  title?: string;
  body?: ReactNode;
  action?: { href: string } | { onClick: () => void };
  actionLabel?: string;
}) {
  if (variant === "loading") {
    return (
      <section className="state-panel state-panel-loading" aria-busy="true">
        <span className="state-panel-skeleton" style={{ width: "38%" }} />
        <span className="state-panel-skeleton" style={{ width: "72%" }} />
        <span className="state-panel-skeleton" style={{ width: "56%" }} />
      </section>
    );
  }
  return (
    <section className={`state-panel state-panel-${variant}`} data-variant={variant}>
      <strong className="state-panel-title">{title ?? VARIANT_TITLES[variant]}</strong>
      {body ? <div className="state-panel-body">{body}</div> : null}
      {action && actionLabel ? (
        "href" in action ? (
          <Link className="button" href={action.href}>
            {actionLabel}
          </Link>
        ) : (
          <button className="button" type="button" onClick={action.onClick}>
            {actionLabel}
          </button>
        )
      ) : null}
    </section>
  );
}
