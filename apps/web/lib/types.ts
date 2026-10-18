export type CandidateTopic = {
  franchise?: string | null;
  characters?: string[];
  scene_archetype?: string;
  composition?: string;
  emotion?: string;
};

export type ProposalStatus =
  | "generating"
  | "needs_review"
  | "approved"
  | "rejected"
  | "internally_scheduled"
  | "publishing"
  | "externally_scheduled"
  | "publish_unverified"
  | "publish_failed"
  | "published"
  | "cancelled";

export type LatestPublishAttempt = {
  id: number;
  status: string;
  error_summary: string | null;
  prepared_at: string;
  submitted_at: string | null;
  completed_at: string | null;
};

export type Proposal = {
  id: number;
  generation_run_id: number;
  planned_publish_at: string;
  scheduled_publish_at: string | null;
  status: ProposalStatus;
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
  latest_publish_attempt: LatestPublishAttempt | null;
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
  proposal_ids: number[];
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
  generated_at: string;
  timezone: string;
  default_time: string;
  posts_per_day: number;
  coverage: number;
  next_available_at: string;
  scheduled: Proposal[];
};

export type PlatformKind = "youtube" | "instagram" | "tiktok";

export type CapabilityState =
  | "implemented"
  | "supported_not_implemented"
  | "approval_required"
  | "unsupported"
  | "unverified";

export type CapabilityView = {
  capability: string;
  state: CapabilityState;
  ready: boolean;
  missing_scopes: string[];
  blocked_by_setting: boolean;
  detail: string;
};

export type PlatformCapabilityView = Omit<
  CapabilityView,
  "ready" | "missing_scopes" | "blocked_by_setting"
> & {
  ready?: boolean;
  missing_scopes?: string[];
  blocked_by_setting?: boolean;
};

export type AccountView = {
  id: number;
  platform: PlatformKind;
  external_account_id: string | null;
  handle: string | null;
  display_name: string | null;
  avatar_url: string | null;
  profile_url: string | null;
  account_type: string | null;
  status: string;
  granted_scopes: string[];
  scopes_source: string | null;
  connected_at: string | null;
  last_verified_at: string | null;
  last_sync_at: string | null;
  last_sync_status: string | null;
  last_error: string | null;
  freshness: "fresh" | "stale" | "never";
  capabilities: CapabilityView[];
  latest_snapshot: {
    followers: number | null;
    following: number | null;
    content_count: number | null;
    total_views: number | null;
    total_likes: number | null;
    captured_at: string;
  } | null;
};

export type PlatformEntry = {
  platform: PlatformKind;
  configured: boolean;
  required_env: string[];
  capabilities: PlatformCapabilityView[];
  accounts: AccountView[];
};

export type PlatformsResponse = {
  provider_mode: "live" | "mock";
  platforms: PlatformEntry[];
};

export type SyncRunStep = {
  step: string;
  status: string;
  items: number;
  error: string | null;
  quota_units: number;
};

export type SyncRunView = {
  id: number;
  account_id: number;
  trigger: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  steps: SyncRunStep[];
  items_fetched: number;
  quota_units_estimate: number;
  error_summary: string | null;
};

export type PlatformSummary = {
  platform: PlatformKind;
  account_id: number | null;
  handle: string | null;
  display_name: string | null;
  avatar_url: string | null;
  state: string;
  freshness: string;
  followers_label: string;
  followers: number | null;
  content_count: number | null;
  views_28d: number | null;
  last_sync_at: string | null;
  top_content: {
    id: number;
    title: string | null;
    caption?: string | null;
    content_type: string;
    view_count: number | null;
    like_count: number | null;
    comment_count: number | null;
    published_at: string | null;
    permalink: string | null;
  }[];
};

export type OverviewResponse = {
  generated_at: string;
  provider_mode: "live" | "mock";
  accounts: AccountView[];
  platform_summaries: PlatformSummary[];
  inbox: {
    unanswered_total: number;
    by_platform: Record<string, number>;
    latest: {
      id: number;
      platform: PlatformKind;
      author_name: string | null;
      text: string;
      published_at: string | null;
      status: string;
    }[];
  };
  lineup: {
    next: {
      id: number;
      final_caption: string | null;
      scheduled_publish_at: string;
      status: string;
      image_url: string | null;
    }[];
    needs_review: number;
    in_lineup: number;
    published_count: number;
  };
  drafts: {
    scheduled_next: {
      id: number;
      title: string;
      scheduled_for: string | null;
      status: string;
    }[];
    counts: Record<string, number>;
  };
  recent_sync_runs: {
    id: number;
    account_id: number;
    platform: PlatformKind;
    handle: string | null;
    status: string;
    items_fetched: number;
    error_summary: string | null;
    started_at: string | null;
  }[];
  warnings: string[];
};

export type AnalyticsKpi = {
  key: string;
  label: string;
  value: number | null;
  delta: number | null;
  source: string | null;
  as_of: string | null;
};

export type DailySeriesPoint = { day: string; value: number };
export type SnapshotSeriesPoint = { captured_at: string; value: number };

export type AccountAnalytics = {
  account: { id: number; platform: PlatformKind; handle: string | null };
  range: number;
  kpis: AnalyticsKpi[];
  series: { metric: string; label: string; points: DailySeriesPoint[] }[];
  snapshot_series: {
    metric: string;
    label: string;
    source: string;
    points: SnapshotSeriesPoint[];
  }[];
  top_content: {
    id: number;
    platform: PlatformKind;
    content_type: string;
    title: string | null;
    caption: string | null;
    thumbnail_url: string | null;
    view_count: number | null;
    like_count: number | null;
    comment_count: number | null;
    published_at: string | null;
    permalink: string | null;
  }[];
  notes: string[];
  data_sources: { label: string; url: string }[];
  latest_snapshot: {
    followers: number | null;
    content_count: number | null;
    captured_at: string;
  } | null;
};

export type InboxComment = {
  id: number;
  account_id: number;
  platform: PlatformKind;
  external_id: string;
  content_id: number | null;
  parent_external_id: string | null;
  author_name: string | null;
  author_external_id: string | null;
  author_avatar_url: string | null;
  author_is_owner: boolean;
  text: string;
  like_count: number | null;
  reply_count: number | null;
  published_at: string | null;
  status: string;
  has_owner_reply: boolean;
  permalink: string | null;
  draft: string | null;
};

export type InboxListResponse = {
  items: InboxComment[];
  total: number;
  counts: { unanswered: number; new: number };
};

export type ContentItem = {
  id: number;
  account_id: number;
  platform: PlatformKind;
  external_id: string;
  content_type: string;
  title: string | null;
  caption: string | null;
  permalink: string | null;
  thumbnail_url: string | null;
  published_at: string | null;
  duration_seconds: number | null;
  privacy_status: string | null;
  view_count: number | null;
  like_count: number | null;
  comment_count: number | null;
  share_count: number | null;
  metrics: Record<string, number | null>;
  metrics_fetched_at: string | null;
  group_id: number | null;
};

export type ContentListResponse = {
  items: ContentItem[];
  total: number;
  counts: { by_platform: Record<string, number>; by_type: Record<string, number> };
};

export type ContentDetail = ContentItem & {
  metric_history: { captured_at: string; metrics: Record<string, number> }[];
  comment_count_local: number;
  group: {
    id: number;
    title: string | null;
    members: { id: number; external_id: string; title: string | null; platform: PlatformKind }[];
  } | null;
};

export type ReplyCapability = {
  ready: boolean;
  state: string;
  detail: string;
  handoff_url: string | null;
};

export type InboxDetail = {
  comment: InboxComment;
  replies: InboxComment[];
  content: ContentItem | null;
  draft: string | null;
  reply_capability: ReplyCapability;
};

export type DraftTarget = {
  id: number;
  platform: PlatformKind;
  account_id: number | null;
  caption_override: string | null;
  status: string;
  published_content_id: number | null;
  published_url: string | null;
  published_at: string | null;
  error: string | null;
  publish_capability: ReplyCapability;
};

export type DraftView = {
  id: number;
  title: string;
  body: string;
  scheduled_for: string | null;
  status: string;
  notes: string | null;
  created_at: string;
  updated_at: string;
  targets: DraftTarget[];
};

export type ReplyActionResult = {
  id: number;
  state: string;
  verified: boolean;
  external_result_id: string | null;
  detail: string | null;
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
