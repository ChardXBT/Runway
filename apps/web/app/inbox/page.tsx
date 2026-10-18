import type { Metadata } from "next";

import { InboxWorkspace } from "@/components/inbox-workspace";
import { apiGetRequired } from "@/lib/api";
import { isInboxListResponse, isPlatformsResponse } from "@/lib/guards";
import type { InboxListResponse, PlatformsResponse } from "@/lib/types";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Inbox",
};

export default async function InboxPage({
  searchParams,
}: {
  searchParams: Promise<{
    platform?: string;
    account?: string;
    status?: string;
    unanswered?: string;
    q?: string;
    id?: string;
  }>;
}) {
  const params = await searchParams;
  const platforms = await apiGetRequired<PlatformsResponse>(
    "/api/platforms",
    isPlatformsResponse,
  );
  const accounts = platforms.platforms.flatMap((entry) => entry.accounts);

  const unanswered = params.unanswered !== "0";
  const query = new URLSearchParams();
  if (params.platform) query.set("platform", params.platform);
  if (params.account) query.set("account_id", params.account);
  if (params.status) query.set("status", params.status);
  if (unanswered) query.set("unanswered", "true");
  if (params.q) query.set("q", params.q);
  query.set("limit", "50");

  const list = await apiGetRequired<InboxListResponse>(
    `/api/inbox?${query.toString()}`,
    isInboxListResponse,
  );

  return (
    <InboxWorkspace
      accounts={accounts}
      list={list}
      filters={{
        platform: params.platform ?? "",
        account: params.account ?? "",
        status: params.status ?? "",
        unanswered,
        q: params.q ?? "",
      }}
      selectedId={params.id ? Number(params.id) : null}
    />
  );
}
