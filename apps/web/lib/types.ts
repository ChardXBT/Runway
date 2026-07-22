export type CandidateTopic = {
  franchise?: string | null;
  characters?: string[];
  scene_archetype?: string;
  composition?: string;
  emotion?: string;
};

export type Proposal = {
  id: number;
  generation_run_id: number;
  planned_publish_at: string;
  scheduled_publish_at: string | null;
  status: string;
  candidate_image_id: number;
  backup_candidate_ids: number[];
  recommended_caption: string;
  alternative_captions: string[];
  caption_rationale: string;
  caption_confidence: number | null;
  caption_reference_post_ids: number[];
  factual_uncertainty_warning: string | null;
  final_caption: string;
  selection_reason: string;
  scores: { style: number; novelty: number; quality: number };
  closest_historical_matches: {
    post_id: number;
    caption: string;
    published_at: string | null;
    visual_similarity: number;
    media_url?: string;
  }[];
  warnings: string[];
  rights_decision: string | null;
  rights_reviewed_at: string | null;
  approved_at?: string | null;
  rejected_at?: string | null;
  external_post_id: string | null;
  external_post_url: string | null;
  scheduled_verified_at: string | null;
  caption_feedback?: {
    id: number;
    verdict: string;
    generated_caption: string;
    preferred_caption: string | null;
    preferred_structure: string | null;
    reason_codes: string[];
    image_verdict: string | null;
    note: string | null;
    created_at: string;
  }[];
  candidate: {
    original_url: string | null;
    preview_url: string | null;
    source_page_url: string | null;
    direct_image_url: string | null;
    source_domain: string | null;
    rights_status: string;
    detected_topic: CandidateTopic;
  } | null;
};

export type PublishPreparation = {
  attempt_id: number;
  proposal_id: number;
  status: string;
  confirmation_token: string;
  confirmation_phrase: string;
  expires_at: string;
  planned_publish_at: string;
  caption: string;
  local_image_path: string;
  channel_name: string;
};

export type WorkflowStatus = {
  needs_review: number;
  queued: number;
  scheduled: number;
  rejected: number;
  next_available_at: string;
  posts_per_day: number;
  timezone: string;
};

export type GenerationActivity = {
  running: boolean;
  started_at: string | null;
  completed_at: string | null;
  detail: string | null;
};

export type PublisherQueueStatus = {
  running: boolean;
  queued: number;
  paused: boolean;
  paused_reason: string | null;
};

export type PublishingMode = "assisted" | "authorized_browser";

export type AssistedPublishingItem = {
  proposal_id: number;
  planned_publish_at: string;
  caption: string;
  image_url: string;
  rights_status: string;
  warnings: string[];
};

export type AssistedPublishingWorkspace = {
  mode: "assisted";
  channel_name: string;
  channel_id: string;
  timezone: string;
  youtube_url: string;
  items: AssistedPublishingItem[];
};

export type LineupPushResponse = {
  mode: PublishingMode;
  detail: string;
  queued_proposal_ids: number[];
  assisted_workspace?: AssistedPublishingWorkspace;
  lineup: LineupSchedule;
  publisher_queue: PublisherQueueStatus;
};

export type PublisherConnectionStatus = {
  state: "disabled" | "unchecked" | "connected" | "stale" | "needs_attention";
  valid: boolean | null;
  detail: string;
  publisher: string;
  checked_at: string | null;
  stale: boolean;
  stale_after_hours: number;
  checks: Record<string, boolean>;
  last_verified_publish_at: string | null;
};

export type LineupSchedule = {
  timezone: string;
  default_time: string;
  posts_per_day: number;
  coverage: number;
  next_available_at: string;
  scheduled: Proposal[];
};

export type EditorialEnvelope = {
  decision?: "approved" | "rejected" | "image_replaced";
  detail?: string;
  proposal?: Proposal;
  next_proposal: Proposal | null;
  workflow: WorkflowStatus;
  publisher_queue?: PublisherQueueStatus;
  generation?: GenerationActivity;
};
