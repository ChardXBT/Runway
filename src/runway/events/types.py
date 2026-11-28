"""Every event type Runway writes to the `events` table (spec 04 section 4).

Add a constant here before writing a new type; `record_event` refuses others.
"""

from __future__ import annotations

ACCOUNT_UPDATED = "account.updated"
SYNC_STARTED = "sync.started"
SYNC_FINISHED = "sync.finished"
CONTENT_IMPORTED = "content.imported"
COMMENT_NEW = "comment.new"
TARGET_STATE_CHANGED = "target.state_changed"
TARGET_PROGRESS = "target.progress"
# Spec 06 section 4: an agent-created target is waiting for a human release.
TARGET_AWAITING_RELEASE = "target.awaiting_release"
# Spec 06 section 8: a person rejected an agent's target; carries the reason
# and the person's edits as structured before/after data.
TARGET_REJECTED = "target.rejected"
MEDIA_READY = "media.ready"
QUOTA_LOW = "quota.low"

EVENT_TYPES: frozenset[str] = frozenset(
    {
        ACCOUNT_UPDATED,
        SYNC_STARTED,
        SYNC_FINISHED,
        CONTENT_IMPORTED,
        COMMENT_NEW,
        TARGET_STATE_CHANGED,
        TARGET_PROGRESS,
        TARGET_AWAITING_RELEASE,
        TARGET_REJECTED,
        MEDIA_READY,
        QUOTA_LOW,
    }
)

# Stream control message, never stored: the client's Last-Event-ID is older
# than retention (or unknown), so it must refetch what it shows.
RESYNC = "resync"
