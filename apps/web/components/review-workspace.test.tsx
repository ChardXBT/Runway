import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Proposal, PublishPreparation } from "@/lib/types";
import { ReviewWorkspace } from "./review-workspace";

const proposal: Proposal = {
  id: 42,
  generation_run_id: 1,
  planned_publish_at: "2026-03-08T10:00:00-04:00",
  status: "needs_review",
  candidate_image_id: 7,
  backup_candidate_ids: [8, 9],
  recommended_caption: "Recommended caption.",
  alternative_captions: ["Alternative one.", "Alternative two."],
  caption_rationale: "Grounded in short historical reactions.",
  caption_confidence: 0.91,
  caption_reference_post_ids: [2, 7],
  factual_uncertainty_warning: "The exact scene is unknown.",
  final_caption: "Recommended caption.",
  selection_reason: "Strong visual fit.",
  scores: { style: 0.8, novelty: 0.7, quality: 0.9 },
  closest_historical_matches: [],
  warnings: [],
  candidate: {
    original_url: "/media/candidate/example.jpg",
    preview_url: "/media/previews/example.jpg",
    source_page_url: "https://fixture.local/example",
    direct_image_url: "fixture://candidate-07",
    source_domain: "fixture.local",
    rights_status: "creator_owned",
    detected_topic: { franchise: "Fixture", characters: ["One"] },
  },
  rights_decision: null,
  rights_reviewed_at: null,
  external_post_id: null,
  external_post_url: null,
  scheduled_verified_at: null,
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ReviewWorkspace", () => {
  it("saves the authoritative caption and then approves", async () => {
    const edited = { ...proposal, final_caption: "Human final caption." };
    const approved = { ...edited, status: "approved" };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => edited })
      .mockResolvedValueOnce({ ok: true, json: async () => approved });
    vi.stubGlobal("fetch", fetchMock);

    render(<ReviewWorkspace initialProposal={proposal} />);
    expect(screen.getByText("Grounded in short historical reactions.")).toBeInTheDocument();
    expect(screen.getByText("The exact scene is unknown.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Final caption"), {
      target: { value: "Human final caption." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save caption" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      final_caption: "Human final caption.",
      reason_codes: [],
      note: null,
      image_verdict: "unsure",
    });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][0]).toContain("/approve");
    expect(await screen.findByText("approved")).toBeInTheDocument();
  });

  it("records a reusable preference without approving or publishing", async () => {
    const learned = {
      ...proposal,
      caption_feedback: [
        {
          id: 1,
          verdict: "preferred",
          generated_caption: proposal.final_caption,
          preferred_caption: "Why is One so excited?",
          preferred_structure: "open_question",
          reason_codes: ["prefer_open_question"],
          image_verdict: "good",
          note: null,
          created_at: "2026-03-01T10:00:00Z",
        },
      ],
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => learned,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ReviewWorkspace initialProposal={proposal} />);
    fireEvent.change(screen.getByLabelText("Final caption"), {
      target: { value: "Why is One so excited?" },
    });
    fireEvent.click(screen.getByText("Prefer an open question"));
    fireEvent.click(screen.getByRole("button", { name: "Save as preferred" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toContain("/feedback");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toMatchObject({
      verdict: "preferred",
      preferred_caption: "Why is One so excited?",
      preferred_structure: "open_question",
      reason_codes: ["prefer_open_question"],
    });
    expect(fetchMock.mock.calls[0][0]).not.toContain("/approve");
    expect(fetchMock.mock.calls[0][0]).not.toContain("/youtube");
  });

  it("does not expose the external submit action before exact preparation", async () => {
    const internallyScheduled = {
      ...proposal,
      status: "internally_scheduled",
      final_caption: "Why is One so excited?",
    };
    const preparation: PublishPreparation = {
      attempt_id: 9,
      proposal_id: proposal.id,
      status: "prepared",
      confirmation_token: "token-long-enough-for-the-api",
      confirmation_phrase: `SCHEDULE QLOB #${proposal.id}`,
      expires_at: "2026-03-08T13:55:00Z",
      planned_publish_at: proposal.planned_publish_at,
      caption: internallyScheduled.final_caption,
      local_image_path: "data/media/approved/example.jpg",
      channel_name: "Qlob",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => preparation,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ReviewWorkspace initialProposal={internallyScheduled} publishingEnabled />,
    );
    expect(
      screen.queryByRole("button", { name: "Schedule on YouTube" }),
    ).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Prepare YouTube schedule" }),
    );

    const submit = await screen.findByRole("button", {
      name: "Schedule on YouTube",
    });
    expect(submit).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain("/youtube/prepare");
  });
});
