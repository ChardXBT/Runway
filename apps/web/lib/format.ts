export function parseApiDate(value: string | Date | null | undefined): Date | null {
  if (value === null || value === undefined) return null;
  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : value;
  }
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  // API timestamps may arrive naive (no offset); they are UTC by contract.
  const normalized = /[zZ]|[+-]\d{2}:?\d{2}$/.test(trimmed)
    ? trimmed
    : `${trimmed}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatCompactNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const absolute = Math.abs(value);
  const sign = value < 0 ? "-" : "";
  if (absolute >= 1_000_000_000) {
    return `${sign}${trimNumber(absolute / 1_000_000_000)}B`;
  }
  if (absolute >= 1_000_000) {
    return `${sign}${trimNumber(absolute / 1_000_000)}M`;
  }
  if (absolute >= 1_000) {
    return `${sign}${trimNumber(absolute / 1_000)}K`;
  }
  return `${value}`;
}

function trimNumber(value: number): string {
  const rounded = value >= 100 ? Math.round(value) : Math.round(value * 10) / 10;
  return String(rounded).replace(/\.0$/, "");
}

export function formatInteger(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(value);
}

export function formatPercent(
  value: number | null | undefined,
  { decimals = 1 }: { decimals?: number } = {},
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(decimals)}%`;
}

export function formatDelta(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${formatCompactNumber(Math.abs(value))}`;
}

export function formatRelativeTime(
  value: string | Date | null | undefined,
  now: Date = new Date(),
): string {
  if (value === null || value === undefined) return "never";
  const date = parseApiDate(value);
  if (date === null) return "never";
  const rawSeconds = Math.round((now.getTime() - date.getTime()) / 1000);
  if (rawSeconds >= 0 && rawSeconds < 10) return "just now";
  const future = rawSeconds < 0;
  const seconds = Math.abs(rawSeconds);
  const units: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ["year", 60 * 60 * 24 * 365.25],
    ["month", 60 * 60 * 24 * 30.44],
    ["week", 60 * 60 * 24 * 7],
    ["day", 60 * 60 * 24],
    ["hour", 60 * 60],
    ["minute", 60],
    ["second", 1],
  ];
  let chosen: [Intl.RelativeTimeFormatUnit, number] = ["second", 1];
  for (const candidate of units) {
    if (seconds >= candidate[1]) {
      chosen = candidate;
      break;
    }
  }
  const count = Math.max(1, Math.floor(seconds / chosen[1]));
  return new Intl.RelativeTimeFormat("en", { numeric: "always" }).format(
    future ? count : -count,
    chosen[0],
  );
}

export function formatAbsoluteTime(value: string | Date | null | undefined): string {
  if (value === null || value === undefined) return "";
  const date = parseApiDate(value);
  if (date === null) return "";
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

export function formatHandle(handle: string | null | undefined): string {
  if (!handle) return "";
  const stripped = handle.trim().replace(/^@+/, "");
  return stripped ? `@${stripped}` : "";
}

export function excerpt(
  value: string | null | undefined,
  maxLength: number,
): string | null {
  if (!value) return null;
  const compact = value.replace(/\s+/g, " ").trim();
  if (!compact) return null;
  return compact.length > maxLength
    ? `${compact.slice(0, maxLength - 1)}…`
    : compact;
}

export function formatDurationSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const total = Math.max(0, Math.round(value));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const mm = String(minutes).padStart(2, "0");
  const ss = String(seconds).padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${minutes}:${ss}`;
}
