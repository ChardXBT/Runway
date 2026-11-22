from enum import StrEnum


class CaptureMode(StrEnum):
    MANAGED_BROWSER = "managed_browser"
    CDP = "cdp"
    FIXTURE = "fixture"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class DatePrecision(StrEnum):
    EXACT = "exact"
    DAY = "day"
    MONTH = "month"
    RELATIVE = "relative"
    UNKNOWN = "unknown"


class PostType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    MULTI_IMAGE = "multi_image"
    POLL = "poll"
    QUIZ = "quiz"
    VIDEO_SHARE = "video_share"
    UNKNOWN = "unknown"


class MediaKind(StrEnum):
    HISTORICAL = "historical"
    CANDIDATE = "candidate"
    APPROVED = "approved"
    PREVIEW = "preview"


class RightsStatus(StrEnum):
    UNKNOWN = "unknown"
    CREATOR_OWNED = "creator_owned"
    LICENSED = "licensed"
    PUBLIC_DOMAIN = "public_domain"
    BLOCKED = "blocked"


class ProposalStatus(StrEnum):
    GENERATING = "generating"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    INTERNALLY_SCHEDULED = "internally_scheduled"
    PUBLISHING = "publishing"
    EXTERNALLY_SCHEDULED = "externally_scheduled"
    PUBLISH_UNVERIFIED = "publish_unverified"
    PUBLISHED = "published"
    PUBLISH_FAILED = "publish_failed"
    CANCELLED = "cancelled"


class PublishAttemptStatus(StrEnum):
    PREPARED = "prepared"
    QUEUED = "queued"
    SUBMITTING = "submitting"
    BLOCKED_SESSION = "blocked_session"
    VERIFIED = "verified"
    SUBMITTED_UNVERIFIED = "submitted_unverified"
    FAILED_BEFORE_SUBMISSION = "failed_before_submission"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    SUPERSEDED = "superseded"
    # Resolved only at read time for rows the machine did not write (unknown
    # strings or status/timestamp combos that violate the record invariants).
    # UNKNOWN is inert: it is in no active, pending, or transitionable set.
    UNKNOWN = "unknown"


class DraftStatus(StrEnum):
    """`ContentDraft.status`. The spec 03 post status will be derived from targets."""

    DRAFT = "draft"
    SCHEDULED = "scheduled"
    PARTIALLY_PUBLISHED = "partially_published"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class TargetState(StrEnum):
    """`DraftTarget.status`, named with the spec 03 section 2 vocabulary.

    Stored values are unchanged from before the machine existed. The only
    renamed value is DRAFT, stored as the legacy "pending" (a target that has
    not been sent). Every other member stores its own spec name.
    """

    DRAFT = "pending"
    READY = "ready"
    RELEASED = "released"
    QUEUED = "queued"
    SENDING = "sending"
    PROCESSING = "processing"
    PUBLISHED = "published"
    VERIFIED = "verified"
    NEEDS_USER = "needs_user"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


class PlatformActionState(StrEnum):
    """`PlatformAction.state` for comment replies (stored values unchanged).

    Correspondence with the spec 03 vocabulary: proposed ~ queued,
    attempted ~ sending, completed ~ verified (written only after the platform
    confirmed the reply), failed ~ failed_final, uncertain = uncertain.
    """

    PROPOSED = "proposed"
    ATTEMPTED = "attempted"
    COMPLETED = "completed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class CommentStatus(StrEnum):
    """`CommentRecord.status`: the owner's moderation label for a comment."""

    NEW = "new"
    READ = "read"
    ANSWERED = "answered"
    ESCALATED = "escalated"
    IGNORED = "ignored"
