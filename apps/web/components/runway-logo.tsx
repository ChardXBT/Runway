export function RunwayLogo({ className = "" }: { className?: string }) {
  return (
    <span className={className} aria-hidden="true">
      {/* Generated Runway ribbon mark. Kept as a project asset for consistent rendering. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src="/runway-logo-512.png" alt="" />
    </span>
  );
}
