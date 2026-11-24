/** Typed client for the `/api/v1` contract.
 *
 * Types come from the generated OpenAPI document (`lib/v1/schema.d.ts`, see
 * `npm run gen:api`). Mutations that accept a version send `If-Match` so a
 * stale edit never silently overwrites a concurrent change; create calls send
 * an `Idempotency-Key` so a retried submit never double-creates.
 */

import { API_URL } from "@/lib/api";
import { ApiError } from "@/lib/client-api";
import {
  isAccountCapabilities,
  isApiKeyPage,
  isApprovalPage,
  isAttemptPage,
  isCreatedApiKey,
  isDeliveryPage,
  isEnablePublishingResult,
  isMediaOut,
  isPolicyResponse,
  isPostOut,
  isPostPage,
  isSyncStatus,
  isTikTokCreatorInfo,
  isUploadStatus,
  isWebhookPage,
  isYoutubeCategories,
  type AccountCapabilities,
  type ApiKeyPage,
  type ApiKeyView,
  type ApprovalPage,
  type AttemptPage,
  type CreateKeyRequest,
  type CreatedApiKey,
  type CrossPostRequest,
  type DeliveryPage,
  type EnablePublishingResult,
  type MarkPublishedRequest,
  type MediaComplete,
  type MediaCreate,
  type MediaOut,
  type PolicyRequest,
  type PolicyResponse,
  type PostCreate,
  type PostOut,
  type PostPage,
  type PostPatch,
  type ReconcileRequest,
  type RejectRequest,
  type ReleaseGrantsRequest,
  type ReleaseRequest,
  type ScheduleRequest,
  type SyncStatusView,
  type TargetInput,
  type TargetOut,
  type TargetUpdate,
  type TikTokCreatorInfo,
  type UploadStatusOut,
  type WebhookCreate,
  type WebhookPage,
  type WebhookPatch,
  type WebhookView,
  type YoutubeCategories,
} from "@/lib/v1-guards";

export class V1ApiError extends ApiError {
  readonly status: number | null;
  /** Stable problem code (`release_requires_human`, `conflict`, …). */
  readonly code: string | null;
  readonly problem: Record<string, unknown> | null;

  constructor(
    message: string,
    {
      status = null,
      code = null,
      problem = null,
      uncertainOutcome = false,
    }: {
      status?: number | null;
      code?: string | null;
      problem?: Record<string, unknown> | null;
      uncertainOutcome?: boolean;
    } = {},
  ) {
    super(message, { uncertainOutcome });
    this.name = "V1ApiError";
    this.status = status;
    this.code = code;
    this.problem = problem;
  }
}

type Validator<T> = (value: unknown) => value is T;

type V1RequestOptions = {
  method?: string;
  body?: unknown;
  rawBody?: BodyInit | null;
  contentType?: string;
  /** Target/post `version`; sent as `If-Match: "<version>"`. */
  ifMatch?: number | string;
  /** Reuse one key across retries of the same user action. */
  idempotencyKey?: string;
  signal?: AbortSignal;
  timeoutMs?: number;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const MALFORMED =
  "Runway received an unexpected response. Nothing has been confirmed; refresh before retrying.";

async function v1Request<T>(
  path: string,
  validate: Validator<T>,
  options: V1RequestOptions = {},
): Promise<T> {
  const {
    method = "GET",
    body,
    rawBody,
    contentType,
    ifMatch,
    idempotencyKey,
    signal,
    timeoutMs = 30_000,
  } = options;
  const headers = new Headers();
  if (body !== undefined) headers.set("Content-Type", "application/json");
  if (contentType !== undefined) headers.set("Content-Type", contentType);
  if (ifMatch !== undefined) {
    headers.set("If-Match", typeof ifMatch === "number" ? `"${ifMatch}"` : ifMatch);
  }
  if (idempotencyKey !== undefined) headers.set("Idempotency-Key", idempotencyKey);
  const signals = [AbortSignal.timeout(timeoutMs)];
  if (signal) signals.push(signal);

  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      method,
      headers,
      body: rawBody ?? (body === undefined ? undefined : JSON.stringify(body)),
      cache: "no-store",
      signal: AbortSignal.any(signals),
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new V1ApiError(
      "Runway could not reach the local service. Check that the API is running, then try again.",
      { uncertainOutcome: true },
    );
  }

  const uncertain = response.status === 408 || response.status >= 500;
  let payload: unknown = null;
  let hasPayload = false;
  try {
    payload = await response.json();
    hasPayload = true;
  } catch {
    // Empty bodies (204, binary routes) stay unparsed.
  }

  if (!response.ok) {
    const problem = isRecord(payload) && typeof payload.code === "string" ? payload : null;
    const detail =
      isRecord(payload) && typeof payload.detail === "string" && payload.detail.trim()
        ? payload.detail.trim().slice(0, 500)
        : `The local service returned ${response.status}.`;
    throw new V1ApiError(detail, {
      status: response.status,
      code: problem ? (problem.code as string) : null,
      problem,
      uncertainOutcome: uncertain,
    });
  }
  if (!hasPayload) {
    if (!validate(undefined)) throw new V1ApiError(MALFORMED, { uncertainOutcome: true });
    return undefined as T;
  }
  if (!validate(payload)) {
    throw new V1ApiError(MALFORMED, { uncertainOutcome: true });
  }
  return payload;
}

export function newIdempotencyKey(): string {
  return typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `web-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

const always = (): boolean => true;
const isAnyRecord = (value: unknown): value is Record<string, unknown> =>
  isRecord(value);
const isKeyView = (value: unknown): value is ApiKeyView =>
  isRecord(value) && typeof value.id === "number";
const isWebhookView = (value: unknown): value is WebhookView =>
  isRecord(value) && typeof value.id === "number" && typeof value.url === "string";
const isCreatedWebhook = (
  value: unknown,
): value is WebhookView & { secret: string } =>
  isRecord(value) &&
  typeof value.id === "number" &&
  typeof value.secret === "string";

// ---------------------------------------------------------------------------
// Posts

export const V1 = {
  posts: {
    list(cursor?: string | null): Promise<PostPage> {
      const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
      return v1Request(`/api/v1/posts${query}`, isPostPage);
    },
    create(input: PostCreate, idempotencyKey = newIdempotencyKey()): Promise<PostOut> {
      return v1Request("/api/v1/posts", isPostOut, {
        method: "POST",
        body: input,
        idempotencyKey,
      });
    },
    get(postId: number): Promise<PostOut> {
      return v1Request(`/api/v1/posts/${postId}`, isPostOut);
    },
    patch(postId: number, patch: PostPatch, version: number): Promise<PostOut> {
      return v1Request(`/api/v1/posts/${postId}`, isPostOut, {
        method: "PATCH",
        body: patch,
        ifMatch: version,
      });
    },
    addTarget(postId: number, target: TargetInput): Promise<PostOut> {
      return v1Request(`/api/v1/posts/${postId}/targets`, isPostOut, {
        method: "POST",
        body: target,
      });
    },
    updateTarget(
      postId: number,
      targetId: number,
      update: TargetUpdate,
      version: number,
    ): Promise<PostOut> {
      return v1Request(`/api/v1/posts/${postId}/targets/${targetId}`, isPostOut, {
        method: "PUT",
        body: update,
        ifMatch: version,
      });
    },
    scheduleTarget(
      postId: number,
      targetId: number,
      request: ScheduleRequest,
    ): Promise<PostOut> {
      return v1Request(
        `/api/v1/posts/${postId}/targets/${targetId}/schedule`,
        isPostOut,
        { method: "POST", body: request },
      );
    },
    cancelTarget(postId: number, targetId: number): Promise<PostOut> {
      return v1Request(
        `/api/v1/posts/${postId}/targets/${targetId}/cancel`,
        isPostOut,
        { method: "POST" },
      );
    },
    releaseTarget(
      postId: number,
      targetId: number,
      request: ReleaseRequest,
    ): Promise<PostOut> {
      return v1Request(
        `/api/v1/posts/${postId}/targets/${targetId}/release`,
        isPostOut,
        { method: "POST", body: request },
      );
    },
    reconcileTarget(
      postId: number,
      targetId: number,
      request: ReconcileRequest,
    ): Promise<PostOut> {
      return v1Request(
        `/api/v1/posts/${postId}/targets/${targetId}/reconcile`,
        isPostOut,
        { method: "POST", body: request },
      );
    },
    markPublished(
      postId: number,
      targetId: number,
      request: MarkPublishedRequest,
    ): Promise<PostOut> {
      return v1Request(
        `/api/v1/posts/${postId}/targets/${targetId}/mark-published`,
        isPostOut,
        { method: "POST", body: request },
      );
    },
    listAttempts(
      postId: number,
      targetId: number,
      cursor?: string | null,
    ): Promise<AttemptPage> {
      const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
      return v1Request(
        `/api/v1/posts/${postId}/targets/${targetId}/attempts${query}`,
        isAttemptPage,
      );
    },
  },

  // -------------------------------------------------------------------------
  // Media (chunked, resumable upload)

  media: {
    create(input: MediaCreate, idempotencyKey = newIdempotencyKey()): Promise<MediaOut> {
      return v1Request("/api/v1/media", isMediaOut, {
        method: "POST",
        body: input,
        idempotencyKey,
      });
    },
    get(assetId: number): Promise<MediaOut> {
      return v1Request(`/api/v1/media/${assetId}`, isMediaOut);
    },
    uploadStatus(assetId: number): Promise<UploadStatusOut> {
      return v1Request(`/api/v1/media/${assetId}/upload`, isUploadStatus);
    },
    putPart(assetId: number, partNumber: number, bytes: BodyInit): Promise<UploadStatusOut> {
      return v1Request(`/api/v1/media/${assetId}/parts/${partNumber}`, isUploadStatus, {
        method: "PUT",
        rawBody: bytes,
        contentType: "application/octet-stream",
        timeoutMs: 300_000,
      });
    },
    complete(assetId: number, request: MediaComplete): Promise<MediaOut> {
      return v1Request(`/api/v1/media/${assetId}/complete`, isMediaOut, {
        method: "POST",
        body: request,
      });
    },
    posterUrl(assetId: number): string {
      return `${API_URL}/api/v1/media/${assetId}/poster`;
    },
    remove(assetId: number): Promise<void> {
      return v1Request(`/api/v1/media/${assetId}`, always as Validator<void>, {
        method: "DELETE",
      });
    },
  },

  // -------------------------------------------------------------------------
  // Accounts

  accounts: {
    syncStatus(accountId: number): Promise<SyncStatusView> {
      return v1Request(`/api/v1/accounts/${accountId}/sync-status`, isSyncStatus);
    },
    youtubeCategories(accountId: number, region = "US"): Promise<YoutubeCategories> {
      return v1Request(
        `/api/v1/accounts/${accountId}/youtube/categories?region=${encodeURIComponent(region)}`,
        isYoutubeCategories,
      );
    },
    tiktokCreatorInfo(accountId: number): Promise<TikTokCreatorInfo> {
      return v1Request(
        `/api/v1/accounts/${accountId}/tiktok/creator-info`,
        isTikTokCreatorInfo,
      );
    },
    enablePublishing(accountId: number): Promise<EnablePublishingResult> {
      return v1Request(
        `/api/v1/accounts/${accountId}/publishing/enable`,
        isEnablePublishingResult,
        { method: "POST" },
      );
    },
    publishingCapabilities(accountId: number): Promise<AccountCapabilities> {
      return v1Request(
        `/api/v1/accounts/${accountId}/publishing/capability`,
        isAccountCapabilities,
      );
    },
  },

  // -------------------------------------------------------------------------
  // Approvals, keys, webhooks (workspace:admin, human only)

  approvals: {
    list(cursor?: string | null): Promise<ApprovalPage> {
      const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
      return v1Request(`/api/v1/approvals${query}`, isApprovalPage);
    },
    reject(targetId: number, request: RejectRequest): Promise<Record<string, unknown>> {
      return v1Request(`/api/v1/approvals/${targetId}/reject`, isAnyRecord, {
        method: "POST",
        body: request,
      });
    },
  },

  keys: {
    list(cursor?: string | null): Promise<ApiKeyPage> {
      const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
      return v1Request(`/api/v1/keys${query}`, isApiKeyPage);
    },
    create(
      request: CreateKeyRequest,
      idempotencyKey = newIdempotencyKey(),
    ): Promise<CreatedApiKey> {
      return v1Request("/api/v1/keys", isCreatedApiKey, {
        method: "POST",
        body: request,
        idempotencyKey,
      });
    },
    revoke(keyId: number): Promise<ApiKeyView> {
      return v1Request(`/api/v1/keys/${keyId}`, isKeyView, { method: "DELETE" });
    },
    setReleaseGrants(keyId: number, request: ReleaseGrantsRequest): Promise<ApiKeyView> {
      return v1Request(`/api/v1/keys/${keyId}/release-grants`, isKeyView, {
        method: "PUT",
        body: request,
      });
    },
    policy(): Promise<PolicyResponse> {
      return v1Request("/api/v1/policy", isPolicyResponse);
    },
    setPolicy(request: PolicyRequest): Promise<PolicyResponse> {
      return v1Request("/api/v1/policy", isPolicyResponse, {
        method: "PUT",
        body: request,
      });
    },
  },

  webhooks: {
    list(cursor?: string | null): Promise<WebhookPage> {
      const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
      return v1Request(`/api/v1/webhooks${query}`, isWebhookPage);
    },
    create(
      request: WebhookCreate,
      idempotencyKey = newIdempotencyKey(),
    ): Promise<WebhookView & { secret: string }> {
      return v1Request("/api/v1/webhooks", isCreatedWebhook, {
        method: "POST",
        body: request,
        idempotencyKey,
      });
    },
    update(endpointId: number, patch: WebhookPatch): Promise<WebhookView> {
      return v1Request(`/api/v1/webhooks/${endpointId}`, isWebhookView, {
        method: "PATCH",
        body: patch,
      });
    },
    remove(endpointId: number): Promise<void> {
      return v1Request(`/api/v1/webhooks/${endpointId}`, always as Validator<void>, {
        method: "DELETE",
      });
    },
    deliveries(endpointId: number, cursor?: string | null): Promise<DeliveryPage> {
      const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
      return v1Request(`/api/v1/webhooks/${endpointId}/deliveries${query}`, isDeliveryPage);
    },
  },

  // -------------------------------------------------------------------------
  // Cross-post from an accepted proposal

  proposals: {
    crossPost(
      proposalId: number,
      request: CrossPostRequest,
      idempotencyKey = newIdempotencyKey(),
    ): Promise<PostOut> {
      return v1Request(`/api/v1/proposals/${proposalId}/cross-post`, isPostOut, {
        method: "POST",
        body: request,
        idempotencyKey,
      });
    },
  },
};

export function findTarget(post: PostOut, targetId: number): TargetOut | undefined {
  return post.targets.find((target) => target.id === targetId);
}
