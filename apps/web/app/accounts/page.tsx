import type { Metadata } from "next";

import { AccountsManager } from "@/components/accounts-manager";
import { apiGetRequired } from "@/lib/api";
import { isPlatformsResponse } from "@/lib/guards";
import type { PlatformsResponse } from "@/lib/types";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Accounts",
};

export default async function AccountsPage({
  searchParams,
}: {
  searchParams: Promise<{ connected?: string; error?: string }>;
}) {
  const params = await searchParams;
  const data = await apiGetRequired<PlatformsResponse>(
    "/api/platforms",
    isPlatformsResponse,
  );
  return (
    <AccountsManager
      data={data}
      connected={params.connected ?? null}
      errorCode={params.error ?? null}
    />
  );
}
