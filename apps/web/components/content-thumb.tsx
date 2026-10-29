"use client";

import { useState } from "react";

import { PlatformGlyph } from "@/components/platform-badge";
import type { PlatformKind } from "@/lib/types";

export function ContentThumb({
  src,
  platform,
  size = 40,
  width,
  height,
}: {
  src: string | null;
  platform: PlatformKind;
  size?: number;
  width?: number;
  height?: number;
}) {
  const [failed, setFailed] = useState(false);
  const [prevSrc, setPrevSrc] = useState(src);
  if (prevSrc !== src) {
    setPrevSrc(src);
    setFailed(false);
  }
  const w = width ?? size;
  const h = height ?? size;
  if (src && !failed) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        className="content-thumb"
        src={src}
        alt=""
        width={w}
        height={h}
        onError={() => setFailed(true)}
      />
    );
  }
  return (
    <span
      className="content-thumb content-thumb-empty"
      style={{ width: w, height: h }}
      aria-hidden="true"
    >
      <PlatformGlyph platform={platform} />
    </span>
  );
}
