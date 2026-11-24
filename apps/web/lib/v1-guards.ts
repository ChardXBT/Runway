/** Runtime validators for the `/api/v1` contract.
 *
 * The shapes are typed from the generated OpenAPI document (`lib/v1/schema.d.ts`);
 * these guards check the fields the UI relies on before it treats a response as
 * the contract, matching the `lib/guards.ts` convention for the older routes.
 */

import type { components } from "@/lib/v1/schema";

export type PostOut = components["schemas"]["PostOut"];
export type TargetOut = components["schemas"]["TargetOut"];
export type IssueOut = components["schemas"]["IssueOut"];
export type AttemptOut = components["schemas"]["AttemptOut"];
export type MediaOut = components["schemas"]["MediaOut"];
export type MediaRefOut = components["schemas"]["MediaRefOut"];
export type UploadPlanOut = components["schemas"]["UploadPlanOut"];
export type UploadStatusOut = components["schemas"]["UploadStatusOut"];
export type CapabilityOut = components["schemas"]["CapabilityOut"];
export type ReleaseOut = components["schemas"]["ReleaseOut"];
export type ApprovalItem = components["schemas"]["ApprovalItem"];
export type ApprovalPage = components["schemas"]["ApprovalPage"];
export type ApiKeyView = components["schemas"]["ApiKeyView"];
export type CreatedApiKey = components["schemas"]["CreatedApiKey"];
export type PolicyResponse = components["schemas"]["PolicyResponse"];
export type WebhookView = components["schemas"]["WebhookView"];
export type DeliveryView = components["schemas"]["DeliveryView"];
export type PostPage = components["schemas"]["CursorPage_PostOut_"];
export type AttemptPage = components["schemas"]["CursorPage_AttemptOut_"];
export type ApiKeyPage = components["schemas"]["CursorPage_ApiKeyView_"];
export type WebhookPage = components["schemas"]["CursorPage_WebhookView_"];
export type DeliveryPage = components["schemas"]["CursorPage_DeliveryView_"];
export type PostCreate = components["schemas"]["PostCreate"];
export type PostPatch = components["schemas"]["PostPatch"];
export type TargetInput = components["schemas"]["TargetInput"];
export type TargetUpdate = components["schemas"]["TargetUpdate"];
export type ReleaseRequest = components["schemas"]["ReleaseRequest"];
export type ReconcileRequest = components["schemas"]["ReconcileRequest"];
export type ScheduleRequest = components["schemas"]["ScheduleRequest"];
export type MarkPublishedRequest =
  components["schemas"]["runway__posts__schemas__MarkPublishedRequest"];
export type MediaCreate = components["schemas"]["MediaCreate"];
export type MediaComplete = components["schemas"]["MediaComplete"];
export type PartIn = components["schemas"]["PartIn"];
export type RejectRequest = components["schemas"]["RejectRequest"];
export type FieldEdit = components["schemas"]["FieldEdit"];
export type CreateKeyRequest = components["schemas"]["CreateKeyRequest"];
export type ReleaseGrantInput = components["schemas"]["ReleaseGrantInput"];
export type ReleaseGrantsRequest = components["schemas"]["ReleaseGrantsRequest"];
export type PolicyRequest = components["schemas"]["PolicyRequest"];
export type WebhookCreate = components["schemas"]["WebhookCreate"];
export type WebhookPatch = components["schemas"]["WebhookPatch"];
export type CrossPostRequest = components["schemas"]["CrossPostRequest"];
export type MediaIssueOut = components["schemas"]["MediaIssueOut"];

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isOptString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isStringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isIssue(value: unknown): value is IssueOut {
  return (
    isRecord(value) &&
    typeof value.severity === "string" &&
    typeof value.code === "string" &&
    typeof value.message === "string"
  );
}

export function isTargetOut(value: unknown): value is TargetOut {
  return (
    isRecord(value) &&
    isNumber(value.id) &&
    isNumber(value.post_id) &&
    typeof value.platform === "string" &&
    isOptString(value.post_type) &&
    (value.account_id === null || isNumber(value.account_id)) &&
    typeof value.caption === "string" &&
    isOptString(value.caption_override) &&
    typeof value.state === "string" &&
    isOptString(value.state_reason) &&
    isNumber(value.version) &&
    isOptString(value.payload_hash) &&
    isRecord(value.payload) &&
    isRecord(value.settings) &&
    isRecord(value.effective_settings) &&
    Array.isArray(value.issues) &&
    value.issues.every(isIssue) &&
    typeof value.releasable === "boolean" &&
    isOptString(value.privacy_in_words) &&
    isOptString(value.release_notice) &&
    isRecord(value.release) &&
    isOptString(value.planned_at) &&
    isOptString(value.publisher)
  );
}

export function isPostOut(value: unknown): value is PostOut {
  return (
    isRecord(value) &&
    isNumber(value.id) &&
    typeof value.title === "string" &&
    typeof value.body === "string" &&
    typeof value.status === "string" &&
    typeof value.source === "string" &&
    typeof value.ai_generated === "boolean" &&
    isNumber(value.version) &&
    Array.isArray(value.targets) &&
    value.targets.every(isTargetOut) &&
    Array.isArray(value.media) &&
    value.media.every(
      (item) => isRecord(item) && isNumber(item.asset_id) && isNumber(item.position),
    )
  );
}

export function isPostPage(value: unknown): value is PostPage {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isPostOut) &&
    isOptString(value.next_cursor)
  );
}

export function isAttemptOut(value: unknown): value is AttemptOut {
  return (
    isRecord(value) &&
    isNumber(value.id) &&
    isNumber(value.target_id) &&
    typeof value.adapter === "string" &&
    typeof value.kind === "string" &&
    typeof value.outcome === "string" &&
    typeof value.started_at === "string" &&
    isOptString(value.finished_at) &&
    isOptString(value.error_code) &&
    isOptString(value.error_detail) &&
    isRecord(value.response)
  );
}

export function isAttemptPage(value: unknown): value is AttemptPage {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isAttemptOut) &&
    isOptString(value.next_cursor)
  );
}

export function isApprovalPage(value: unknown): value is ApprovalPage {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(
      (item) =>
        isRecord(item) &&
        isNumber(item.target_id) &&
        isNumber(item.post_id) &&
        typeof item.platform === "string" &&
        typeof item.title === "string",
    ) &&
    isOptString(value.next_cursor)
  );
}

export function isApiKeyPage(value: unknown): value is ApiKeyPage {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(
      (item) =>
        isRecord(item) &&
        isNumber(item.id) &&
        typeof item.name === "string" &&
        typeof item.state === "string" &&
        isStringList(item.scopes) &&
        isStringList(item.effective_scopes) &&
        Array.isArray(item.release_grants),
    ) &&
    isOptString(value.next_cursor)
  );
}

export function isCreatedApiKey(value: unknown): value is CreatedApiKey {
  return (
    isRecord(value) &&
    typeof value.secret === "string" &&
    isRecord(value.key) &&
    isNumber((value.key as Record<string, unknown>).id)
  );
}

export function isPolicyResponse(value: unknown): value is PolicyResponse {
  return (
    isRecord(value) &&
    (value.agent_release === "never" || value.agent_release === "per_key") &&
    isStringList(value.default_agent_scopes) &&
    Array.isArray(value.human_only_platforms)
  );
}

export function isWebhookPage(value: unknown): value is WebhookPage {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(
      (item) =>
        isRecord(item) &&
        isNumber(item.id) &&
        typeof item.url === "string" &&
        typeof item.state === "string" &&
        isStringList(item.event_types),
    ) &&
    isOptString(value.next_cursor)
  );
}

export function isDeliveryPage(value: unknown): value is DeliveryPage {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(
      (item) =>
        isRecord(item) &&
        isNumber(item.id) &&
        isNumber(item.event_id) &&
        typeof item.event_type === "string" &&
        typeof item.state === "string",
    ) &&
    isOptString(value.next_cursor)
  );
}

export function isMediaOut(value: unknown): value is MediaOut {
  return (
    isRecord(value) &&
    isNumber(value.id) &&
    typeof value.status === "string" &&
    typeof value.content_type === "string" &&
    isNumber(value.size_bytes) &&
    typeof value.sha256 === "string" &&
    isRecord(value.probe) &&
    isRecord(value.validation) &&
    (value.upload === undefined ||
      value.upload === null ||
      (isRecord(value.upload) &&
        (value.upload.mode === "api" || value.upload.mode === "direct") &&
        isNumber(value.upload.part_count) &&
        isNumber(value.upload.part_size)))
  );
}

export function isUploadStatus(value: unknown): value is UploadStatusOut {
  return (
    isRecord(value) &&
    isNumber(value.asset_id) &&
    isNumber(value.bytes_received) &&
    Array.isArray(value.missing_parts) &&
    value.missing_parts.every(isNumber) &&
    Array.isArray(value.received_parts) &&
    value.received_parts.every(isNumber)
  );
}

/** Per-scope freshness row from `GET /accounts/{id}/sync-status`. */
export interface SyncScopeStatus {
  scope: string;
  last_run_at: string | null;
  last_status: string | null;
  last_error: string | null;
  next_run_at: string | null;
  boosted_until: string | null;
  source: string | null;
}

export interface SyncStatusView {
  push_enabled: boolean;
  quota: Record<string, unknown>;
  adaptive_sync_enabled: boolean;
  scopes: SyncScopeStatus[];
}

export function isSyncStatus(value: unknown): value is SyncStatusView {
  return (
    isRecord(value) &&
    typeof value.push_enabled === "boolean" &&
    isRecord(value.quota) &&
    Array.isArray(value.scopes) &&
    value.scopes.every(
      (scope) =>
        isRecord(scope) &&
        typeof scope.scope === "string" &&
        isOptString(scope.last_run_at) &&
        isOptString(scope.last_status) &&
        isOptString(scope.last_error) &&
        isOptString(scope.next_run_at) &&
        isOptString(scope.source),
    )
  );
}

/** Fresh `creator_info` for the TikTok mandatory posting screen. */
export interface TikTokCreatorInfo {
  account_id: number;
  creator_nickname: string | null;
  creator_username: string | null;
  creator_avatar_url: string | null;
  privacy_level_options: string[];
  comment_disabled: boolean;
  duet_disabled: boolean;
  stitch_disabled: boolean;
  max_video_post_duration_sec: number | null;
  consent: { default: string | null; branded: string | null };
}

export function isTikTokCreatorInfo(value: unknown): value is TikTokCreatorInfo {
  return (
    isRecord(value) &&
    isNumber(value.account_id) &&
    isStringList(value.privacy_level_options) &&
    typeof value.comment_disabled === "boolean" &&
    typeof value.duet_disabled === "boolean" &&
    typeof value.stitch_disabled === "boolean" &&
    isRecord(value.consent)
  );
}

export interface YoutubeCategories {
  items: { id: string; title: string }[];
  region: string;
}

export function isYoutubeCategories(value: unknown): value is YoutubeCategories {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(
      (item) => isRecord(item) && typeof item.id === "string" && typeof item.title === "string",
    ) &&
    typeof value.region === "string"
  );
}

/** `POST /accounts/{id}/publishing/enable` answer. */
export interface EnablePublishingResult {
  status?: string;
  authorization_url?: string;
  state?: string;
  scopes?: string[];
  requested_scopes?: string[];
}

export function isEnablePublishingResult(
  value: unknown,
): value is EnablePublishingResult {
  return (
    isRecord(value) &&
    (value.authorization_url === undefined ||
      typeof value.authorization_url === "string") &&
    (value.status === undefined || typeof value.status === "string")
  );
}

/** Per-post-type capability for one account (`/publishing/capability`). */
export interface AccountCapabilities {
  account_id: number;
  platform: string;
  items: CapabilityOut[];
}

export function isAccountCapabilities(
  value: unknown,
): value is AccountCapabilities {
  return (
    isRecord(value) &&
    isNumber(value.account_id) &&
    typeof value.platform === "string" &&
    Array.isArray(value.items) &&
    value.items.every(
      (item) =>
        isRecord(item) &&
        typeof item.post_type === "string" &&
        typeof item.state === "string" &&
        typeof item.detail === "string" &&
        isStringList(item.missing_scopes),
    )
  );
}
