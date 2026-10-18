import type { PlatformKind } from "@/lib/types";

export const PLATFORM_LABELS: Record<PlatformKind, string> = {
  youtube: "YouTube",
  instagram: "Instagram",
  tiktok: "TikTok",
};

export function platformLabel(platform: PlatformKind | string): string {
  return PLATFORM_LABELS[platform as PlatformKind] ?? platform;
}

export function PlatformGlyph({ platform }: { platform: PlatformKind }) {
  if (platform === "youtube") {
    return (
      <svg
        className="platform-badge-glyph"
        viewBox="0 0 20 20"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <rect x="2.5" y="4.5" width="15" height="11" rx="3" />
        <path d="M8.5 7.5l4 2.5-4 2.5z" />
      </svg>
    );
  }
  if (platform === "instagram") {
    return (
      <svg
        className="platform-badge-glyph"
        viewBox="0 0 20 20"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <rect x="3" y="3" width="14" height="14" rx="4" />
        <circle cx="10" cy="10" r="3.2" />
        <circle cx="14.2" cy="5.8" r="0.6" fill="currentColor" stroke="none" />
      </svg>
    );
  }
  return (
    <svg
      className="platform-badge-glyph"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="7.5" cy="14.5" r="2.5" />
      <path d="M10 14.5V4.5" />
      <path d="M10 4.5c.5 2.4 2.2 4.1 4.6 4.6" />
    </svg>
  );
}

export function PlatformBadge({
  platform,
  size = "md",
  label = true,
}: {
  platform: PlatformKind;
  size?: "sm" | "md";
  label?: boolean;
}) {
  return (
    <span
      className={`platform-badge platform-badge-${size}`}
      data-platform={platform}
      title={label ? undefined : PLATFORM_LABELS[platform]}
    >
      <PlatformGlyph platform={platform} />
      {label ? (
        <span className="platform-badge-label">{PLATFORM_LABELS[platform]}</span>
      ) : null}
    </span>
  );
}
