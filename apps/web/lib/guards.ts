import type {
  AccountAnalytics,
  AccountView,
  AssistedPublishingWorkspace,
  CapabilityView,
  ContentDetail,
  ContentItem,
  ContentListResponse,
  DraftView,
  EditorialEnvelope,
  GenerationActivity,
  InboxComment,
  InboxDetail,
  InboxListResponse,
  LineupSchedule,
  LineupPushResponse,
  OverviewResponse,
  PlatformsResponse,
  Proposal,
  PublisherConnectionStatus,
  PublisherQueueStatus,
  ReplyActionResult,
  SyncRunView,
  WorkflowStatus,
} from "@/lib/types";
import { isValidTimeZone, localDate } from "@/lib/datetime";

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isDateString(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.trim().length > 0 &&
    !Number.isNaN(new Date(value).getTime())
  );
}

const proposalStatuses = new Set([
  "generating",
  "needs_review",
  "approved",
  "rejected",
  "internally_scheduled",
  "publishing",
  "externally_scheduled",
  "publish_unverified",
  "publish_failed",
  "published",
  "cancelled",
]);

function isLatestPublishAttempt(value: unknown) {
  return (
    value === null ||
    (isRecord(value) &&
      isFiniteNumber(value.id) &&
      Number.isInteger(value.id) &&
      typeof value.status === "string" &&
      (value.error_summary === null || typeof value.error_summary === "string") &&
      isDateString(value.prepared_at) &&
      (value.submitted_at === null || isDateString(value.submitted_at)) &&
      (value.completed_at === null || isDateString(value.completed_at)))
  );
}

export function isProposal(value: unknown): value is Proposal {
  if (!isRecord(value)) return false;
  const candidateValid =
    value.candidate === null ||
    (isRecord(value.candidate) &&
      (value.candidate.original_url === null ||
        typeof value.candidate.original_url === "string") &&
      (value.candidate.preview_url === null ||
        typeof value.candidate.preview_url === "string") &&
      isRecord(value.candidate.detected_topic) &&
      (value.candidate.detected_topic.franchise === undefined ||
        value.candidate.detected_topic.franchise === null ||
        typeof value.candidate.detected_topic.franchise === "string") &&
      (value.candidate.detected_topic.characters === undefined ||
        (Array.isArray(value.candidate.detected_topic.characters) &&
          value.candidate.detected_topic.characters.every(
            (item) => typeof item === "string",
          ))));
  return (
    isFiniteNumber(value.id) &&
    isFiniteNumber(value.generation_run_id) &&
    isDateString(value.planned_publish_at) &&
    (value.scheduled_publish_at === null ||
      isDateString(value.scheduled_publish_at)) &&
    typeof value.status === "string" &&
    proposalStatuses.has(value.status) &&
    isFiniteNumber(value.candidate_image_id) &&
    Array.isArray(value.backup_candidate_ids) &&
    value.backup_candidate_ids.every(
      (item) => isFiniteNumber(item) && Number.isInteger(item),
    ) &&
    typeof value.final_caption === "string" &&
    typeof value.recommended_caption === "string" &&
    typeof value.caption_rationale === "string" &&
    typeof value.selection_reason === "string" &&
    (value.caption_confidence === null ||
      isFiniteNumber(value.caption_confidence)) &&
    (value.rejected_at === undefined ||
      value.rejected_at === null ||
      isDateString(value.rejected_at)) &&
    Array.isArray(value.alternative_captions) &&
    value.alternative_captions.every((item) => typeof item === "string") &&
    Array.isArray(value.warnings) &&
    value.warnings.every((item) => typeof item === "string") &&
    (value.external_post_id === null || typeof value.external_post_id === "string") &&
    (value.external_post_url === null || typeof value.external_post_url === "string") &&
    (value.scheduled_verified_at === null ||
      isDateString(value.scheduled_verified_at)) &&
    isLatestPublishAttempt(value.latest_publish_attempt) &&
    candidateValid
  );
}

export function isWorkflowStatus(value: unknown): value is WorkflowStatus {
  if (!isRecord(value)) return false;
  return (
    isFiniteNumber(value.needs_review) &&
    isFiniteNumber(value.queued) &&
    isFiniteNumber(value.scheduled) &&
    isFiniteNumber(value.rejected) &&
    isDateString(value.next_available_at) &&
    isFiniteNumber(value.posts_per_day) &&
    typeof value.timezone === "string" &&
    isValidTimeZone(value.timezone)
  );
}

export function isPublisherQueueStatus(
  value: unknown,
): value is PublisherQueueStatus {
  if (!isRecord(value)) return false;
  return (
    typeof value.running === "boolean" &&
    isFiniteNumber(value.queued) &&
    Number.isInteger(value.queued) &&
    value.queued >= 0 &&
    typeof value.paused === "boolean" &&
    !(value.running && value.paused) &&
    (value.paused_reason === null || typeof value.paused_reason === "string") &&
    Array.isArray(value.proposal_ids) &&
    value.proposal_ids.every(
      (proposalId) => isFiniteNumber(proposalId) && Number.isInteger(proposalId),
    )
  );
}

export function isAssistedPublishingWorkspace(
  value: unknown,
): value is AssistedPublishingWorkspace {
  if (!isRecord(value) || !Array.isArray(value.items)) return false;
  return (
    value.mode === "assisted" &&
    typeof value.channel_name === "string" &&
    typeof value.channel_id === "string" &&
    typeof value.timezone === "string" &&
    isValidTimeZone(value.timezone) &&
    typeof value.youtube_url === "string" &&
    value.youtube_url.startsWith("https://www.youtube.com/") &&
    value.items.every(
      (item) =>
        isRecord(item) &&
        isFiniteNumber(item.proposal_id) &&
        Number.isInteger(item.proposal_id) &&
        isDateString(item.planned_publish_at) &&
        typeof item.caption === "string" &&
        typeof item.image_url === "string" &&
        item.image_url.startsWith("/media/") &&
        typeof item.rights_status === "string" &&
        Array.isArray(item.warnings) &&
        item.warnings.every((warning) => typeof warning === "string"),
    )
  );
}

export function isLineupPushResponse(value: unknown): value is LineupPushResponse {
  if (!isRecord(value)) return false;
  const assistedValid =
    value.assisted_workspace === undefined ||
    isAssistedPublishingWorkspace(value.assisted_workspace);
  return (
    (value.mode === "assisted" || value.mode === "authorized_browser") &&
    typeof value.detail === "string" &&
    Array.isArray(value.queued_proposal_ids) &&
    value.queued_proposal_ids.every(
      (proposalId) => typeof proposalId === "number" && Number.isInteger(proposalId),
    ) &&
    assistedValid &&
    (value.mode !== "assisted" || isAssistedPublishingWorkspace(value.assisted_workspace)) &&
    isLineupSchedule(value.lineup) &&
    isPublisherQueueStatus(value.publisher_queue)
  );
}

export function isPublisherConnectionStatus(
  value: unknown,
): value is PublisherConnectionStatus {
  if (!isRecord(value) || !isRecord(value.checks)) return false;
  const states = new Set([
    "disabled",
    "unchecked",
    "connected",
    "stale",
    "needs_attention",
  ]);
  return (
    typeof value.state === "string" &&
    states.has(value.state) &&
    (value.valid === null || typeof value.valid === "boolean") &&
    typeof value.detail === "string" &&
    typeof value.publisher === "string" &&
    (value.checked_at === null || isDateString(value.checked_at)) &&
    typeof value.stale === "boolean" &&
    isFiniteNumber(value.stale_after_hours) &&
    value.stale_after_hours > 0 &&
    Object.values(value.checks).every((passed) => typeof passed === "boolean") &&
    (value.last_verified_publish_at === null ||
      isDateString(value.last_verified_publish_at))
  );
}

export function isGenerationActivity(
  value: unknown,
): value is GenerationActivity {
  if (!isRecord(value)) return false;
  return (
    typeof value.running === "boolean" &&
    (value.started_at === null || isDateString(value.started_at)) &&
    (value.completed_at === null || isDateString(value.completed_at)) &&
    (value.detail === null || typeof value.detail === "string")
  );
}

export function isEditorialEnvelope(value: unknown): value is EditorialEnvelope {
  if (!isRecord(value)) return false;
  return (
    (value.next_proposal === null || isProposal(value.next_proposal)) &&
    isWorkflowStatus(value.workflow) &&
    (value.proposal === undefined || isProposal(value.proposal)) &&
    (value.publisher_queue === undefined ||
      isPublisherQueueStatus(value.publisher_queue)) &&
    (value.generation === undefined ||
      isGenerationActivity(value.generation))
  );
}

export function isLineupSchedule(value: unknown): value is LineupSchedule {
  if (!isRecord(value)) return false;
  return (
    isDateString(value.generated_at) &&
    typeof value.timezone === "string" &&
    isValidTimeZone(value.timezone) &&
    typeof value.default_time === "string" &&
    /^([01]\d|2[0-3]):[0-5]\d$/.test(value.default_time) &&
    isFiniteNumber(value.posts_per_day) &&
    value.posts_per_day === 1 &&
    isFiniteNumber(value.coverage) &&
    isDateString(value.next_available_at) &&
    Array.isArray(value.scheduled) &&
    value.scheduled.every(isProposal) &&
    value.coverage === value.scheduled.length
  );
}

export function proposalList(value: unknown): value is Proposal[] {
  return Array.isArray(value) && value.every(isProposal);
}

const platformKinds = new Set(["youtube", "instagram", "tiktok"]);

function isCapabilityView(value: unknown): value is CapabilityView {
  return (
    isRecord(value) &&
    typeof value.capability === "string" &&
    typeof value.state === "string" &&
    typeof value.ready === "boolean" &&
    Array.isArray(value.missing_scopes) &&
    typeof value.blocked_by_setting === "boolean" &&
    typeof value.detail === "string"
  );
}

export function isAccountView(value: unknown): value is AccountView {
  return (
    isRecord(value) &&
    isFiniteNumber(value.id) &&
    typeof value.platform === "string" &&
    platformKinds.has(value.platform) &&
    typeof value.status === "string" &&
    (value.handle === null || typeof value.handle === "string") &&
    (value.display_name === null || typeof value.display_name === "string") &&
    Array.isArray(value.granted_scopes) &&
    value.granted_scopes.every((scope) => typeof scope === "string") &&
    (value.connected_at === null || typeof value.connected_at === "string") &&
    (value.last_sync_at === null || typeof value.last_sync_at === "string") &&
    (value.last_verified_at === null ||
      typeof value.last_verified_at === "string") &&
    Array.isArray(value.capabilities) &&
    value.capabilities.every(isCapabilityView) &&
    typeof value.freshness === "string"
  );
}

export function isPlatformsResponse(
  value: unknown,
): value is PlatformsResponse {
  return (
    isRecord(value) &&
    (value.provider_mode === "live" || value.provider_mode === "mock") &&
    Array.isArray(value.platforms) &&
    value.platforms.every(
      (entry) =>
        isRecord(entry) &&
        typeof entry.platform === "string" &&
        platformKinds.has(entry.platform) &&
        typeof entry.configured === "boolean" &&
        Array.isArray(entry.required_env) &&
        entry.required_env.every((name) => typeof name === "string") &&
        Array.isArray(entry.capabilities) &&
        entry.capabilities.every(
          (capability) =>
            isRecord(capability) &&
            typeof capability.capability === "string" &&
            typeof capability.state === "string" &&
            typeof capability.detail === "string",
        ) &&
        Array.isArray(entry.accounts) &&
        entry.accounts.every(isAccountView),
    )
  );
}

export function isSyncRunView(value: unknown): value is SyncRunView {
  return (
    isRecord(value) &&
    isFiniteNumber(value.id) &&
    isFiniteNumber(value.account_id) &&
    typeof value.status === "string" &&
    Array.isArray(value.steps) &&
    value.steps.every(
      (step) =>
        isRecord(step) &&
        typeof step.step === "string" &&
        typeof step.status === "string" &&
        isFiniteNumber(step.items) &&
        (step.error === null || typeof step.error === "string"),
    ) &&
    isFiniteNumber(value.items_fetched) &&
    (value.error_summary === null || typeof value.error_summary === "string")
  );
}

export function isOverviewResponse(
  value: unknown,
): value is OverviewResponse {
  return (
    isRecord(value) &&
    isDateString(value.generated_at) &&
    (value.provider_mode === "live" || value.provider_mode === "mock") &&
    Array.isArray(value.accounts) &&
    value.accounts.every(isAccountView) &&
    Array.isArray(value.platform_summaries) &&
    value.platform_summaries.every(
      (entry) =>
        isRecord(entry) &&
        typeof entry.platform === "string" &&
        platformKinds.has(entry.platform) &&
        typeof entry.state === "string" &&
        typeof entry.followers_label === "string",
    ) &&
    isRecord(value.inbox) &&
    isFiniteNumber(value.inbox.unanswered_total) &&
    isRecord(value.inbox.by_platform) &&
    Array.isArray(value.inbox.latest) &&
    isRecord(value.lineup) &&
    Array.isArray(value.lineup.next) &&
    isFiniteNumber(value.lineup.needs_review) &&
    isFiniteNumber(value.lineup.in_lineup) &&
    isRecord(value.drafts) &&
    Array.isArray(value.drafts.scheduled_next) &&
    isRecord(value.drafts.counts) &&
    Array.isArray(value.recent_sync_runs) &&
    Array.isArray(value.warnings) &&
    value.warnings.every((warning) => typeof warning === "string")
  );
}

export function isAccountAnalytics(
  value: unknown,
): value is AccountAnalytics {
  return (
    isRecord(value) &&
    isRecord(value.account) &&
    isFiniteNumber(value.account.id) &&
    typeof value.account.platform === "string" &&
    platformKinds.has(value.account.platform) &&
    isFiniteNumber(value.range) &&
    Array.isArray(value.kpis) &&
    Array.isArray(value.series) &&
    Array.isArray(value.snapshot_series) &&
    Array.isArray(value.top_content) &&
    Array.isArray(value.notes) &&
    Array.isArray(value.data_sources)
  );
}

function isInboxComment(value: unknown): value is InboxComment {
  return (
    isRecord(value) &&
    isFiniteNumber(value.id) &&
    isFiniteNumber(value.account_id) &&
    typeof value.platform === "string" &&
    platformKinds.has(value.platform) &&
    typeof value.text === "string" &&
    typeof value.status === "string" &&
    typeof value.has_owner_reply === "boolean"
  );
}

export function isInboxListResponse(
  value: unknown,
): value is InboxListResponse {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isInboxComment) &&
    isFiniteNumber(value.total) &&
    isRecord(value.counts) &&
    isFiniteNumber(value.counts.unanswered)
  );
}

export function isInboxDetail(value: unknown): value is InboxDetail {
  return (
    isRecord(value) &&
    isInboxComment(value.comment) &&
    Array.isArray(value.replies) &&
    value.replies.every(isInboxComment) &&
    isRecord(value.reply_capability) &&
    typeof value.reply_capability.ready === "boolean" &&
    typeof value.reply_capability.detail === "string"
  );
}

function isContentItem(value: unknown): value is ContentItem {
  return (
    isRecord(value) &&
    isFiniteNumber(value.id) &&
    typeof value.platform === "string" &&
    platformKinds.has(value.platform) &&
    typeof value.content_type === "string"
  );
}

export function isContentListResponse(
  value: unknown,
): value is ContentListResponse {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isContentItem) &&
    isFiniteNumber(value.total) &&
    isRecord(value.counts) &&
    isRecord(value.counts.by_platform) &&
    isRecord(value.counts.by_type)
  );
}

export function isContentDetail(value: unknown): value is ContentDetail {
  if (!isContentItem(value)) return false;
  const record = value as Record<string, unknown>;
  return (
    Array.isArray(record.metric_history) &&
    isFiniteNumber(record.comment_count_local)
  );
}

function isDraftView(value: unknown): value is DraftView {
  return (
    isRecord(value) &&
    isFiniteNumber(value.id) &&
    typeof value.title === "string" &&
    typeof value.status === "string" &&
    Array.isArray(value.targets) &&
    value.targets.every(
      (target) =>
        isRecord(target) &&
        isFiniteNumber(target.id) &&
        typeof target.platform === "string" &&
        platformKinds.has(target.platform) &&
        typeof target.status === "string" &&
        isRecord(target.publish_capability) &&
        typeof target.publish_capability.state === "string",
    )
  );
}

export function isDraftList(value: unknown): value is DraftView[] {
  return Array.isArray(value) && value.every(isDraftView);
}

export function isReplyActionResult(
  value: unknown,
): value is ReplyActionResult {
  return (
    isRecord(value) &&
    typeof value.state === "string" &&
    typeof value.verified === "boolean"
  );
}

export function conflictingLineupDates(lineup: LineupSchedule) {
  const counts = new Map<string, number>();
  for (const proposal of lineup.scheduled) {
    const slot = proposal.scheduled_publish_at ?? proposal.planned_publish_at;
    const date = localDate(slot, lineup.timezone);
    if (!date) return ["invalid-date"];
    counts.set(date, (counts.get(date) ?? 0) + 1);
  }
  return [...counts.entries()]
    .filter(([, count]) => count > 1)
    .map(([date]) => date);
}
