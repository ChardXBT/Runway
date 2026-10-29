"use client";

import { useState } from "react";

export function AvatarImage({
  src,
  label,
  size = 34,
}: {
  src: string | null;
  label: string;
  size?: number;
}) {
  const [failed, setFailed] = useState(false);
  const [prevSrc, setPrevSrc] = useState(src);
  if (prevSrc !== src) {
    setPrevSrc(src);
    setFailed(false);
  }
  if (src && !failed) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={src}
        alt=""
        width={size}
        height={size}
        onError={() => setFailed(true)}
      />
    );
  }
  return (
    <span
      className="platform-card-avatar"
      style={{ width: size, height: size }}
      aria-hidden="true"
    >
      {label.charAt(0)}
    </span>
  );
}
