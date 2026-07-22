import { ReviewWorkspace } from "@/components/review-workspace";
import { apiGet } from "@/lib/api";
import {
  isEditorialEnvelope,
  isProposal,
} from "@/lib/guards";
import type {
  EditorialEnvelope,
  GenerationActivity,
  Proposal,
  WorkflowStatus,
} from "@/lib/types";

const fallbackWorkflow: WorkflowStatus = {
  needs_review: 0,
  queued: 0,
  scheduled: 0,
  rejected: 0,
  next_available_at: new Date(Date.now() + 86_400_000).toISOString(),
  posts_per_day: 1,
  timezone: "America/Toronto",
};

const fallbackGeneration: GenerationActivity = {
  running: false,
  started_at: null,
  completed_at: null,
  detail: null,
};

export default async function ReviewPage({
  searchParams,
}: {
  searchParams: Promise<{ id?: string }>;
}) {
  const { id } = await searchParams;
  const editorial = await apiGet<EditorialEnvelope>("/api/editorial/next", {
      next_proposal: null,
      workflow: fallbackWorkflow,
    }, isEditorialEnvelope);

  let proposal = editorial.next_proposal;
  if (id) {
    const requested = await apiGet<Proposal | null>(
      `/api/proposals/${id}`,
      null,
      (value): value is Proposal | null => value === null || isProposal(value),
    );
    if (requested?.status === "needs_review") proposal = requested;
  }

  return (
    <ReviewWorkspace
      initialProposal={proposal}
      initialWorkflow={editorial.workflow}
      initialGeneration={editorial.generation ?? fallbackGeneration}
    />
  );
}
