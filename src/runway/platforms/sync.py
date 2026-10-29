from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    AccountMetricDay,
    AccountMetricSnapshot,
    CommentRecord,
    ConnectedAccount,
    ContentMetricSnapshot,
    DraftTarget,
    PlatformContent,
    SyncCursor,
    SyncRun,
    utcnow,
)
from runway.platforms.base import (
    AuthorizationExpired,
    CapabilityUnsupported,
    CommentItem,
    ContentItem,
    PlatformProvider,
    ProviderError,
    RateLimited,
    TokenSet,
    iso_utc,
    safe_error_summary,
)
from runway.platforms.capabilities import Platform
from runway.platforms.drafts import external_id_candidates
from runway.platforms.instagram import InstagramProvider
from runway.platforms.registry import ProviderRegistry

logger = logging.getLogger(__name__)

STALE_RUN_AGE = timedelta(minutes=30)
TOKEN_REFRESH_WINDOW = timedelta(minutes=5)
COMMENT_PAGE_LIMIT = 5


class SyncRunInProgress(Exception):
    pass


class SyncAccountMissing(Exception):
    pass


class SyncService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        registry: ProviderRegistry | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.registry = registry or ProviderRegistry(database, settings)
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # --------------------------------------------------------------- locking

    def _account_lock(self, account_id: int) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(account_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[account_id] = lock
            return lock

    def mark_stale_runs_failed(self) -> int:
        """Fail runs left in `running` state for longer than STALE_RUN_AGE."""
        cutoff = datetime.now(UTC) - STALE_RUN_AGE
        marked = 0
        with self.database.session() as session:
            runs = session.scalars(select(SyncRun).where(SyncRun.status == "running")).all()
            for run in runs:
                started = run.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                if started < cutoff:
                    run.status = "failed"
                    run.completed_at = utcnow()
                    run.error_summary = "interrupted"
                    marked += 1
        return marked

    def _has_recent_running_run(self, account_id: int) -> bool:
        cutoff = datetime.now(UTC) - STALE_RUN_AGE
        with self.database.session() as session:
            runs = session.scalars(
                select(SyncRun).where(
                    SyncRun.account_id == account_id,
                    SyncRun.status == "running",
                )
            ).all()
            for run in runs:
                started = run.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                if started >= cutoff:
                    return True
        return False

    # -------------------------------------------------------------------- run

    def _begin_run(self, account_id: int, trigger: str) -> tuple[threading.Lock, int]:
        lock = self._account_lock(account_id)
        if not lock.acquire(blocking=False):
            raise SyncRunInProgress(f"account {account_id} already has a sync in progress")
        try:
            if self._has_recent_running_run(account_id):
                raise SyncRunInProgress(f"account {account_id} already has a running sync")
            with self.database.session() as session:
                account = session.get(ConnectedAccount, account_id)
                if account is None:
                    raise SyncAccountMissing(f"account {account_id} does not exist")
                run = SyncRun(account_id=account_id, trigger=trigger, status="running")
                session.add(run)
                session.flush()
                return lock, run.id
        except BaseException:
            lock.release()
            raise

    def run(self, account_id: int, trigger: str = "manual") -> SyncRun:
        lock, run_id = self._begin_run(account_id, trigger)
        try:
            self._execute(run_id, account_id)
        finally:
            lock.release()
        return self.get_run(run_id)

    def start_background(self, account_id: int, trigger: str = "manual") -> SyncRun:
        lock, run_id = self._begin_run(account_id, trigger)

        def worker() -> None:
            try:
                self._execute(run_id, account_id)
            except Exception:  # noqa: BLE001
                logger.exception("background sync failed for account %s", account_id)
            finally:
                lock.release()

        try:
            threading.Thread(
                target=worker,
                name=f"runway-sync-account-{account_id}",
                daemon=True,
            ).start()
        except Exception:
            # The worker never started, so nothing else will release the lock
            # or finish the run; unwind both before surfacing the failure.
            lock.release()
            try:
                self._finish_run(
                    run_id,
                    account_id,
                    status="failed",
                    steps=[],
                    items_fetched=0,
                    quota_units=0,
                    error_summary="sync worker thread failed to start",
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not record failed start for sync run %s", run_id)
            raise
        return self.get_run(run_id)

    def get_run(self, run_id: int) -> SyncRun:
        with self.database.session() as session:
            run = session.get(SyncRun, run_id)
            if run is None:
                raise SyncAccountMissing(f"sync run {run_id} does not exist")
            session.expunge(run)
            return run

    def _execute(self, run_id: int, account_id: int) -> None:
        # Every path out of this method must finish the run; an exception that
        # escapes before a step records its own failure (e.g. a transport error
        # in ensure_fresh or a provider construction failure) would otherwise
        # leave the run "running" and wedge the account behind
        # _has_recent_running_run until the stale-run sweeper fires.
        steps: list[dict[str, Any]] = []
        items_fetched = 0
        quota_units = 0
        try:
            final_status = "completed"
            error_summary: str | None = None

            provider = self.registry.provider(Platform(self._account_platform(account_id)))

            try:
                token = self.ensure_fresh(account_id)
            except AuthorizationExpired as exc:
                detail = safe_error_summary(exc)
                self._finish_account_needs_reauth(account_id, detail)
                self._finish_run(
                    run_id,
                    account_id,
                    status="failed",
                    steps=steps,
                    items_fetched=items_fetched,
                    quota_units=quota_units,
                    error_summary=detail,
                )
                return

            steps_to_run: list[tuple[str, Callable[[], dict[str, int]]]] = [
                ("identity", lambda: self._step_identity(provider, account_id, token)),
                ("content", lambda: self._step_content(provider, account_id, token)),
                (
                    "content_metrics",
                    lambda: self._step_content_metrics(provider, account_id, token),
                ),
                (
                    "account_daily_metrics",
                    lambda: self._step_daily_metrics(provider, account_id, token),
                ),
                ("comments", lambda: self._step_comments(provider, account_id, token)),
            ]
            for name, step in steps_to_run:
                record: dict[str, Any] = {
                    "step": name,
                    "status": "completed",
                    "items": 0,
                    "error": None,
                    "quota_units": 0,
                }
                try:
                    result = step()
                    record["items"] = result.get("items", 0)
                    record["quota_units"] = result.get("quota_units", 0)
                    items_fetched += int(result.get("items", 0))
                    quota_units += int(result.get("quota_units", 0))
                except AuthorizationExpired as exc:
                    record["status"] = "failed"
                    record["error"] = safe_error_summary(exc)
                    final_status = "failed"
                    error_summary = record["error"]
                    steps.append(record)
                    self._finish_account_needs_reauth(account_id, record["error"])
                    break
                except CapabilityUnsupported as exc:
                    record["status"] = "skipped"
                    record["error"] = safe_error_summary(exc)
                except RateLimited as exc:
                    record["status"] = "failed"
                    record["error"] = f"{safe_error_summary(exc)} (retryable)"
                    final_status = "partial"
                    error_summary = record["error"]
                except ProviderError as exc:
                    record["status"] = "failed"
                    record["error"] = safe_error_summary(exc)
                    final_status = "partial"
                    error_summary = record["error"]
                except Exception as exc:  # noqa: BLE001
                    logger.exception("sync step %s failed for account %s", name, account_id)
                    record["status"] = "failed"
                    record["error"] = safe_error_summary(exc)
                    final_status = "partial"
                    error_summary = record["error"]
                steps.append(record)

            self._finish_run(
                run_id,
                account_id,
                status=final_status,
                steps=steps,
                items_fetched=items_fetched,
                quota_units=quota_units,
                error_summary=error_summary,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync run %s failed for account %s", run_id, account_id)
            try:
                self._finish_run(
                    run_id,
                    account_id,
                    status="failed",
                    steps=steps,
                    items_fetched=items_fetched,
                    quota_units=quota_units,
                    error_summary=safe_error_summary(exc),
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not record failure for sync run %s", run_id)

    def ensure_fresh(self, account_id: int) -> TokenSet:
        with self.database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            if account is None:
                raise SyncAccountMissing(f"account {account_id} does not exist")
            token_ref = account.token_ref
            platform = account.platform
        if not token_ref:
            raise AuthorizationExpired("account has no stored token", status=401)
        token = self.registry.token_store.load(token_ref)
        if token is None:
            raise AuthorizationExpired("account token is missing", status=401)
        if token.expires_at is None:
            return token
        expires_at = token.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at - datetime.now(UTC) > TOKEN_REFRESH_WINDOW:
            return token
        provider = self.registry.provider(Platform(platform))
        try:
            refreshed = provider.refresh(token)
        except AuthorizationExpired:
            raise
        except ProviderError as exc:
            raise AuthorizationExpired(
                f"token refresh failed: {exc.detail}", status=exc.status
            ) from exc
        self.registry.token_store.store(token_ref, refreshed)
        return refreshed

    def _account_platform(self, account_id: int) -> str:
        with self.database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            if account is None:
                raise SyncAccountMissing(f"account {account_id} does not exist")
            return str(account.platform)

    def _finish_account_needs_reauth(self, account_id: int, detail: str) -> None:
        with self.database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            if account is not None:
                account.status = "needs_reauth"
                account.last_error = detail

    def _finish_run(
        self,
        run_id: int,
        account_id: int,
        *,
        status: str,
        steps: list[dict[str, Any]],
        items_fetched: int,
        quota_units: int,
        error_summary: str | None,
    ) -> None:
        with self.database.session() as session:
            run = session.get(SyncRun, run_id)
            if run is not None:
                run.status = status
                run.completed_at = utcnow()
                run.steps_json = json.dumps(steps)
                run.items_fetched = items_fetched
                run.quota_units_estimate = quota_units
                run.error_summary = error_summary
            account = session.get(ConnectedAccount, account_id)
            if account is not None:
                account.last_sync_at = utcnow()
                account.last_sync_status = status
                if status == "failed" and error_summary:
                    account.last_error = error_summary

    # ------------------------------------------------------------------ steps

    def _step_identity(
        self, provider: PlatformProvider, account_id: int, token: TokenSet
    ) -> dict[str, int]:
        identity = provider.fetch_identity(token)
        totals: dict[str, int | float | None] = {}
        if isinstance(provider, InstagramProvider):
            try:
                totals = provider.fetch_account_totals(token)
            except ProviderError as exc:
                logger.warning(
                    "instagram totals unavailable for account %s: %s",
                    account_id,
                    exc.detail,
                )
        now = utcnow()
        with self.database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            if account is None:
                raise SyncAccountMissing(f"account {account_id} does not exist")
            account.handle = identity.handle
            account.display_name = identity.display_name
            account.avatar_url = identity.avatar_url
            account.profile_url = identity.profile_url
            account.account_type = identity.account_type
            account.last_verified_at = now
            metadata = json.loads(account.metadata_json or "{}")
            metadata["raw"] = identity.raw
            if totals:
                metadata["total_value_metrics"] = totals
            account.metadata_json = json.dumps(metadata)
            session.add(
                AccountMetricSnapshot(
                    account_id=account_id,
                    captured_at=now,
                    followers=identity.followers,
                    following=identity.following,
                    content_count=identity.content_count,
                    total_views=identity.total_views,
                    total_likes=identity.total_likes,
                    metrics_json=json.dumps(totals),
                )
            )
        return {"items": 1, "quota_units": 1}

    def _step_content(
        self, provider: PlatformProvider, account_id: int, token: TokenSet
    ) -> dict[str, int]:
        max_items = self.settings.platform_sync_max_content_items
        scope = "content"
        stored = self._load_cursor(account_id, scope) or {}
        items = 0
        quota = 0

        # Phase a: always refresh the head page so new uploads are imported
        # immediately even while a backlog is still being paged.
        page = provider.list_content(token, cursor=None, limit=min(50, max_items))
        quota += page.quota_units
        if page.items:
            self._store_content_page(account_id, provider.platform.value, page.items)
            items += len(page.items)
        head_next = page.next_cursor

        if stored.get("complete"):
            self._link_published_targets(provider.platform.value)
            return {"items": items, "quota_units": quota}

        # Phase b: backfill. Resume from the stored page token, or continue from
        # the head page when nothing was stored yet. Legacy cursors stored the
        # raw next_cursor dict, which is also a valid resume token.
        had_resume = bool(stored)
        if "resume" in stored:
            cursor = stored["resume"]
        elif stored:
            cursor = stored
        else:
            cursor = head_next
        # An outstanding backlog always gets at least one page per run, so a
        # small item bound can't starve the backfill behind the head refresh.
        backfill_pages = 0
        while cursor is not None and (items < max_items or (had_resume and backfill_pages == 0)):
            remaining = max_items - items
            page = provider.list_content(
                token, cursor=cursor, limit=min(50, remaining) if remaining > 0 else 50
            )
            quota += page.quota_units
            backfill_pages += 1
            if not page.items:
                break
            self._store_content_page(account_id, provider.platform.value, page.items)
            items += len(page.items)
            cursor = page.next_cursor
        if cursor is None:
            self._store_cursor(
                account_id,
                scope,
                {"complete": True, "completed_at": iso_utc(datetime.now(UTC))},
            )
        else:
            self._store_cursor(account_id, scope, {"resume": cursor})
        self._link_published_targets(provider.platform.value)
        return {"items": items, "quota_units": quota}

    def _store_content_page(self, account_id: int, platform: str, items: list[ContentItem]) -> None:
        now = utcnow()
        external_ids = [item.external_id for item in items]
        with self.database.session() as session:
            # One IN query prefetches every existing row for this page; the
            # per-item scalar lookup this replaces was the sync hot path.
            existing_by_external_id = {
                row.external_id: row
                for row in session.scalars(
                    select(PlatformContent).where(
                        PlatformContent.account_id == account_id,
                        PlatformContent.external_id.in_(external_ids),
                    )
                ).all()
            }
            for item in items:
                existing = existing_by_external_id.get(item.external_id)
                if existing is None:
                    existing = PlatformContent(
                        account_id=account_id,
                        platform=platform,
                        external_id=item.external_id,
                        content_type=item.content_type,
                        title=item.title,
                        caption=item.caption,
                        permalink=item.permalink,
                        thumbnail_url=item.thumbnail_url,
                        published_at=item.published_at,
                        duration_seconds=item.duration_seconds,
                        privacy_status=item.privacy_status,
                        metrics_json=json.dumps(item.metrics),
                        first_seen_at=now,
                        last_seen_at=now,
                        raw_json=json.dumps(item.raw),
                        **self._metric_columns(item.metrics),
                    )
                    session.add(existing)
                    # Register immediately so a duplicated external_id within
                    # the same page updates rather than double-inserting.
                    existing_by_external_id[item.external_id] = existing
                else:
                    existing.content_type = item.content_type
                    existing.title = item.title
                    existing.caption = item.caption
                    existing.permalink = item.permalink
                    existing.thumbnail_url = item.thumbnail_url
                    existing.published_at = item.published_at
                    existing.duration_seconds = item.duration_seconds
                    existing.privacy_status = item.privacy_status
                    existing.raw_json = json.dumps(item.raw)
                    existing.last_seen_at = now
                    self._apply_metric_columns(existing, item.metrics)

    def _link_published_targets(self, platform: str) -> None:
        """Link pending published draft targets once per run, not per page."""
        with self.database.session() as session:
            pending = session.scalars(
                select(DraftTarget).where(
                    DraftTarget.platform == platform,
                    DraftTarget.status == "published",
                    DraftTarget.published_content_id.is_(None),
                    DraftTarget.published_url.is_not(None),
                )
            ).all()
            if not pending:
                return
            urls = [target.published_url or "" for target in pending]
            external_ids = {
                candidate
                for target in pending
                for candidate in external_id_candidates(platform, target.published_url or "")
            }
            rows = session.scalars(
                select(PlatformContent).where(
                    PlatformContent.platform == platform,
                    or_(
                        PlatformContent.permalink.in_(urls),
                        PlatformContent.external_id.in_(external_ids),
                    ),
                )
            ).all()
            by_permalink = {row.permalink: row for row in rows if row.permalink is not None}
            for target in pending:
                match = by_permalink.get(target.published_url or "")
                if match is None:
                    candidates = external_id_candidates(platform, target.published_url or "")
                    match = next(
                        (row for row in rows if row.external_id in candidates),
                        None,
                    )
                if match is not None:
                    target.published_content_id = match.id

    @staticmethod
    def _metric_columns(metrics: dict[str, int | float | None]) -> dict[str, int | None]:
        def as_int(key: str) -> int | None:
            value = metrics.get(key)
            if isinstance(value, bool):
                return None
            if isinstance(value, (int, float)):
                return int(value)
            return None

        return {
            "view_count": as_int("views"),
            "like_count": as_int("likes"),
            "comment_count": as_int("comments"),
            "share_count": as_int("shares"),
        }

    def _apply_metric_columns(
        self, content: PlatformContent, metrics: dict[str, int | float | None]
    ) -> None:
        for column, value in self._metric_columns(metrics).items():
            if value is not None:
                setattr(content, column, value)

    def _step_content_metrics(
        self, provider: PlatformProvider, account_id: int, token: TokenSet
    ) -> dict[str, int]:
        with self.database.session() as session:
            rows = session.scalars(
                select(PlatformContent)
                .where(PlatformContent.account_id == account_id)
                .order_by(PlatformContent.published_at.desc())
                .limit(50)
            ).all()
            external_ids = [row.external_id for row in rows]
        if not external_ids:
            return {"items": 0, "quota_units": 0}
        try:
            metrics = provider.fetch_content_metrics(token, external_ids)
        except CapabilityUnsupported:
            return {"items": 0, "quota_units": 0}
        now = utcnow()
        touched = 0
        with self.database.session() as session:
            for row in session.scalars(
                select(PlatformContent).where(
                    PlatformContent.account_id == account_id,
                    PlatformContent.external_id.in_(external_ids),
                )
            ).all():
                values = metrics.get(row.external_id)
                if values is None:
                    continue
                merged = {**json.loads(row.metrics_json or "{}"), **values}
                row.metrics_json = json.dumps(merged)
                row.metrics_fetched_at = now
                self._apply_metric_columns(row, values)
                session.add(
                    ContentMetricSnapshot(
                        content_id=row.id,
                        captured_at=now,
                        metrics_json=json.dumps(values),
                    )
                )
                touched += 1
        return {"items": touched, "quota_units": (len(external_ids) + 49) // 50}

    def _step_daily_metrics(
        self, provider: PlatformProvider, account_id: int, token: TokenSet
    ) -> dict[str, int]:
        today = date.today()
        with self.database.session() as session:
            latest = session.scalar(
                select(func.max(AccountMetricDay.day)).where(
                    AccountMetricDay.account_id == account_id
                )
            )
        since = (
            latest - timedelta(days=2)
            if latest is not None
            else today - timedelta(days=self.settings.platform_sync_analytics_days)
        )
        metrics = provider.fetch_account_daily_metrics(token, since=since, until=today)
        now = utcnow()
        with self.database.session() as session:
            for metric in metrics:
                existing = session.scalar(
                    select(AccountMetricDay).where(
                        AccountMetricDay.account_id == account_id,
                        AccountMetricDay.day == metric.day,
                        AccountMetricDay.metric == metric.metric,
                    )
                )
                if existing is None:
                    session.add(
                        AccountMetricDay(
                            account_id=account_id,
                            day=metric.day,
                            metric=metric.metric,
                            value=metric.value,
                            source=metric.source,
                            fetched_at=now,
                        )
                    )
                else:
                    existing.value = metric.value
                    existing.source = metric.source
                    existing.fetched_at = now
        return {"items": len(metrics), "quota_units": 1 if metrics else 0}

    def _step_comments(
        self, provider: PlatformProvider, account_id: int, token: TokenSet
    ) -> dict[str, int]:
        limit = self.settings.platform_sync_comment_content_limit
        if limit <= 0:
            return {"items": 0, "quota_units": 0}
        with self.database.session() as session:
            contents = session.scalars(
                select(PlatformContent)
                .where(
                    PlatformContent.account_id == account_id,
                    PlatformContent.comment_count > 0,
                )
                .order_by(PlatformContent.published_at.desc())
                .limit(limit)
            ).all()
            targets = [(row.id, row.external_id) for row in contents]
        if not targets:
            return {"items": 0, "quota_units": 0}
        items = 0
        quota = 0
        now = utcnow()
        for content_id, external_id in targets:
            cursor: dict[str, Any] | None = None
            pages = 0
            while pages < COMMENT_PAGE_LIMIT:
                try:
                    page = provider.list_comments(
                        token,
                        content_external_id=external_id,
                        cursor=cursor,
                        limit=100,
                    )
                except CapabilityUnsupported:
                    raise
                quota += page.quota_units
                pages += 1
                if not page.items:
                    break
                items += self._store_comments(
                    account_id, provider.platform.value, content_id, page.items, now
                )
                cursor = page.next_cursor
                if cursor is None:
                    break
        return {"items": items, "quota_units": quota}

    def _store_comments(
        self,
        account_id: int,
        platform: str,
        content_id: int,
        items: list[CommentItem],
        now: datetime,
    ) -> int:
        stored = 0
        with self.database.session() as session:
            for item in items:
                existing = session.scalar(
                    select(CommentRecord).where(
                        CommentRecord.account_id == account_id,
                        CommentRecord.external_id == item.external_id,
                    )
                )
                if existing is None:
                    session.add(
                        CommentRecord(
                            account_id=account_id,
                            platform=platform,
                            external_id=item.external_id,
                            content_id=content_id,
                            parent_external_id=item.parent_external_id,
                            author_name=item.author_name,
                            author_external_id=item.author_external_id,
                            author_avatar_url=item.author_avatar_url,
                            author_is_owner=item.author_is_owner,
                            text=item.text,
                            like_count=item.like_count,
                            reply_count=item.reply_count,
                            published_at=item.published_at,
                            platform_updated_at=item.platform_updated_at,
                            status="new",
                            has_owner_reply=False,
                            permalink=item.permalink,
                            raw_json=json.dumps(item.raw),
                            first_seen_at=now,
                            last_seen_at=now,
                        )
                    )
                    stored += 1
                else:
                    existing.author_is_owner = item.author_is_owner
                    existing.text = item.text
                    existing.like_count = item.like_count
                    existing.reply_count = item.reply_count
                    existing.platform_updated_at = item.platform_updated_at
                    existing.last_seen_at = now
            session.flush()
            for item in items:
                if not item.parent_external_id:
                    continue
                parent = session.scalar(
                    select(CommentRecord).where(
                        CommentRecord.account_id == account_id,
                        CommentRecord.external_id == item.parent_external_id,
                    )
                )
                if parent is not None and item.author_is_owner:
                    parent.has_owner_reply = True
        return stored

    # ---------------------------------------------------------------- cursors

    def _load_cursor(self, account_id: int, scope: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            row = session.scalar(
                select(SyncCursor).where(
                    SyncCursor.account_id == account_id,
                    SyncCursor.scope == scope,
                )
            )
            if row is None:
                return None
            try:
                value = json.loads(row.cursor_json or "{}")
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) and value else None

    def _store_cursor(self, account_id: int, scope: str, cursor: dict[str, Any] | None) -> None:
        with self.database.session() as session:
            row = session.scalar(
                select(SyncCursor).where(
                    SyncCursor.account_id == account_id,
                    SyncCursor.scope == scope,
                )
            )
            if row is None:
                session.add(
                    SyncCursor(
                        account_id=account_id,
                        scope=scope,
                        cursor_json=json.dumps(cursor or {}),
                        updated_at=utcnow(),
                    )
                )
            else:
                row.cursor_json = json.dumps(cursor or {})
                row.updated_at = utcnow()


class PlatformSyncScheduler:
    """Background thread syncing connected accounts on a fixed interval."""

    def __init__(self, service: SyncService, interval_minutes: int) -> None:
        self.service = service
        self.interval_seconds = max(interval_minutes, 1) * 60
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="runway-platform-sync",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("platform sync scheduler tick failed")

    def _tick(self) -> None:
        with self.service.database.session() as session:
            account_ids = session.scalars(
                select(ConnectedAccount.id).where(ConnectedAccount.status == "connected")
            ).all()
        for account_id in account_ids:
            try:
                self.service.run(int(account_id), trigger="scheduled")
            except SyncRunInProgress:
                continue
            except Exception:  # noqa: BLE001
                logger.exception("scheduled sync failed for account %s", account_id)
