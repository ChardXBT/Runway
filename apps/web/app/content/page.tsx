import type { Metadata } from "next";

import { ContentLibrary } from "@/components/content-library";
import { apiGetRequired } from "@/lib/api";
import { isContentListResponse, isPlatformsResponse } from "@/lib/guards";
import type { ContentListResponse, PlatformsResponse } from "@/lib/types";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Content",
};

const SORTS = new Set(["published_at", "view_count", "like_count", "comment_count"]);
const PAGE_SIZE = 50;

export default async function ContentPage({
  searchParams,
}: {
  searchParams: Promise<{
    platform?: string;
    type?: string;
    q?: string;
    sort?: string;
    order?: string;
    account?: string;
    offset?: string;
  }>;
}) {
  const params = await searchParams;
  const platforms = await apiGetRequired<PlatformsResponse>(
    "/api/platforms",
    isPlatformsResponse,
  );
  const accounts = platforms.platforms.flatMap((entry) => entry.accounts);

  const sort = SORTS.has(params.sort ?? "") ? params.sort! : "published_at";
  const order = params.order === "asc" ? "asc" : "desc";
  const offset = Math.max(0, Number(params.offset) || 0);

  const query = new URLSearchParams();
  if (params.platform) query.set("platform", params.platform);
  if (params.account) query.set("account_id", params.account);
  if (params.type) query.set("type", params.type);
  if (params.q) query.set("q", params.q);
  query.set("sort", sort);
  query.set("order", order);
  query.set("limit", String(PAGE_SIZE));
  query.set("offset", String(offset));

  const list = await apiGetRequired<ContentListResponse>(
    `/api/content?${query.toString()}`,
    isContentListResponse,
  );

  return (
    <ContentLibrary
      accounts={accounts}
      list={list}
      filters={{
        platform: params.platform ?? "",
        type: params.type ?? "",
        q: params.q ?? "",
        account: params.account ?? "",
        sort,
        order,
        offset,
      }}
      pageSize={PAGE_SIZE}
    />
  );
}
