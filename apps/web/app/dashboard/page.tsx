import type { Metadata } from "next";

import { DashboardOverview } from "@/components/dashboard-overview";
import { apiGetRequired } from "@/lib/api";
import { isOverviewResponse } from "@/lib/guards";
import type { OverviewResponse } from "@/lib/types";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Dashboard",
};

export default async function DashboardPage() {
  const overview = await apiGetRequired<OverviewResponse>(
    "/api/overview",
    isOverviewResponse,
  );
  return <DashboardOverview overview={overview} />;
}
