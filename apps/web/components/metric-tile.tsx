import {
  formatAbsoluteTime,
  formatCompactNumber,
  formatDelta,
} from "@/lib/format";

export function MetricTile({
  label,
  value,
  delta,
  deltaTone,
  source,
  asOf,
}: {
  label: string;
  value: number | string | null | undefined;
  delta?: number | null;
  deltaTone?: "positive" | "negative" | "neutral" | "auto";
  source?: string;
  asOf?: string | null;
}) {
  const rendered =
    typeof value === "number" ? formatCompactNumber(value) : (value ?? "—");
  const tone =
    deltaTone === "auto" || deltaTone === undefined
      ? delta === null || delta === undefined || delta === 0
        ? "neutral"
        : delta > 0
          ? "positive"
          : "negative"
      : deltaTone;
  const deltaText = formatDelta(delta);
  return (
    <div className="metric-tile">
      <span className="metric-tile-label">{label}</span>
      <span className="metric-tile-value">{rendered}</span>
      {delta !== null && delta !== undefined && deltaText ? (
        <span className={`metric-tile-delta tone-${tone}`} data-tone={tone}>
          {deltaText}
        </span>
      ) : null}
      {source || asOf ? (
        <span className="metric-tile-source">
          {source}
          {source && asOf ? " · " : ""}
          {asOf ? (
            <time title={asOf}>{formatAbsoluteTime(asOf)}</time>
          ) : null}
        </span>
      ) : null}
    </div>
  );
}
