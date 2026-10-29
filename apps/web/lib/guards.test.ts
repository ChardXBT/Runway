import { describe, expect, it } from "vitest";

import { isAccountView, isProposal } from "./guards";
import type { AccountView, Proposal } from "./types";

const proposal: Proposal = {
  id: 42,
  generation_run_id: 1,
  planned_publish_at: "2026-03-08T10:00:00-04:00",
  scheduled_publish_at: null,
  status: "needs_review",
  candidate_image_id: 7,
  backup_candidate_ids: [8, 9],
  recommended_caption: "Recommended caption.",
  alternative_captions: ["Alternative one."],
  caption_rationale: "Grounded in short historical reactions.",
  caption_confidence: 0.91,
  caption_reference_post_ids: [2, 7],
  factual_uncertainty_warning: null,
  final_caption: "Recommended caption.",
  selection_reason: "Strong visual fit.",
  scores: { style: 0.8, novelty: 0.7, quality: 0.9 },
  closest_historical_matches: [],
  warnings: [],
  rights_decision: null,
  rights_reviewed_at: null,
  rejected_at: "2026-03-08T10:00:00-04:00",
  external_post_id: null,
  external_post_url: null,
  scheduled_verified_at: null,
  latest_publish_attempt: null,
  candidate: {
    original_url: "/media/candidate/example.jpg",
    preview_url: "/media/previews/example.jpg",
    source_page_url: "https://fixture.local/example",
    direct_image_url: "fixture://candidate-07",
    source_domain: "fixture.local",
    rights_status: "unknown",
    detected_topic: { franchise: "Fixture", characters: ["One"] },
  },
};

const account: AccountView = {
  id: 7,
  platform: "youtube",
  external_account_id: "UC1",
  handle: "northwindstudio",
  display_name: "Northwind Studio",
  avatar_url: null,
  profile_url: null,
  account_type: null,
  status: "connected",
  granted_scopes: [],
  scopes_source: "granted",
  connected_at: "2026-07-01T10:00:00",
  last_verified_at: null,
  last_sync_at: null,
  last_sync_status: null,
  last_error: null,
  freshness: "fresh",
  capabilities: [],
  latest_snapshot: null,
};

describe("isProposal", () => {
  it("accepts a well-formed proposal", () => {
    expect(isProposal(proposal)).toBe(true);
    expect(isProposal({ ...proposal, rejected_at: null })).toBe(true);
    expect(isProposal({ ...proposal, caption_confidence: null })).toBe(true);
    expect(isProposal({ ...proposal, candidate: null })).toBe(true);
  });

  it("rejects non-string rationale, selection_reason, and original_url", () => {
    expect(isProposal({ ...proposal, caption_rationale: { x: 1 } })).toBe(false);
    expect(isProposal({ ...proposal, selection_reason: 42 })).toBe(false);
    expect(
      isProposal({
        ...proposal,
        candidate: { ...proposal.candidate!, original_url: { x: 1 } },
      }),
    ).toBe(false);
  });

  it("rejects a non-numeric confidence and a malformed rejected_at", () => {
    expect(isProposal({ ...proposal, caption_confidence: "high" })).toBe(false);
    expect(isProposal({ ...proposal, rejected_at: "not-a-date" })).toBe(false);
    expect(isProposal({ ...proposal, rejected_at: 42 })).toBe(false);
  });
});

describe("isAccountView", () => {
  it("accepts a well-formed account", () => {
    expect(isAccountView(account)).toBe(true);
  });

  it("rejects non-string timestamp fields", () => {
    expect(isAccountView({ ...account, connected_at: 123 })).toBe(false);
    expect(isAccountView({ ...account, last_sync_at: {} })).toBe(false);
    expect(isAccountView({ ...account, last_verified_at: false })).toBe(false);
  });
});
