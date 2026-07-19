export function RunwayLogo({
  className = "",
  dark = false,
}: {
  className?: string;
  dark?: boolean;
}) {
  return (
    <span className={className} aria-hidden="true">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={dark ? "/runway-mark-dark.svg" : "/runway-mark.svg"}
        alt=""
        width="64"
        height="64"
      />
    </span>
  );
}
