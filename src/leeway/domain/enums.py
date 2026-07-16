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
    PUBLISHED = "published"
    PUBLISH_FAILED = "publish_failed"
    CANCELLED = "cancelled"
