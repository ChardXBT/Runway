import { ReviewWorkspace } from "@/components/review-workspace";
import { apiGetRequired } from "@/lib/api";
import { isEditorialEnvelope } from "@/lib/guards";
import type {
  EditorialEnvelope,
  GenerationActivity,
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
  let requestedId: number | null = null;
  if (id !== undefined) {
    requestedId = Number(id);
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
  }

  const editorial = await apiGetRequired<EditorialEnvelope>(
    requestedId === null
      ? "/api/editorial/next"
      : `/api/editorial/next?proposal_id=${requestedId}`,
    isEditorialEnvelope,
  );
  const proposal = editorial.next_proposal;
  if (requestedId !== null && !proposal) {
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

  return (
    <ReviewWorkspace
      initialProposal={proposal}
      initialWorkflow={editorial.workflow}
      initialGeneration={editorial.generation ?? fallbackGeneration}
    />
  );
}
