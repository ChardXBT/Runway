import { ReviewWorkspace } from "@/components/review-workspace";
import { apiGetOptional, apiGetRequired } from "@/lib/api";
import {
  isEditorialEnvelope,
  isProposal,
} from "@/lib/guards";
import type {
  EditorialEnvelope,
  GenerationActivity,
  Proposal
} from "@/lib/types";

export const dynamic = "force-dynamic";

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
  const editorial = await apiGetRequired<EditorialEnvelope>(
    "/api/editorial/next",
    isEditorialEnvelope,
  );

  let proposal = editorial.next_proposal;
  if (id !== undefined) {
    const requestedId = Number(id);
    if (!Number.isInteger(requestedId) || requestedId < 1) {
      return (
        <section className="panel empty">
          <div>
            <strong>Requested Generator option is invalid.</strong>
            <a href="/review">Return to the current Generator option</a>
          </div>
        </section>
      );
    }
    const requested = await apiGetOptional<Proposal>(
      `/api/proposals/${requestedId}`,
      isProposal,
    );
    if (!requested || requested.status !== "needs_review") {
      return (
        <section className="panel empty">
          <div>
            <strong>Requested Generator option is unavailable.</strong>
            <span>
              It may already have been accepted, rejected, or removed from review.
            </span>
            <a href="/review">Return to the current Generator option</a>
          </div>
        </section>
      );
    }
    proposal = requested;
  }

  return (
    <ReviewWorkspace
      initialProposal={proposal}
      initialWorkflow={editorial.workflow}
      initialGeneration={editorial.generation ?? fallbackGeneration}
    />
  );
}
