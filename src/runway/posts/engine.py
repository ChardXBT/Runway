"""The worker side of publishing: send, poll, verify, reconcile (spec 03 sections 3, 4, 6).

At-most-once sending with verified outcomes:

1. One send job per target per release (`target_id:payload_hash`).
2. The intent (a `target_attempts` row, target `sending`) is committed before
   any platform call, in an exclusive transaction that re-checks the target's
   state, release and payload hash. A changed payload is refused.
3. The adapter calls `ctx.mark_request_sent()` right before the request that
   can create the post; the marker is committed before the call.
4. Failures are classified (`classify_send_failure`): before the marker a
   failure is retryable; after it, anything but a provable non-delivery is
   `uncertain`, which is never retried automatically - a read-only
   `reconcile` job asks the platform instead.
5. A worker that dies mid-send leaves an expired lease. `handle_expired_send`
   re-runs the job only when its attempt never reached the marker; otherwise
   the target becomes `uncertain` and is reconciled.
6. Bounded automatic retries only from `failed_retryable`: 3 retries after
   1, 5 and 20 minutes, never past `planned_at + 6 h`.

`published` is never the end: `verified` needs a read-back (`verify`).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    ContentDraft,
    DraftTarget,
    PublishingControl,
    PublishJob,
    TargetAttempt,
    utcnow,
)
from runway.domain.enums import TargetState
from runway.events.log import record_event
from runway.events.types import TARGET_PROGRESS
from runway.hosted.quotas import publishing_allowed
from runway.jobs.queue import JOB_STATE_MACHINE, Job, JobState, hand_expired_send_to_reconcile
from runway.jobs.worker import DeferJob, Handler
from runway.platforms.base import safe_error_summary
from runway.platforms.publisher import (
    FailureClass,
    PublishContext,
    Publisher,
    PublishError,
    adapter_kind,
    adapter_prepare_lead,
    classify_send_failure,
    has_errors,
)
from runway.platforms.publisher_registry import PublisherRegistry, build_publisher_registry
from runway.posts.context import build_context
from runway.posts.jobs import (
    POLL_PROCESSING_JOB,
    RECONCILE_JOB,
    SEND_JOB,
    SEND_MAX_ATTEMPTS,
    VERIFY_JOB,
    enqueue_in,
    free_key,
    release_job_key,
)
from runway.posts.service import (
    SCHEDULE_ISSUE_CODES,
    check_rate_guards,
    clear_release,
    native_youtube,
)
from runway.posts.snapshot import (
    MISSED_WINDOW,
    MediaValidator,
    SnapshotSources,
    TargetSnapshot,
    TokenProvider,
    no_media_validation,
    snapshot,
    utc,
)
from runway.posts.states import current_state, set_target_state
from runway.posts.types import AttemptKind, AttemptOutcome, ReleaseState

logger = logging.getLogger(__name__)

_T = TargetState

RETRY_BACKOFF: tuple[timedelta, ...] = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=20),
)
RETRY_HORIZON = timedelta(hours=6)
PAUSED_RECHECK = timedelta(seconds=60)
DEFAULT_POLL = timedelta(seconds=30)
MAX_POLLS = 240
VERIFY_BACKOFF: tuple[timedelta, ...] = (
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=6),
    timedelta(hours=12),
    timedelta(hours=24),
)
AUTO_RECONCILE_DELAY = timedelta(seconds=60)
# A send whose worker died this many times before the marker stops re-running.
MAX_ABANDONED = 3
_DETAIL_LIMIT = 1000
_JSON_LIMIT = 16_000


def _json(value: Mapping[str, Any] | None) -> str:
    text = json.dumps(dict(value or {}), sort_keys=True, default=str)
    if len(text) > _JSON_LIMIT:
        return json.dumps({"truncated": True, "size": len(text)})
    return text


def _loads(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _detail(text: str | None) -> str | None:
    return text[:_DETAIL_LIMIT] if text else None


class ThrottledProgress:
    """`Progress` that writes at most one `target.progress` event per second (and the last)."""

    def __init__(
        self,
        database: Database,
        target: DraftTarget,
        *,
        interval: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database = database
        self.workspace_id = target.workspace_id
        self.target_id = target.id
        self.post_id = target.draft_id
        self.interval = interval
        self.monotonic = monotonic
        self._last: float | None = None

    def report(self, done: int, total: int | None = None, *, stage: str | None = None) -> None:
        now = self.monotonic()
        finished = total is not None and done >= total
        if self._last is not None and now - self._last < self.interval and not finished:
            return
        self._last = now
        try:
            with self.database.session() as session:
                record_event(
                    session,
                    self.workspace_id,
                    TARGET_PROGRESS,
                    "target",
                    self.target_id,
                    {
                        "post_id": self.post_id,
                        "target_id": self.target_id,
                        "done": done,
                        "total": total,
                        "stage": stage,
                    },
                )
        except Exception:  # noqa: BLE001 - progress must never break a send
            logger.exception("could not record progress for target %s", self.target_id)


@dataclass
class PublishingEngine:
    database: Database
    settings: Settings
    registry: PublisherRegistry
    clock: Callable[[], datetime] = utcnow
    media_validator: MediaValidator = no_media_validation
    token_provider: TokenProvider | None = None
    media_stores: dict[str, Any] = field(default_factory=dict)
    progress_interval: float = 1.0

    @property
    def sources(self) -> SnapshotSources:
        return SnapshotSources(
            settings=self.settings,
            registry=self.registry,
            clock=self.clock,
            media_validator=self.media_validator,
            media_stores=self.media_stores,
            token_provider=self.token_provider,
        )

    def handlers(self) -> dict[str, Handler]:
        return {
            SEND_JOB: self.run_send,
            POLL_PROCESSING_JOB: self.run_poll,
            VERIFY_JOB: self.run_verify,
            RECONCILE_JOB: self.run_reconcile,
        }

    # ------------------------------------------------------------ plumbing

    @contextmanager
    def _exclusive(self) -> Iterator[Session]:
        """A write transaction that holds the database write lock from its start."""
        session = self.database.session_factory()
        try:
            if self.database.engine.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def publishing_paused(self, session: Session, workspace_id: int) -> bool:
        # The global switch and the operator's workspace kill switch
        # (runway.hosted.quotas), then the workspace's own pause control.
        if not publishing_allowed(session, self.settings, workspace_id):
            return True
        control = session.get(PublishingControl, workspace_id)
        return bool(control and control.paused)

    def _load(
        self, session: Session, job: Job
    ) -> tuple[DraftTarget, ContentDraft] | tuple[None, None]:
        if job.target_id is None:
            return None, None
        target = session.get(DraftTarget, job.target_id)
        if target is None or target.workspace_id != job.workspace_id:
            return None, None
        post = session.get(ContentDraft, target.draft_id)
        if post is None:
            return None, None
        return target, post

    def _publisher(self, target: DraftTarget) -> Publisher | None:
        return self.registry.get(target.publisher)

    def _send_attempt(self, session: Session, target: DraftTarget) -> TargetAttempt | None:
        """The latest send attempt for the target's released payload."""
        return session.scalar(
            select(TargetAttempt)
            .where(
                TargetAttempt.target_id == target.id,
                TargetAttempt.kind == AttemptKind.SEND.value,
                TargetAttempt.payload_hash == target.payload_hash,
            )
            .order_by(TargetAttempt.id.desc())
            .limit(1)
        )

    def _attempt(
        self,
        session: Session,
        target: DraftTarget,
        *,
        kind: AttemptKind,
        outcome: AttemptOutcome,
        job_id: int | None,
        adapter: str | None = None,
        finished: bool = True,
        request: Mapping[str, Any] | None = None,
        response: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        external_id: str | None = None,
        resumable: Mapping[str, Any] | None = None,
        request_sent_at: datetime | None = None,
    ) -> TargetAttempt:
        now = self.clock()
        row = TargetAttempt(
            workspace_id=target.workspace_id,
            target_id=target.id,
            job_id=job_id,
            adapter=adapter or target.publisher or "engine",
            kind=kind.value,
            payload_hash=target.payload_hash,
            started_at=now,
            finished_at=now if finished else None,
            outcome=outcome.value,
            request_sent_at=request_sent_at,
            request_json=_json(request),
            response_json=_json(response),
            external_id=external_id,
            error_code=error_code,
            error_detail=_detail(error_detail),
            resumable_json=_json(resumable),
        )
        session.add(row)
        session.flush()
        return row

    def _context(
        self,
        snap: TargetSnapshot,
        target: DraftTarget,
        *,
        attempt: TargetAttempt | None,
        job_id: int | None,
        resumable: Mapping[str, Any] | None = None,
        request_sent_at: datetime | None = None,
    ) -> PublishContext:
        ctx = build_context(
            self.sources,
            snap,
            publisher=str(target.publisher),
            payload_hash=target.payload_hash,
            resumable=resumable,
            request_sent_at=request_sent_at,
            attempt_id=attempt.id if attempt else None,
            job_id=job_id,
        )
        if attempt is not None:
            attempt_id = attempt.id
            ctx._mark_request_sent = lambda: self._mark_request_sent(attempt_id)
            ctx._save_resumable = lambda state: self._save_resumable(attempt_id, state)
        return ctx

    def _mark_request_sent(self, attempt_id: int) -> datetime:
        """The point of no return, committed before the creating request."""
        with self.database.session() as session:
            attempt = session.get(TargetAttempt, attempt_id)
            if attempt is None or attempt.outcome != AttemptOutcome.PENDING.value:
                raise RuntimeError("the attempt is no longer open; refusing to send")
            if attempt.request_sent_at is None:
                attempt.request_sent_at = self.clock()
            return utc(attempt.request_sent_at) or self.clock()

    def _save_resumable(self, attempt_id: int, state: Mapping[str, Any]) -> None:
        with self.database.session() as session:
            attempt = session.get(TargetAttempt, attempt_id)
            if attempt is not None and attempt.outcome == AttemptOutcome.PENDING.value:
                attempt.resumable_json = _json(state)

    def _refuse_before_send(
        self,
        session: Session,
        target: DraftTarget,
        post: ContentDraft,
        job: Job,
        *,
        to: TargetState,
        outcome: AttemptOutcome,
        code: str,
        reason: str,
    ) -> None:
        """Record why nothing was sent and move the target back for a person."""
        self._attempt(
            session,
            target,
            kind=AttemptKind.SEND,
            outcome=outcome,
            job_id=job.id,
            error_code=code,
            error_detail=reason,
        )
        if to in {_T.READY, _T.DRAFT}:
            clear_release(target)
        set_target_state(session, target, to, reason=reason, post=post)

    # ------------------------------------------------------------------ send

    def run_send(self, job: Job) -> None:
        payload_hash = str(job.payload.get("payload_hash") or "")
        with self._exclusive() as session:
            target, post = self._load(session, job)
            if target is None or post is None:
                return
            if current_state(target) is not _T.QUEUED:
                logger.info(
                    "send job %s: target %s is not queued; nothing to do", job.id, target.id
                )
                return
            if (
                target.release_state != ReleaseState.RELEASED.value
                or not payload_hash
                or target.payload_hash != payload_hash
            ):
                logger.info("send job %s: its release was withdrawn", job.id)
                return
            now = self.clock()
            if self.publishing_paused(session, target.workspace_id):
                raise DeferJob(now + PAUSED_RECHECK, "publishing is paused")
            snap = snapshot(session, self.sources, target, post=post)
            publisher = self._publisher(target)
            if snap.payload_hash != target.payload_hash:
                self._refuse_before_send(
                    session,
                    target,
                    post,
                    job,
                    to=_T.READY,
                    outcome=AttemptOutcome.MISMATCH,
                    code="payload_changed",
                    reason="the post changed after it was released; review it and release again",
                )
                return
            planned = utc(target.planned_at)
            if planned is not None and not native_youtube(snap) and now - planned > MISSED_WINDOW:
                self._refuse_before_send(
                    session,
                    target,
                    post,
                    job,
                    to=_T.READY,
                    outcome=AttemptOutcome.NOT_SENT,
                    code="missed_window",
                    reason="missed while offline: the planned time passed more than an hour ago; "
                    "choose a new time",
                )
                return
            if publisher is None:
                self._refuse_before_send(
                    session,
                    target,
                    post,
                    job,
                    to=_T.FAILED_FINAL,
                    outcome=AttemptOutcome.NOT_SENT,
                    code="publisher_unavailable",
                    reason=f"the publisher {target.publisher} is not available; choose another",
                )
                return
            blocking = [
                issue
                for issue in snap.issues
                if issue.severity == "error" and issue.code not in SCHEDULE_ISSUE_CODES
            ]
            if blocking:
                self._refuse_before_send(
                    session,
                    target,
                    post,
                    job,
                    to=_T.FAILED_FINAL,
                    outcome=AttemptOutcome.NOT_SENT,
                    code="not_valid",
                    reason="not sent: " + "; ".join(issue.message for issue in blocking[:3]),
                )
                return
            decision = check_rate_guards(session, self.sources, snap, str(target.publisher))
            if not decision.allowed:
                self._refuse_before_send(
                    session,
                    target,
                    post,
                    job,
                    to=_T.READY,
                    outcome=AttemptOutcome.NOT_SENT,
                    code="rate_limited",
                    reason=f"not sent: {decision.detail}; choose a new time",
                )
                return
            previous = self._send_attempt(session, target)
            resumable = _loads(previous.resumable_json) if previous is not None else {}
            # An adapter that resumes the saved session (TikTok's upload) never
            # re-marks: the new attempt inherits the point of no return the
            # earlier one committed, or a mid-upload failure reads as "not
            # sent" and blindly retries into a second publish session.
            sent_at = (
                utc(previous.request_sent_at)
                if previous is not None and resumable and previous.request_sent_at is not None
                else None
            )
            lead = adapter_prepare_lead(publisher)
            prepare = (
                lead is not None
                and planned is not None
                and now < planned
                and not self._prepared(session, target)
            )
            if prepare:
                attempt = self._attempt(
                    session,
                    target,
                    kind=AttemptKind.PREPARE,
                    outcome=AttemptOutcome.PENDING,
                    job_id=job.id,
                    finished=False,
                )
            elif planned is not None and now < planned and lead is not None:
                # Prepared already; the publish call waits for the planned time.
                raise DeferJob(planned, "waiting for the planned time")
            else:
                attempt = self._attempt(
                    session,
                    target,
                    kind=AttemptKind.SEND,
                    outcome=AttemptOutcome.PENDING,
                    job_id=job.id,
                    finished=False,
                    request={"payload": snap.payload, "publisher": target.publisher},
                    resumable=resumable,
                    request_sent_at=(previous.request_sent_at if sent_at is not None else None),
                )
                set_target_state(session, target, _T.SENDING, post=post)
            ctx = self._context(
                snap,
                target,
                attempt=attempt,
                job_id=job.id,
                resumable=resumable,
                request_sent_at=sent_at,
            )
            progress = ThrottledProgress(self.database, target, interval=self.progress_interval)
            attempt_id = attempt.id
        if prepare:
            self._run_prepare(job, attempt_id, publisher, ctx, planned)
            return
        self._run_send_call(job, attempt_id, publisher, ctx, progress)

    def _prepared(self, session: Session, target: DraftTarget) -> bool:
        return (
            session.scalar(
                select(TargetAttempt.id).where(
                    TargetAttempt.target_id == target.id,
                    TargetAttempt.kind == AttemptKind.PREPARE.value,
                    TargetAttempt.payload_hash == target.payload_hash,
                    TargetAttempt.outcome == AttemptOutcome.PREPARED.value,
                )
            )
            is not None
        )

    def _run_prepare(
        self,
        job: Job,
        attempt_id: int,
        publisher: Publisher,
        ctx: PublishContext,
        planned: datetime | None,
    ) -> None:
        """Work before `planned_at` that cannot publish (an Instagram container)."""
        prepare = getattr(publisher, "prepare", None)
        try:
            state = dict(prepare(ctx)) if callable(prepare) else {}
        except Exception as exc:  # noqa: BLE001 - nothing can be published by prepare
            with self.database.session() as session:
                attempt = session.get(TargetAttempt, attempt_id)
                if attempt is not None:
                    attempt.outcome = AttemptOutcome.FAILED_RETRYABLE.value
                    attempt.finished_at = self.clock()
                    attempt.error_detail = _detail(safe_error_summary(exc))
            # The send at planned_at gets no prepared state, so the adapter
            # does the preparation inline then.
            raise DeferJob(
                planned or self.clock(), "preparation failed; trying at the planned time"
            ) from None
        with self.database.session() as session:
            attempt = session.get(TargetAttempt, attempt_id)
            if attempt is not None:
                attempt.outcome = AttemptOutcome.PREPARED.value
                attempt.finished_at = self.clock()
                attempt.resumable_json = _json(state)
                attempt.response_json = _json(state)
        raise DeferJob(planned or self.clock(), "prepared; waiting for the planned time")

    def _run_send_call(
        self,
        job: Job,
        attempt_id: int,
        publisher: Publisher,
        ctx: PublishContext,
        progress: ThrottledProgress,
    ) -> None:
        if ctx.resumable == {} and self._latest_prepared(ctx.target_id, ctx.payload_hash):
            ctx.resumable = self._latest_prepared(ctx.target_id, ctx.payload_hash)
        try:
            issues = publisher.preflight(ctx)
        except Exception as exc:  # noqa: BLE001
            self._finish_failure(
                job,
                attempt_id,
                classify_send_failure(exc, request_sent=False, detail=safe_error_summary(exc)),
            )
            return
        if has_errors(issues):
            errors = [issue.message for issue in issues if issue.severity == "error"]
            self._finish_failure(
                job,
                attempt_id,
                FailureClass("failed_final", "preflight_failed", "not sent: " + "; ".join(errors)),
                outcome=AttemptOutcome.NOT_SENT,
            )
            return
        try:
            result = publisher.send(ctx, progress)
        except Exception as exc:  # noqa: BLE001 - classified, never swallowed silently
            detail = exc.detail if isinstance(exc, PublishError) else safe_error_summary(exc)
            failure = classify_send_failure(
                exc, request_sent=ctx.request_sent_at is not None, detail=detail
            )
            logger.warning(
                "send job %s target %s: %s (%s)", job.id, ctx.target_id, failure.state, detail
            )
            self._finish_failure(job, attempt_id, failure)
            return
        self._finish_send(job, attempt_id, publisher, result)

    def _latest_prepared(self, target_id: int, payload_hash: str) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.scalar(
                select(TargetAttempt)
                .where(
                    TargetAttempt.target_id == target_id,
                    TargetAttempt.kind == AttemptKind.PREPARE.value,
                    TargetAttempt.payload_hash == payload_hash,
                    TargetAttempt.outcome == AttemptOutcome.PREPARED.value,
                )
                .order_by(TargetAttempt.id.desc())
                .limit(1)
            )
            return _loads(row.resumable_json) if row is not None else {}

    def _open_attempt(
        self, session: Session, attempt_id: int
    ) -> tuple[TargetAttempt, DraftTarget, ContentDraft] | None:
        attempt = session.get(TargetAttempt, attempt_id)
        if attempt is None or attempt.outcome != AttemptOutcome.PENDING.value:
            # Settled meanwhile (an expired lease handed it to reconciliation).
            return None
        target = session.get(DraftTarget, attempt.target_id)
        post = session.get(ContentDraft, target.draft_id) if target is not None else None
        if target is None or post is None or current_state(target) is not _T.SENDING:
            return None
        return attempt, target, post

    def _finish_send(self, job: Job, attempt_id: int, publisher: Publisher, result: Any) -> None:
        with self._exclusive() as session:
            opened = self._open_attempt(session, attempt_id)
            if opened is None:
                return
            attempt, target, post = opened
            now = self.clock()
            if attempt.request_sent_at is None and adapter_kind(publisher) != "assisted":
                # An adapter that forgot the marker still sent something.
                attempt.request_sent_at = now
            response = dict(result.response)
            if result.checklist:
                response["checklist"] = list(result.checklist)
            attempt.outcome = {
                "processing": AttemptOutcome.PROCESSING,
                "published": AttemptOutcome.PUBLISHED,
                "needs_user": AttemptOutcome.NEEDS_USER,
            }[result.status].value
            attempt.finished_at = now
            attempt.external_id = result.external_id
            attempt.response_json = _json(response)
            if result.external_id:
                target.external_id = result.external_id
            if result.status == "published":
                self._published(session, target, post, result.permalink, job_id=job.id)
            elif result.status == "processing":
                set_target_state(session, target, _T.PROCESSING, reason=result.detail, post=post)
                self._next_poll(session, target, result.next_poll_seconds, number=1)
            else:
                set_target_state(session, target, _T.NEEDS_USER, reason=result.detail, post=post)
                if adapter_kind(publisher) != "assisted":
                    self._next_poll(session, target, result.next_poll_seconds, number=1)

    def _published(
        self,
        session: Session,
        target: DraftTarget,
        post: ContentDraft,
        permalink: str | None,
        *,
        job_id: int | None,
    ) -> None:
        now = self.clock()
        if permalink:
            target.published_url = permalink
        target.published_at = now
        set_target_state(session, target, _T.PUBLISHED, post=post)
        enqueue_in(
            session,
            VERIFY_JOB,
            idempotency_key=free_key(session, f"verify:{target.id}:{target.payload_hash}"),
            run_at=now + VERIFY_BACKOFF[0],
            workspace_id=target.workspace_id,
            target_id=target.id,
            payload={"try": 1},
        )

    def _next_poll(
        self, session: Session, target: DraftTarget, seconds: int | None, *, number: int
    ) -> None:
        delay = timedelta(seconds=seconds) if seconds else DEFAULT_POLL
        enqueue_in(
            session,
            POLL_PROCESSING_JOB,
            idempotency_key=free_key(session, f"poll:{target.id}:{target.payload_hash}:{number}"),
            run_at=self.clock() + delay,
            workspace_id=target.workspace_id,
            target_id=target.id,
            payload={"number": number},
        )

    def _finish_failure(
        self,
        job: Job,
        attempt_id: int,
        failure: FailureClass,
        *,
        outcome: AttemptOutcome | None = None,
    ) -> None:
        with self._exclusive() as session:
            opened = self._open_attempt(session, attempt_id)
            if opened is None:
                return
            attempt, target, post = opened
            attempt.outcome = (outcome or AttemptOutcome(failure.state)).value
            attempt.finished_at = self.clock()
            attempt.error_code = failure.code[:80]
            attempt.error_detail = _detail(failure.detail)
            session.flush()  # counted by the retry rule; the sessions do not autoflush
            self._apply_failure(session, target, post, failure)

    def _apply_failure(
        self, session: Session, target: DraftTarget, post: ContentDraft, failure: FailureClass
    ) -> None:
        if failure.state == "uncertain":
            set_target_state(
                session,
                target,
                _T.UNCERTAIN,
                reason=f"the platform may have received the post ({failure.detail}); checking",
                post=post,
            )
            enqueue_in(
                session,
                RECONCILE_JOB,
                idempotency_key=free_key(session, f"reconcile:{target.id}:{target.payload_hash}"),
                run_at=self.clock() + AUTO_RECONCILE_DELAY,
                workspace_id=target.workspace_id,
                target_id=target.id,
                payload={"automatic": True},
            )
            return
        if failure.state == "failed_final":
            set_target_state(session, target, _T.FAILED_FINAL, reason=failure.detail, post=post)
            return
        set_target_state(session, target, _T.FAILED_RETRYABLE, reason=failure.detail, post=post)
        self._maybe_retry(session, target, post, failure)

    def _maybe_retry(
        self, session: Session, target: DraftTarget, post: ContentDraft, failure: FailureClass
    ) -> None:
        """Spec 03 section 4 step 6: 3 retries, 1/5/20 minutes, never past planned_at + 6 h."""
        failures = len(
            session.scalars(
                select(TargetAttempt.id).where(
                    TargetAttempt.target_id == target.id,
                    TargetAttempt.payload_hash == target.payload_hash,
                    TargetAttempt.kind.in_([AttemptKind.SEND.value, AttemptKind.POLL.value]),
                    TargetAttempt.outcome == AttemptOutcome.FAILED_RETRYABLE.value,
                )
            ).all()
        )
        now = self.clock()
        planned = utc(target.planned_at) or now
        if failures > len(RETRY_BACKOFF):
            target.state_reason = _detail(
                f"{failure.detail}; automatic retries are used up - release again to retry"
            )
            return
        run_at = now + RETRY_BACKOFF[failures - 1]
        if failure.retry_after is not None:
            run_at = max(run_at, failure.retry_after)
        if run_at > planned + RETRY_HORIZON:
            target.state_reason = _detail(
                f"{failure.detail}; too late for an automatic retry - release again to retry"
            )
            return
        job = enqueue_in(
            session,
            SEND_JOB,
            idempotency_key=free_key(
                session,
                f"{release_job_key(target.id, str(target.payload_hash))}:retry{failures}",
            ),
            run_at=run_at,
            workspace_id=target.workspace_id,
            target_id=target.id,
            payload={"payload_hash": target.payload_hash, "publisher": target.publisher},
            max_attempts=SEND_MAX_ATTEMPTS,
        )
        set_target_state(
            session,
            target,
            _T.QUEUED,
            reason=f"retry {failures} of {len(RETRY_BACKOFF)} at {run_at.isoformat()}",
            post=post,
            event_payload={"job_id": job.id, "retry": failures},
        )

    # --------------------------------------------------------- expired leases

    def handle_expired_send(self, session: Session, job: PublishJob, now: datetime) -> None:
        """A send whose worker vanished: re-run only if the marker was never reached."""
        attempt = session.scalar(
            select(TargetAttempt)
            .where(TargetAttempt.job_id == job.id)
            .order_by(TargetAttempt.id.desc())
            .limit(1)
        )
        target = session.get(DraftTarget, job.target_id) if job.target_id is not None else None
        post = session.get(ContentDraft, target.draft_id) if target is not None else None
        if attempt is not None and attempt.outcome != AttemptOutcome.PENDING.value:
            # The outcome was recorded; only the job's own bookkeeping was lost.
            JOB_STATE_MACHINE.apply(job, JobState.COMPLETED)
            job.locked_by = None
            job.locked_until = None
            return
        if attempt is None or attempt.request_sent_at is None:
            if attempt is not None:
                attempt.outcome = AttemptOutcome.ABANDONED.value
                attempt.finished_at = now
                attempt.error_code = "worker_stopped"
                attempt.error_detail = "the worker stopped before the request was sent"
                session.flush()  # the sessions do not autoflush
            abandoned = len(
                session.scalars(
                    select(TargetAttempt.id).where(
                        TargetAttempt.job_id == job.id,
                        TargetAttempt.outcome == AttemptOutcome.ABANDONED.value,
                    )
                ).all()
            )
            job.locked_by = None
            job.locked_until = None
            if abandoned >= MAX_ABANDONED:
                JOB_STATE_MACHINE.apply(job, JobState.FAILED)
                job.last_error = "the worker stopped repeatedly before sending"
                if target is not None and post is not None and current_state(target) is _T.SENDING:
                    set_target_state(
                        session,
                        target,
                        _T.FAILED_RETRYABLE,
                        reason="the worker stopped repeatedly before sending; nothing was sent",
                        post=post,
                    )
                return
            JOB_STATE_MACHINE.apply(job, JobState.PENDING)
            job.run_at = now
            job.last_error = "lease expired before the request was sent; running again"
            if target is not None and post is not None and current_state(target) is _T.SENDING:
                set_target_state(
                    session,
                    target,
                    _T.QUEUED,
                    reason="the worker restarted before sending; sending again",
                    post=post,
                )
            return
        attempt.outcome = AttemptOutcome.UNCERTAIN.value
        attempt.finished_at = now
        attempt.error_code = "worker_lost"
        attempt.error_detail = "the worker stopped after the request was sent; outcome unknown"
        if target is not None and post is not None and current_state(target) is _T.SENDING:
            set_target_state(
                session,
                target,
                _T.UNCERTAIN,
                reason="the worker stopped after sending; checking the platform",
                post=post,
            )
        hand_expired_send_to_reconcile(session, job, now)

    # ------------------------------------------------------------ read jobs

    def _read_context(
        self, session: Session, target: DraftTarget, post: ContentDraft, job: Job
    ) -> tuple[PublishContext, Publisher] | None:
        publisher = self._publisher(target)
        if publisher is None:
            return None
        snap = snapshot(session, self.sources, target, post=post)
        send = self._send_attempt(session, target)
        ctx = self._context(
            snap,
            target,
            attempt=None,
            job_id=job.id,
            resumable=_loads(send.resumable_json) if send is not None else {},
            request_sent_at=utc(send.request_sent_at) if send is not None else None,
        )
        return ctx, publisher

    def run_poll(self, job: Job) -> None:
        number = int(job.payload.get("number") or 1)
        with self.database.session() as session:
            target, post = self._load(session, job)
            if target is None or post is None:
                return
            if current_state(target) not in {_T.PROCESSING, _T.NEEDS_USER}:
                return
            loaded = self._read_context(session, target, post, job)
        if loaded is None:
            return
        ctx, publisher = loaded
        try:
            result = publisher.poll(ctx)
        except Exception as exc:  # noqa: BLE001 - a read; ask again later
            with self.database.session() as session:
                target, post = self._load(session, job)
                if target is not None and number < MAX_POLLS:
                    target.state_reason = _detail(
                        f"could not check the platform: {safe_error_summary(exc)}"
                    )
                    self._next_poll(session, target, None, number=number + 1)
            return
        with self._exclusive() as session:
            target, post = self._load(session, job)
            if target is None or post is None:
                return
            state = current_state(target)
            if state not in {_T.PROCESSING, _T.NEEDS_USER}:
                return
            if result.external_id:
                target.external_id = result.external_id
            if result.status == "published":
                self._attempt(
                    session,
                    target,
                    kind=AttemptKind.POLL,
                    outcome=AttemptOutcome.PUBLISHED,
                    job_id=job.id,
                    external_id=result.external_id,
                    response=result.response,
                )
                self._published(session, target, post, result.permalink, job_id=job.id)
            elif result.status == "failed":
                failure = FailureClass(
                    "failed_retryable" if result.retryable else "failed_final",
                    result.error_code or "processing_failed",
                    result.detail or "the platform could not finish the post",
                )
                self._attempt(
                    session,
                    target,
                    kind=AttemptKind.POLL,
                    outcome=AttemptOutcome(failure.state),
                    job_id=job.id,
                    error_code=failure.code,
                    error_detail=failure.detail,
                    response=result.response,
                )
                self._apply_failure(session, target, post, failure)
            else:
                if result.status == "needs_user" and state is _T.PROCESSING:
                    set_target_state(
                        session, target, _T.NEEDS_USER, reason=result.detail, post=post
                    )
                if number >= MAX_POLLS:
                    target.state_reason = "still waiting on the platform; check it there"
                    return
                self._next_poll(session, target, result.next_poll_seconds, number=number + 1)

    def run_verify(self, job: Job) -> None:
        attempt_number = int(job.payload.get("try") or 1)
        with self.database.session() as session:
            target, post = self._load(session, job)
            if target is None or post is None or current_state(target) is not _T.PUBLISHED:
                return
            loaded = self._read_context(session, target, post, job)
        if loaded is None:
            return
        ctx, publisher = loaded
        verified: bool | None
        detail: str | None
        permalink: str | None
        mismatches: tuple[str, ...]
        response: Mapping[str, Any]
        try:
            result = publisher.verify(ctx)
        except Exception as exc:  # noqa: BLE001 - a read; ask again later
            verified, detail, permalink, mismatches = None, safe_error_summary(exc), None, ()
            response = {}
        else:
            verified, detail, permalink = result.verified, result.detail, result.permalink
            mismatches, response = result.mismatches, result.response
        with self._exclusive() as session:
            target, post = self._load(session, job)
            if target is None or post is None or current_state(target) is not _T.PUBLISHED:
                return
            if verified is True:
                self._attempt(
                    session,
                    target,
                    kind=AttemptKind.VERIFY,
                    outcome=AttemptOutcome.VERIFIED,
                    job_id=job.id,
                    response=response,
                )
                if permalink:
                    target.published_url = permalink
                set_target_state(session, target, _T.VERIFIED, reason=detail, post=post)
            elif verified is False:
                self._attempt(
                    session,
                    target,
                    kind=AttemptKind.VERIFY,
                    outcome=AttemptOutcome.MISMATCH,
                    job_id=job.id,
                    error_detail=", ".join(mismatches) or detail,
                    response=response,
                )
                target.state_reason = _detail(
                    "published, but the platform shows something different: "
                    + (", ".join(mismatches) or detail or "unknown difference")
                )
            elif attempt_number < len(VERIFY_BACKOFF):
                target.state_reason = _detail(f"published; not read back yet ({detail})")
                enqueue_in(
                    session,
                    VERIFY_JOB,
                    idempotency_key=free_key(
                        session, f"verify:{target.id}:{target.payload_hash}:{attempt_number + 1}"
                    ),
                    run_at=self.clock() + VERIFY_BACKOFF[attempt_number],
                    workspace_id=target.workspace_id,
                    target_id=target.id,
                    payload={"try": attempt_number + 1},
                )
            else:
                target.state_reason = "published; Runway could not read it back from the platform"

    def run_reconcile(self, job: Job) -> None:
        with self.database.session() as session:
            target, post = self._load(session, job)
            if target is None or post is None or current_state(target) is not _T.UNCERTAIN:
                return
            loaded = self._read_context(session, target, post, job)
        if loaded is None:
            return
        ctx, publisher = loaded
        result = publisher.reconcile(ctx)  # errors: the queue retries this read job
        with self._exclusive() as session:
            target, post = self._load(session, job)
            if target is None or post is None or current_state(target) is not _T.UNCERTAIN:
                return
            response = dict(result.response)
            response["checked"] = list(result.checked)
            if result.candidates:
                response["candidates"] = [dict(item) for item in result.candidates]
            self._attempt(
                session,
                target,
                kind=AttemptKind.RECONCILE,
                outcome={
                    "published": AttemptOutcome.PUBLISHED,
                    "processing": AttemptOutcome.PROCESSING,
                    "needs_user": AttemptOutcome.NEEDS_USER,
                    "absent": AttemptOutcome.ABSENT,
                    "uncertain": AttemptOutcome.UNCERTAIN,
                }[result.status],
                job_id=job.id,
                external_id=result.external_id,
                error_detail=result.detail,
                response=response,
            )
            if result.external_id:
                target.external_id = result.external_id
            checked = "; ".join(result.checked) or "the platform"
            if result.status == "published":
                self._published(session, target, post, result.permalink, job_id=job.id)
            elif result.status == "processing":
                set_target_state(session, target, _T.PROCESSING, reason=result.detail, post=post)
                self._next_poll(session, target, None, number=1)
            elif result.status == "needs_user":
                set_target_state(session, target, _T.NEEDS_USER, reason=result.detail, post=post)
                if adapter_kind(publisher) != "assisted":
                    self._next_poll(session, target, None, number=1)
            elif result.status == "absent":
                # Provably not published. No automatic retry: a person releases again.
                set_target_state(
                    session,
                    target,
                    _T.FAILED_RETRYABLE,
                    reason=f"not found on the platform (checked {checked}); nothing was "
                    "published - release again to retry",
                    post=post,
                )
            else:
                target.state_reason = _detail(
                    f"could not tell whether it was published (checked {checked}); check the "
                    "platform, then mark it published or not published"
                )


def build_publishing_engine(
    settings: Settings,
    database: Database,
    *,
    registry: PublisherRegistry | None = None,
    clock: Callable[[], datetime] = utcnow,
    media_validator: MediaValidator | None = None,
    media_stores: dict[str, Any] | None = None,
) -> PublishingEngine:
    from runway.posts.media import default_media_stores, probe_media_validator
    from runway.posts.runtime import provider_token_provider

    return PublishingEngine(
        database=database,
        settings=settings,
        registry=registry or build_publisher_registry(settings, database, clock=clock),
        clock=clock,
        media_validator=media_validator or probe_media_validator,
        token_provider=provider_token_provider(database, settings),
        media_stores=media_stores if media_stores is not None else default_media_stores(settings),
    )
