import type { Metadata } from "next";

import { ApprovalsWorkspace } from "@/components/approvals-workspace";
import { editorAccounts } from "@/components/post-editor";
import { apiGet, apiGetRequired } from "@/lib/api";
import { isPlatformsResponse } from "@/lib/guards";
import type { PlatformsResponse } from "@/lib/types";
import { isApprovalPage, type ApprovalPage } from "@/lib/v1-guards";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Approvals",
};

export default async function ApprovalsPage() {
  // The account labels are best-effort: without them the release dialog falls
  // back to "account {id}".
  const [page, platforms] = await Promise.all([
    apiGetRequired<ApprovalPage>("/api/v1/approvals", isApprovalPage),
    apiGet<PlatformsResponse | null>("/api/platforms", null, isPlatformsResponse),
  ]);
  const accounts = platforms
    ? editorAccounts(platforms.platforms.flatMap((platform) => platform.accounts))
    : [];
  return <ApprovalsWorkspace initialPage={page} accounts={accounts} />;
}
