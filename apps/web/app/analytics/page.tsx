import type { Metadata } from "next";

import { AnalyticsWorkspace } from "@/components/analytics-workspace";
import { apiGetOptional, apiGetRequired } from "@/lib/api";
import { isAccountAnalytics, isPlatformsResponse } from "@/lib/guards";
import type { AccountAnalytics, PlatformsResponse } from "@/lib/types";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Analytics",
};

const RANGES = new Set(["7", "28", "90"]);

export default async function AnalyticsPage({
  searchParams,
}: {
  searchParams: Promise<{ account?: string; range?: string }>;
}) {
  const params = await searchParams;
  const platforms = await apiGetRequired<PlatformsResponse>(
    "/api/platforms",
    isPlatformsResponse,
  );
  const accounts = platforms.platforms.flatMap((entry) => entry.accounts);
  const connected = accounts.filter((account) => account.status === "connected");
  const requested = Number(params.account);
  const account =
    connected.find((entry) => entry.id === requested) ?? connected[0] ?? null;
  const range = RANGES.has(params.range ?? "") ? params.range! : "28";

  let analytics: AccountAnalytics | null = null;
  let analyticsFailed = false;
  if (account !== null) {
    analytics = await apiGetOptional<AccountAnalytics>(
      `/api/analytics/accounts/${account.id}?range=${range}`,
      isAccountAnalytics,
    ).catch(() => {
      analyticsFailed = true;
      return null;
    });
  }

  return (
    <AnalyticsWorkspace
      accounts={connected}
      account={account}
      analytics={analytics}
      analyticsFailed={analyticsFailed}
      range={range}
    />
  );
}
