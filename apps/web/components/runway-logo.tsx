export function RunwayLogo({ className = "" }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 64 64"
      aria-hidden="true"
      focusable="false"
    >
      <path
        className="logo-arch"
        d="M7.5 30V24C7.5 11.57 18.47 2 32 2s24.5 9.57 24.5 22v6"
      />
      <path
        className="logo-arch-inner"
        d="M14.5 30v-5.25C14.5 15.45 22.34 8 32 8s17.5 7.45 17.5 16.75V30"
      />
      <path className="logo-runway" d="M18.5 62 27 26h10l8.5 36Z" />
      <path className="logo-centerline" d="M32 30v5m0 5v6m0 5v8" />
      <circle className="logo-light logo-light-left" cx="14" cy="53" r="2.1" />
      <circle className="logo-light logo-light-right" cx="50" cy="53" r="2.1" />
    </svg>
  );
}
