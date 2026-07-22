export type StatusTone =
  | "neutral"
  | "info"
  | "success"
  | "warning"
  | "danger";

export function StatusGlyph({
  tone = "neutral",
  className = "",
}: {
  tone?: StatusTone;
  className?: string;
}) {
  return (
    <span
      className={`status-glyph status-glyph-${tone} ${className}`.trim()}
      aria-hidden="true"
    >
      <svg viewBox="0 0 20 20" focusable="false">
        {tone === "success" ? (
          <path d="m5.4 10.2 2.8 2.8 6.4-6.5" />
        ) : tone === "warning" || tone === "danger" ? (
          <>
            <path d="M10 5.1v5.6" />
            <path d="M10 14.1h.01" />
          </>
        ) : tone === "info" ? (
          <>
            <path d="M10 8.7v5" />
            <path d="M10 5.7h.01" />
          </>
        ) : (
          <path d="M6 10h8" />
        )}
      </svg>
    </span>
  );
}
