export function RunwayLogo({ className = "" }: { className?: string }) {
  return (
    <span className={className} aria-hidden="true">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src="/runway-logo-64.png"
        srcSet="/runway-logo-64.png 1x, /runway-logo-128.png 2x"
        alt=""
        width="64"
        height="64"
      />
    </span>
  );
}
