import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export function GET() {
  return NextResponse.json(
    { product: "Runway", component: "web", status: "ok" },
    { headers: { "Cache-Control": "no-store" } },
  );
}
