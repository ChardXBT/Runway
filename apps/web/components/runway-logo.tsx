export function RunwayLogo({ className = "" }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 48 48"
      role="img"
      aria-label="RunWay"
    >
      <path
        className="logo-arch"
        d="M8.5 23.5V19C8.5 10.44 15.44 3.5 24 3.5S39.5 10.44 39.5 19v4.5"
      />
      <path className="logo-runway" d="M15 44.5 20.5 19h7L33 44.5Z" />
      <path className="logo-centerline" d="M24 22.5v4m0 4.5v5m0 4.5v3" />
      <circle className="logo-light logo-light-left" cx="12" cy="37.5" r="1.7" />
      <circle className="logo-light logo-light-right" cx="36" cy="37.5" r="1.7" />
    </svg>
  );
}
