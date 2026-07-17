import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Proposal } from "@/lib/types";
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
};

afterEach(() => vi.unstubAllGlobals());

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
    });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][0]).toContain("/approve");
    expect(await screen.findByText("approved")).toBeInTheDocument();
  });
});
