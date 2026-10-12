import { NextResponse, type NextRequest } from "next/server";

import { isLoopbackHost } from "./lib/host-guard";

export function proxy(request: NextRequest) {
  if (!isLoopbackHost(request.headers.get("host"))) {
    return new NextResponse("Runway only serves loopback hosts.", {
      status: 421,
      headers: { "Cache-Control": "no-store" },
    });
  }
  return NextResponse.next();
}
