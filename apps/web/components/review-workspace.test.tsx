import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  EditorialEnvelope,
  Proposal,
  WorkflowStatus,
} from "@/lib/types";
import { ReviewWorkspace } from "./review-workspace";

const proposal: Proposal = {
  id: 42,
  generation_run_id: 1,
  planned_publish_at: "2026-03-08T10:00:00-04:00",
  scheduled_publish_at: null,
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
    rights_status: "unknown",
    detected_topic: { franchise: "Fixture", characters: ["One"] },
  },
  rights_decision: null,
  rights_reviewed_at: null,
  external_post_id: null,
  external_post_url: null,
  scheduled_verified_at: null,
};

const workflow: WorkflowStatus = {
  needs_review: 2,
  queued: 0,
  scheduled: 0,
  rejected: 0,
  next_available_at: "2026-03-08T10:00:00-04:00",
  posts_per_day: 1,
  timezone: "America/Toronto",
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ReviewWorkspace", () => {
  it("edits, approves, schedules, and immediately advances", async () => {
    const next = {
      ...proposal,
      id: 43,
      final_caption: "Next caption.",
      recommended_caption: "Next caption.",
    };
    const response: EditorialEnvelope = {
      decision: "approved",
      proposal: {
        ...proposal,
        status: "internally_scheduled",
        final_caption: "Why is One so excited?",
        scheduled_publish_at: "2026-03-08T10:00:00-04:00",
      },
      next_proposal: next,
      workflow: { ...workflow, needs_review: 1, queued: 1 },
      publisher_queue: {
        running: true,
        queued: 1,
        paused: false,
        paused_reason: null,
        mode: "youtube",
      },
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => response,
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          detail: "Editorial options are ready.",
          next_proposal: next,
          workflow: { ...workflow, needs_review: 5, queued: 1 },
        }),
      });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ReviewWorkspace
        initialProposal={proposal}
        initialWorkflow={workflow}
        publishingEnabled
      />,
    );

    expect(screen.queryByText(/rights|provenance|copyright/i)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Caption"), {
      target: { value: "Why is One so excited?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Approve & schedule" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[0][0]).toContain(
      "/api/editorial/proposals/42/approve",
    );
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      final_caption: "Why is One so excited?",
    });
    expect(await screen.findByDisplayValue("Next caption.")).toBeInTheDocument();
    expect(screen.getByLabelText("Editorial session status")).toHaveTextContent(
      "1 this session",
    );
    expect(fetchMock.mock.calls[1][0]).toContain("/api/editorial/options/ensure");
  });

  it("turns rejection into a negative signal and advances", async () => {
    const next = { ...proposal, id: 44, final_caption: "Another option." };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        decision: "rejected",
        proposal: { ...proposal, status: "rejected" },
        next_proposal: next,
        workflow: { ...workflow, needs_review: 3, rejected: 1 },
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ReviewWorkspace
        initialProposal={proposal}
        initialWorkflow={workflow}
        publishingEnabled
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toContain(
      "/api/editorial/proposals/42/reject",
    );
    expect(await screen.findByDisplayValue("Another option.")).toBeInTheDocument();
  });

  it("rejects an image as negative feedback and advances", async () => {
    const next = { ...proposal, id: 45, final_caption: "Fresh image option." };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        decision: "image_rejected",
        proposal: { ...proposal, status: "rejected" },
        next_proposal: next,
        workflow: { ...workflow, needs_review: 3, rejected: 1 },
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ReviewWorkspace
        initialProposal={proposal}
        initialWorkflow={workflow}
        publishingEnabled
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Another image" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toContain(
      "/api/editorial/proposals/42/skip-image",
    );
    expect(await screen.findByDisplayValue("Fresh image option.")).toBeInTheDocument();
    expect(screen.getByLabelText("Editorial session status")).toHaveTextContent(
      "1 this session",
    );
  });

  it("replenishes the conveyor when the tray is empty", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        detail: "Editorial options are ready.",
        next_proposal: proposal,
        workflow,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ReviewWorkspace
        initialProposal={null}
        initialWorkflow={{ ...workflow, needs_review: 0 }}
        publishingEnabled
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Find more options" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toContain("/api/editorial/options/ensure");
    expect(await screen.findByDisplayValue("Recommended caption.")).toBeInTheDocument();
  });
});
