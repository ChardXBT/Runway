import type { Metadata } from "next";

import { ApprovalsWorkspace } from "@/components/approvals-workspace";
import { apiGetRequired } from "@/lib/api";
import { isApprovalPage, type ApprovalPage } from "@/lib/v1-guards";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Approvals",
};

export default async function ApprovalsPage() {
  const page = await apiGetRequired<ApprovalPage>("/api/v1/approvals", isApprovalPage);
  return <ApprovalsWorkspace initialPage={page} />;
}
