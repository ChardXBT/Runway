import { ReviewWorkspace } from "@/components/review-workspace";
import { apiGet } from "@/lib/api";
import type { Proposal } from "@/lib/types";

export default async function ReviewPage({ searchParams }: { searchParams: Promise<{ id?: string }> }) {
  const { id } = await searchParams;
  const settings = await apiGet<{ publishing_enabled: boolean }>(
    "/api/settings",
    { publishing_enabled: false },
  );
  let proposal: Proposal | null = null;
  if (id) proposal = await apiGet<Proposal | null>(`/api/proposals/${id}`, null);
  if (!proposal) {
    const rows = await apiGet<Proposal[]>("/api/proposals?status=needs_review&limit=100", []);
    proposal = rows[0] ?? null;
  }
  if (!proposal) return <><p className="eyebrow">Review</p><h1>Nothing needs review.</h1><section className="panel empty"><div><strong>Your desk is clear.</strong>Generate a fixture batch from the dashboard when you are ready.</div></section></>;
  return (
    <ReviewWorkspace
      initialProposal={proposal}
      publishingEnabled={settings.publishing_enabled}
    />
  );
}
