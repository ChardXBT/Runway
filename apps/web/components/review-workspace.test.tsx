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
  latest_publish_attempt: null,
};


function markProposalImageLoaded() {
  fireEvent.load(
    screen.getByRole("img", { name: /Proposed Fixture image/ }),
  );
}

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
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ReviewWorkspace", () => {
  it("preserves punctuation, adds to Lineup, and immediately advances", async () => {
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
        final_caption: "Why is One so excited?!",
        scheduled_publish_at: "2026-03-08T10:00:00-04:00",
      },
      next_proposal: next,
      workflow: { ...workflow, needs_review: 1, queued: 1 },
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
    markProposalImageLoaded();
    fireEvent.change(screen.getByLabelText("Primary caption"), {
      target: { value: "Why is One so excited?!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[0][0]).toContain(
      "/api/editorial/proposals/42/approve",
    );
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      final_caption: "Why is One so excited?!",
    });
    expect(await screen.findByDisplayValue("Next caption.")).toBeInTheDocument();
    expect(screen.getByLabelText("Editorial session status")).toHaveTextContent(
      "1 decisions",
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Accepted and added to Lineup",
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Nothing was sent to YouTube",
    );
    expect(screen.queryByText(/publishing|browser queue/i)).not.toBeInTheDocument();
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
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      reason: "The complete option was not a fit.",
      reason_codes: ["not_engaging"],
    });
    expect(await screen.findByDisplayValue("Another option.")).toBeInTheDocument();
  });

  it("records a targeted cluster signal when asking for fewer similar images", async () => {
    const next = { ...proposal, id: 46, final_caption: "A distinct option." };
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
    fireEvent.click(screen.getByRole("button", { name: "Fewer like this" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toContain(
      "/api/editorial/proposals/42/reject",
    );
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      reason:
        "This image is too similar to recent options. Show fewer images from this visual cluster.",
      reason_codes: ["too_similar"],
    });
    expect(await screen.findByDisplayValue("A distinct option.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Similar images will receive a negative preference signal.",
    );
  });

  it("keeps the primary decision surface to Reject, Edit, and Accept", () => {
    render(
      <ReviewWorkspace
        initialProposal={proposal}
        initialWorkflow={workflow}
        publishingEnabled
      />,
    );
    const controls = screen.getByLabelText("Decision controls");
    expect(controls).toHaveTextContent("Reject");
    expect(controls).toHaveTextContent("Edit");
    expect(controls).toHaveTextContent("Accept");
    expect(screen.getByRole("button", { name: "Replace image" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Fewer like this" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Regenerate captions" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Alternative captions")).toBeInTheDocument();
    expect(screen.getByLabelText("Primary caption")).toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: "Accept" })).toBeDisabled();
    markProposalImageLoaded();
    expect(screen.getByRole("button", { name: "Accept" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByLabelText("Primary caption")).not.toHaveAttribute("readonly");
  });

  it("enables Accept when a cached image completed before hydration", async () => {
    vi.spyOn(HTMLImageElement.prototype, "complete", "get").mockReturnValue(true);
    vi.spyOn(HTMLImageElement.prototype, "naturalWidth", "get").mockReturnValue(640);

    render(
      <ReviewWorkspace
        initialProposal={proposal}
        initialWorkflow={workflow}
        publishingEnabled
      />,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Accept" })).toBeEnabled();
    });
    expect(screen.getByRole("status")).not.toHaveTextContent(
      "Loading the exact image",
    );
  });

  it("regenerates captions without changing the image", async () => {
    const refreshed = {
      ...proposal,
      recommended_caption: "What has One so excited?",
      final_caption: "What has One so excited?",
      alternative_captions: ["What happens next?"],
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => refreshed,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ReviewWorkspace
        initialProposal={proposal}
        initialWorkflow={workflow}
        publishingEnabled
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Regenerate captions" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toContain("/api/proposals/42/regenerate");
    expect(
      await screen.findByDisplayValue("What has One so excited?"),
    ).toBeInTheDocument();
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
    fireEvent.click(screen.getByRole("button", { name: "Generate more" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[0][0]).toContain("/api/editorial/options/ensure");
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      target: 1,
      live_discovery: true,
    });
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      target: 5,
      live_discovery: false,
    });
    expect(await screen.findByDisplayValue("Recommended caption.")).toBeInTheDocument();
  });

  it("recovers an in-progress generation after returning to Generator", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        next_proposal: proposal,
        workflow,
        generation: {
          running: false,
          started_at: "2026-03-07T15:00:00Z",
          completed_at: "2026-03-07T15:02:00Z",
          detail: "Editorial options are ready.",
        },
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ReviewWorkspace
        initialProposal={null}
        initialWorkflow={{ ...workflow, needs_review: 0 }}
        initialGeneration={{
          running: true,
          started_at: "2026-03-07T15:00:00Z",
          completed_at: null,
          detail: "Discovering images and generating captions.",
        }}
        publishingEnabled
      />,
    );

    expect(screen.getByRole("button", { name: "Generating…" })).toBeDisabled();
    expect(screen.getByText("Building the next look.")).toBeInTheDocument();
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/editorial/next"),
        expect.objectContaining({
          cache: "no-store",
          signal: expect.any(AbortSignal),
        }),
      ),
    );
    expect(await screen.findByDisplayValue("Recommended caption.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "The generated option is ready.",
    );
  });

  it("cancels an in-flight generation check when Generator unmounts", async () => {
    let requestSignal: AbortSignal | undefined;
    const fetchMock = vi.fn().mockImplementation(
      (_url: string, init?: RequestInit) => {
        requestSignal = init?.signal as AbortSignal | undefined;
        return new Promise(() => undefined);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = render(
      <ReviewWorkspace
        initialProposal={null}
        initialWorkflow={{ ...workflow, needs_review: 0 }}
        initialGeneration={{
          running: true,
          started_at: "2026-03-07T15:00:00Z",
          completed_at: null,
          detail: "Discovering images and generating captions.",
        }}
        publishingEnabled
      />,
    );

    await waitFor(() => expect(requestSignal).toBeDefined());
    unmount();

    expect(requestSignal?.aborted).toBe(true);
  });

  it("shows a completed no-result explanation and allows another search", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          next_proposal: null,
          workflow: { ...workflow, needs_review: 0 },
          generation: {
            running: false,
            started_at: "2026-03-07T15:00:00Z",
            completed_at: "2026-03-07T15:02:00Z",
            detail: "No usable image candidates were found.",
          },
        }),
      }),
    );

    render(
      <ReviewWorkspace
        initialProposal={null}
        initialWorkflow={{ ...workflow, needs_review: 0 }}
        initialGeneration={{
          running: true,
          started_at: "2026-03-07T15:00:00Z",
          completed_at: null,
          detail: "Discovering images and generating captions.",
        }}
        publishingEnabled
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No usable image candidates were found.",
    );
    expect(screen.getByRole("button", { name: "Generate more" })).toBeEnabled();
    expect(screen.queryByText("Building the next look.")).not.toBeInTheDocument();
  });

  it("locks an accept immediately so double clicks submit only once", async () => {
    let resolveResponse!: (value: unknown) => void;
    const pending = new Promise((resolve) => {
      resolveResponse = resolve;
    });
    const fetchMock = vi.fn().mockReturnValue(pending);
    vi.stubGlobal("fetch", fetchMock);
    const next = {
      ...proposal,
      id: 45,
      final_caption: "Next safe option.",
      recommended_caption: "Next safe option.",
    };

    render(
      <ReviewWorkspace
        initialProposal={proposal}
        initialWorkflow={{ ...workflow, needs_review: 5 }}
        publishingEnabled
      />,
    );
    markProposalImageLoaded();
    fireEvent.change(screen.getByLabelText("Primary caption"), {
      target: { value: "Why is One celebrating?!" },
    });
    const accept = screen.getByRole("button", { name: "Accept" });
    fireEvent.click(accept);
    fireEvent.click(accept);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Accepting…" })).toBeDisabled();

    resolveResponse({
      ok: true,
      json: async () => ({
        decision: "approved",
        proposal: {
          ...proposal,
          status: "internally_scheduled",
          final_caption: "Why is One celebrating?!",
          scheduled_publish_at: "2026-03-08T10:00:00-04:00",
        },
        next_proposal: next,
        workflow: { ...workflow, needs_review: 4, queued: 1 },
      }),
    });

    expect(await screen.findByDisplayValue("Next safe option.")).toBeInTheDocument();
  });

  it("preserves the caption and blocks another decision after an ambiguous network failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
    );
    render(
      <ReviewWorkspace
        initialProposal={proposal}
        initialWorkflow={workflow}
        publishingEnabled
      />,
    );
    markProposalImageLoaded();
    fireEvent.change(screen.getByLabelText("Primary caption"), {
      target: { value: "Wait... what?!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "response could not be verified",
    );
    expect(screen.getByDisplayValue("Wait... what?!")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Accept" })).toBeDisabled();
    expect(
      screen.getByRole("link", { name: "Reload Generator to verify" }),
    ).toHaveAttribute("href", "/review");
  });

  it("does not advance or claim success for a malformed approval response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ decision: "approved" }),
      }),
    );
    render(
      <ReviewWorkspace
        initialProposal={proposal}
        initialWorkflow={workflow}
        publishingEnabled
      />,
    );
    markProposalImageLoaded();
    fireEvent.change(screen.getByLabelText("Primary caption"), {
      target: { value: "Is this really happening?!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "response could not be verified",
    );
    expect(
      screen.getByDisplayValue("Is this really happening?!"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Accepted for/)).not.toBeInTheDocument();
  });

  it("keeps the decision retryable after a confirmed server rejection", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        json: async () => ({ detail: "Caption is not valid yet." }),
      }),
    );
    render(
      <ReviewWorkspace
        initialProposal={proposal}
        initialWorkflow={workflow}
        publishingEnabled
      />,
    );
    markProposalImageLoaded();
    fireEvent.change(screen.getByLabelText("Primary caption"), {
      target: { value: "Keep this?!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Caption is not valid yet.",
    );
    expect(screen.getByDisplayValue("Keep this?!")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Accept" })).toBeEnabled();
    expect(
      screen.queryByRole("link", { name: "Reload Generator to verify" }),
    ).not.toBeInTheDocument();
  });

  it("requires the next proposal image to load before enabling its Accept action", async () => {
    const next = {
      ...proposal,
      id: 99,
      candidate_image_id: 99,
      candidate: {
        ...proposal.candidate!,
        preview_url: "/media/previews/next.jpg",
      },
      final_caption: "Next image caption.",
      recommended_caption: "Next image caption.",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          decision: "rejected",
          proposal: { ...proposal, status: "rejected" },
          next_proposal: next,
          workflow: { ...workflow, needs_review: 1, rejected: 1 },
        }),
      }),
    );
    render(
      <ReviewWorkspace
        initialProposal={proposal}
        initialWorkflow={workflow}
        publishingEnabled
      />,
    );
    markProposalImageLoaded();
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));

    expect(await screen.findByDisplayValue("Next image caption.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Accept" })).toBeDisabled();
    fireEvent.load(screen.getByRole("img", { name: /Proposed Fixture image/ }));
    expect(screen.getByRole("button", { name: "Accept" })).toBeEnabled();
  });
});
