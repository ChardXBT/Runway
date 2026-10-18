from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    AccountMetricDay,
    AccountMetricSnapshot,
    ConnectedAccount,
    PlatformContent,
    SyncRun,
    utcnow,
)
from runway.platforms.base import (
    ProviderError,
    TokenSet,
    iso_utc,
)
from runway.platforms.capabilities import (
    YOUTUBE_ENGAGE_SCOPE,
    Capability,
    Platform,
    capability_matrix,
    capability_ready,
)
from runway.platforms.content import ContentService
from runway.platforms.drafts import DraftService
from runway.platforms.inbox import InboxReplyForbidden, InboxService
from runway.platforms.instagram import InstagramProvider
from runway.platforms.oauth import OAuthStateError, OAuthStateService
from runway.platforms.overview import OverviewService
from runway.platforms.registry import ProviderRegistry, parse_platform
from runway.platforms.sync import SyncRunInProgress, SyncService
from runway.platforms.tokens import pkce_challenge

logger = logging.getLogger(__name__)

FRESHNESS_STALE_AFTER = timedelta(hours=24)

_METRIC_LABELS = {
    "views": "Views",
    "estimatedMinutesWatched": "Watch time (min)",
    "averageViewDuration": "Avg. view duration (s)",
    "subscribersGained": "Subscribers gained",
    "subscribersLost": "Subscribers lost",
    "likes": "Likes",
    "comments": "Comments",
    "shares": "Shares",
    "reach": "Reach",
    "follower_count": "Followers",
}


INSTAGRAM_KNOWN_SCOPES = (
    "instagram_business_basic",
    "instagram_business_manage_insights",
    "instagram_business_manage_comments",
    "instagram_business_manage_messages",
    "instagram_business_content_publish",
)


class ImportTokenRequest(BaseModel):
    access_token: str = Field(min_length=8, max_length=2000)
    granted_scopes: list[
        Literal[
            "instagram_business_basic",
            "instagram_business_manage_insights",
            "instagram_business_manage_comments",
            "instagram_business_manage_messages",
            "instagram_business_content_publish",
        ]
    ] = ["instagram_business_basic"]


class ConfirmedRequest(BaseModel):
    confirmed: Literal[True]


class GroupCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content_ids: list[int] = Field(default_factory=list, max_length=500)


class GroupAssignRequest(BaseModel):
    group_id: int | None = Field(default=None, ge=1)


class InboxStatusRequest(BaseModel):
    status: Literal["new", "read", "answered", "escalated", "ignored"]


class DraftTextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class ReplyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    confirmed: Literal[True]


class DraftTargetRequest(BaseModel):
    platform: Literal["youtube", "instagram", "tiktok"]
    account_id: int | None = Field(default=None, ge=1)
    caption_override: str | None = Field(default=None, max_length=8000)


class DraftCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=20000)
    scheduled_for: datetime | None = None
    notes: str | None = Field(default=None, max_length=4000)
    targets: list[DraftTargetRequest] = Field(default_factory=list, max_length=20)


class DraftUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = Field(default=None, max_length=20000)
    scheduled_for: datetime | None = None
    clear_schedule: bool = False
    notes: str | None = Field(default=None, max_length=4000)
    status: Literal["draft", "scheduled"] | None = None


class MarkPublishedRequest(BaseModel):
    published_url: str = Field(min_length=1, max_length=2000)
    confirmed: Literal[True]


def _account_freshness(account: ConnectedAccount) -> str:
    if account.last_sync_at is None:
        return "never"
    last_sync = account.last_sync_at
    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=UTC)
    if datetime.now(UTC) - last_sync > FRESHNESS_STALE_AFTER:
        return "stale"
    return "fresh"


REQUIRED_ENV: dict[Platform, list[str]] = {
    Platform.youtube: ["RUNWAY_YOUTUBE_CLIENT_ID", "RUNWAY_YOUTUBE_CLIENT_SECRET"],
    Platform.instagram: ["RUNWAY_INSTAGRAM_APP_ID", "RUNWAY_INSTAGRAM_APP_SECRET"],
    Platform.tiktok: ["RUNWAY_TIKTOK_CLIENT_KEY", "RUNWAY_TIKTOK_CLIENT_SECRET"],
}


def _blocked_by_setting(
    entry: object,
    platform: Platform,
    granted_scopes: list[str],
    settings: Settings,
) -> bool:
    capability = getattr(entry, "capability", None)
    if capability != Capability.comments_reply:
        return False
    if not settings.inbox_replies_enabled:
        return True
    return (
        platform is Platform.youtube
        and not settings.youtube_engage_scopes
        and YOUTUBE_ENGAGE_SCOPE not in granted_scopes
    )


def _account_view(
    database: Database,
    settings: Settings,
    account: ConnectedAccount,
) -> dict[str, object]:
    granted_scopes = json.loads(account.granted_scopes_json or "[]")
    try:
        metadata = json.loads(account.metadata_json or "{}")
    except json.JSONDecodeError:
        metadata = {}
    scopes_source = metadata.get("scopes_source")
    entries = capability_matrix(
        Platform(account.platform),
        engage_scopes=settings.youtube_engage_scopes,
        replies_enabled=settings.inbox_replies_enabled,
    )
    with database.session() as session:
        snapshot = session.scalar(
            select(AccountMetricSnapshot)
            .where(AccountMetricSnapshot.account_id == account.id)
            .order_by(AccountMetricSnapshot.captured_at.desc())
            .limit(1)
        )
        latest_snapshot = (
            {
                "followers": snapshot.followers,
                "following": snapshot.following,
                "content_count": snapshot.content_count,
                "total_views": snapshot.total_views,
                "total_likes": snapshot.total_likes,
                "captured_at": iso_utc(snapshot.captured_at),
            }
            if snapshot is not None
            else None
        )
    return {
        "id": account.id,
        "platform": account.platform,
        "external_account_id": account.external_account_id,
        "handle": account.handle,
        "display_name": account.display_name,
        "avatar_url": account.avatar_url,
        "profile_url": account.profile_url,
        "account_type": account.account_type,
        "status": account.status,
        "granted_scopes": granted_scopes,
        "scopes_source": scopes_source,
        "connected_at": iso_utc(account.connected_at),
        "last_verified_at": (iso_utc(account.last_verified_at)),
        "last_sync_at": iso_utc(account.last_sync_at),
        "last_sync_status": account.last_sync_status,
        "last_error": account.last_error,
        "freshness": _account_freshness(account),
        "capabilities": [
            {
                "capability": entry.capability.value,
                "state": entry.state,
                "ready": capability_ready(
                    entry,
                    account_status=account.status,
                    granted_scopes=granted_scopes,
                ),
                "missing_scopes": [
                    scope for scope in entry.requires_scopes if scope not in granted_scopes
                ],
                "blocked_by_setting": _blocked_by_setting(
                    entry, Platform(account.platform), granted_scopes, settings
                ),
                "detail": entry.detail,
            }
            for entry in entries
        ],
        "latest_snapshot": latest_snapshot,
    }


def _run_view(run: SyncRun) -> dict[str, object]:
    try:
        steps = json.loads(run.steps_json or "[]")
    except json.JSONDecodeError:
        steps = []
    return {
        "id": run.id,
        "account_id": run.account_id,
        "trigger": run.trigger,
        "status": run.status,
        "started_at": iso_utc(run.started_at),
        "completed_at": iso_utc(run.completed_at),
        "steps": steps,
        "items_fetched": run.items_fetched,
        "quota_units_estimate": run.quota_units_estimate,
        "error_summary": run.error_summary,
    }


def _enrich_token(
    platform: Platform,
    token: TokenSet,
    identity_raw: dict[str, object],
    external_account_id: str,
) -> TokenSet:
    extra = dict(token.extra)
    if platform is Platform.youtube:
        extra["channel_id"] = external_account_id
        uploads = identity_raw.get("uploads_playlist")
        if isinstance(uploads, str) and uploads:
            extra["uploads_playlist"] = uploads
        else:
            details = identity_raw.get("contentDetails")
            if isinstance(details, dict):
                related = details.get("relatedPlaylists")
                if isinstance(related, dict) and isinstance(related.get("uploads"), str):
                    extra["uploads_playlist"] = str(related["uploads"])
    elif platform is Platform.instagram:
        extra["user_id"] = external_account_id
    return token.model_copy(update={"extra": extra})


def build_platform_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["platforms"])
    registry = ProviderRegistry(database, settings)
    oauth_states = OAuthStateService(database)
    sync_service = SyncService(database, settings, registry)
    content_service = ContentService(database)
    inbox_service = InboxService(database, settings, registry)
    draft_service = DraftService(database, settings)
    overview_service = OverviewService(database, settings)

    def _persist_connection(
        platform: Platform,
        token: TokenSet,
        scopes: list[str],
        scopes_source: str = "granted",
    ) -> ConnectedAccount:
        provider = registry.provider(platform)
        identity = provider.fetch_identity(token)
        token = _enrich_token(platform, token, identity.raw, identity.external_account_id)
        now = utcnow()
        with database.session() as session:
            account = session.scalar(
                select(ConnectedAccount).where(
                    ConnectedAccount.platform == platform.value,
                    ConnectedAccount.external_account_id == identity.external_account_id,
                )
            )
            if account is None:
                account = ConnectedAccount(
                    platform=platform.value,
                    external_account_id=identity.external_account_id,
                    token_ref=registry.token_store.new_token_ref(),
                )
                session.add(account)
            account.handle = identity.handle
            account.display_name = identity.display_name
            account.avatar_url = identity.avatar_url
            account.profile_url = identity.profile_url
            account.account_type = identity.account_type
            account.status = "connected"
            account.granted_scopes_json = json.dumps(scopes)
            account.connected_at = now
            account.last_verified_at = now
            account.last_error = None
            account.metadata_json = json.dumps(
                {"raw": identity.raw, "scopes_source": scopes_source}
            )
            session.flush()
            token_ref = str(account.token_ref)
            account_id = account.id
        registry.token_store.store(token_ref, token)
        with database.session() as session:
            persisted = session.get(ConnectedAccount, account_id)
            assert persisted is not None
            session.expunge(persisted)
            return persisted

    # ------------------------------------------------------------- read APIs

    @router.get("/platforms")
    def platforms() -> dict[str, object]:
        if settings.platform_provider == "mock":
            registry.ensure_mock_accounts()
        with database.session() as session:
            accounts = session.scalars(select(ConnectedAccount)).all()
            views = [_account_view(database, settings, account) for account in accounts]
        return {
            "provider_mode": settings.platform_provider,
            "platforms": [
                {
                    "platform": platform.value,
                    "configured": registry.provider(platform).configured(),
                    "required_env": REQUIRED_ENV.get(platform, []),
                    "capabilities": [
                        entry.model_dump(mode="json")
                        for entry in registry.provider(platform).capabilities()
                    ],
                    "accounts": [view for view in views if view["platform"] == platform.value],
                }
                for platform in Platform
            ],
        }

    @router.get("/platforms/accounts")
    def accounts() -> list[dict[str, object]]:
        if settings.platform_provider == "mock":
            registry.ensure_mock_accounts()
        with database.session() as session:
            records = session.scalars(select(ConnectedAccount)).all()
            return [_account_view(database, settings, account) for account in records]

    # ---------------------------------------------------------------- OAuth

    def _issue_connect(platform: Platform) -> dict[str, object]:
        if settings.platform_provider == "mock":
            raise HTTPException(
                status_code=400,
                detail="OAuth connect is unavailable in mock provider mode",
            )
        provider = registry.provider(platform)
        if not provider.configured():
            raise HTTPException(
                status_code=409,
                detail=f"{platform.value} OAuth is not configured",
            )
        use_pkce = platform is Platform.youtube
        issued = oauth_states.issue(
            platform,
            redirect_uri=registry.redirect_uri(platform),
            use_pkce=use_pkce,
        )
        authorization_url = provider.authorization_url(
            state=issued.state,
            redirect_uri=registry.redirect_uri(platform),
            code_challenge=(pkce_challenge(issued.code_verifier) if issued.code_verifier else None),
        )
        return {"authorization_url": authorization_url, "state": issued.state}

    @router.post("/platforms/{platform}/connect")
    def connect(platform: str) -> dict[str, object]:
        try:
            parsed = parse_platform(platform)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _issue_connect(parsed)

    @router.get("/platforms/{platform}/oauth/callback")
    def oauth_callback(
        platform: str,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> Response:
        web_base = settings.resolved_web_base_url

        def fail(short_code: str) -> RedirectResponse:
            return RedirectResponse(
                url=f"{web_base}/accounts?error={short_code}",
                status_code=303,
            )

        try:
            parsed = parse_platform(platform)
        except ValueError:
            return fail("unknown_platform")
        if error:
            return fail("denied" if error == "access_denied" else "provider_error")
        if not code or not state:
            return fail("missing_code")
        try:
            record = oauth_states.consume(parsed, state=state)
        except OAuthStateError as exc:
            return fail(str(exc))
        provider = registry.provider(parsed)
        try:
            token = provider.exchange_code(
                code=code,
                redirect_uri=record.redirect_uri,
                code_verifier=record.code_verifier,
            )
            _persist_connection(parsed, token, token.scopes)
        except ProviderError as exc:
            logger.warning("oauth callback failed for %s: %s", platform, exc.detail)
            return fail("exchange_failed")
        except Exception:  # noqa: BLE001
            logger.exception("oauth callback failed for %s", platform)
            return fail("exchange_failed")
        return RedirectResponse(
            url=f"{web_base}/accounts?connected={parsed.value}",
            status_code=303,
        )

    @router.post("/platforms/instagram/import-token")
    def import_instagram_token(payload: ImportTokenRequest) -> dict[str, object]:
        if settings.platform_provider == "mock":
            raise HTTPException(
                status_code=400,
                detail="token import is unavailable in mock provider mode",
            )
        provider = registry.provider(Platform.instagram)
        token = TokenSet(access_token=payload.access_token)
        try:
            provider.fetch_identity(token)
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=exc.detail) from exc
        if token.expires_at is None and isinstance(provider, InstagramProvider):
            with contextlib.suppress(ProviderError):
                token = provider.exchange_long_lived(token)
        scopes = list(dict.fromkeys(str(scope) for scope in payload.granted_scopes))
        account = _persist_connection(
            Platform.instagram, token, scopes, scopes_source="user_declared"
        )
        return {"account_id": account.id, "platform": "instagram"}

    # ----------------------------------------------------------- account ops

    def _account_or_404(account_id: int) -> ConnectedAccount:
        with database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            if account is None:
                raise HTTPException(status_code=404, detail="account not found")
            session.expunge(account)
            return account

    @router.post("/platforms/accounts/{account_id}/disconnect")
    def disconnect(account_id: int, payload: ConfirmedRequest) -> dict[str, object]:
        account = _account_or_404(account_id)
        if account.token_ref:
            token = registry.token_store.load(account.token_ref)
            if token is not None:
                try:
                    registry.provider(Platform(account.platform)).revoke(token)
                except ProviderError as exc:
                    logger.warning(
                        "token revoke failed for account %s: %s",
                        account_id,
                        exc.detail,
                    )
            registry.token_store.delete(account.token_ref)
        with database.session() as session:
            record = session.get(ConnectedAccount, account_id)
            if record is not None:
                record.status = "disconnected"
                record.token_ref = None
                record.last_error = None
        return {"account_id": account_id, "status": "disconnected"}

    @router.post("/platforms/accounts/{account_id}/reconnect")
    def reconnect(account_id: int) -> dict[str, object]:
        account = _account_or_404(account_id)
        return _issue_connect(Platform(account.platform))

    @router.post("/platforms/accounts/{account_id}/sync")
    def sync_account(account_id: int) -> Response:
        _account_or_404(account_id)
        try:
            run = sync_service.start_background(account_id, trigger="manual")
        except SyncRunInProgress as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(
            content=json.dumps({"run_id": run.id}),
            status_code=202,
            media_type="application/json",
        )

    @router.get("/platforms/sync-runs/{run_id}")
    def sync_run(run_id: int) -> dict[str, object]:
        with database.session() as session:
            run = session.get(SyncRun, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="sync run not found")
            return _run_view(run)

    @router.get("/platforms/accounts/{account_id}/sync-runs")
    def account_sync_runs(
        account_id: int, limit: int = Query(default=20, ge=1, le=100)
    ) -> list[dict[str, object]]:
        _account_or_404(account_id)
        with database.session() as session:
            runs = session.scalars(
                select(SyncRun)
                .where(SyncRun.account_id == account_id)
                .order_by(SyncRun.started_at.desc())
                .limit(limit)
            ).all()
            return [_run_view(run) for run in runs]

    # ---------------------------------------------------------- read services

    @router.get("/overview")
    def overview() -> dict[str, object]:
        if settings.platform_provider == "mock":
            registry.ensure_mock_accounts()
        with database.session() as session:
            records = session.scalars(select(ConnectedAccount)).all()
            views = [_account_view(database, settings, account) for account in records]
        return overview_service.summary(views)

    @router.get("/content")
    def content_list(
        platform: str | None = None,
        account_id: int | None = Query(default=None, ge=1),
        type: str | None = None,  # noqa: A002
        q: str | None = Query(default=None, max_length=200),
        sort: Literal["published_at", "view_count", "like_count", "comment_count"] = "published_at",
        order: Literal["asc", "desc"] = "desc",
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        return content_service.list_items(
            platform=platform,
            account_id=account_id,
            content_type=type,
            query=q,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )

    @router.get("/content/{content_id}")
    def content_detail(content_id: int) -> dict[str, object]:
        view = content_service.get_item(content_id)
        if view is None:
            raise HTTPException(status_code=404, detail="content not found")
        return view

    @router.post("/content/groups")
    def content_group_create(payload: GroupCreateRequest) -> dict[str, object]:
        return content_service.create_group(payload.title, payload.content_ids)

    @router.post("/content/{content_id}/group")
    def content_group_assign(content_id: int, payload: GroupAssignRequest) -> dict[str, object]:
        try:
            return content_service.set_group(content_id, payload.group_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/analytics/accounts/{account_id}")
    def account_analytics(
        account_id: int,
        range: Literal["7", "28", "90"] = "28",  # noqa: A002
    ) -> dict[str, object]:
        days = int(range)
        account = _account_or_404(account_id)
        with database.session() as session:
            start_day = datetime.now(UTC).date() - timedelta(days=days)
            metric_rows = session.scalars(
                select(AccountMetricDay)
                .where(
                    AccountMetricDay.account_id == account_id,
                    AccountMetricDay.day >= start_day,
                )
                .order_by(AccountMetricDay.day.asc())
            ).all()
            prior_start = start_day - timedelta(days=days)
            prior_rows = session.scalars(
                select(AccountMetricDay).where(
                    AccountMetricDay.account_id == account_id,
                    AccountMetricDay.day >= prior_start,
                    AccountMetricDay.day < start_day,
                )
            ).all()
            snapshot = session.scalar(
                select(AccountMetricSnapshot)
                .where(AccountMetricSnapshot.account_id == account_id)
                .order_by(AccountMetricSnapshot.captured_at.desc())
                .limit(1)
            )
            top = session.scalars(
                select(PlatformContent)
                .where(PlatformContent.account_id == account_id)
                .order_by(PlatformContent.view_count.desc())
                .limit(10)
            ).all()
        mean_metrics = {"averageViewDuration"}
        last_metrics = {"follower_count"}

        def metric_label(metric: str) -> str:
            return _METRIC_LABELS.get(metric, metric.replace("_", " ").title())

        def aggregate(metric: str, values: list[tuple[object, float]]) -> float | None:
            if not values:
                return None
            raw = [value for _key, value in values]
            if metric in mean_metrics:
                return sum(raw) / len(raw)
            if metric in last_metrics:
                return raw[-1]
            return sum(raw)

        series_by_metric: dict[str, list[dict[str, object]]] = {}
        values_by_metric: dict[str, list[tuple[object, float]]] = {}
        sources: set[str] = set()
        for row in metric_rows:
            sources.add(row.source)
            values_by_metric.setdefault(row.metric, []).append((row.day, row.value))
            series_by_metric.setdefault(row.metric, []).append(
                {"day": row.day.isoformat(), "value": row.value}
            )
        prior_values: dict[str, list[tuple[object, float]]] = {}
        for row in prior_rows:
            prior_values.setdefault(row.metric, []).append((row.day, row.value))
        kpis: list[dict[str, object]] = []
        for metric, points in series_by_metric.items():
            value = aggregate(metric, values_by_metric.get(metric, []))
            prior = aggregate(metric, prior_values.get(metric, []))
            kpis.append(
                {
                    "key": metric,
                    "label": metric_label(metric),
                    "value": value,
                    "delta": (value - prior) if value is not None and prior is not None else None,
                    "source": next(iter(sources), None),
                    "as_of": points[-1]["day"] if points else None,
                }
            )
        snapshot_start = datetime.combine(start_day, datetime.min.time(), tzinfo=UTC)
        with database.session() as session:
            snapshots = session.scalars(
                select(AccountMetricSnapshot)
                .where(AccountMetricSnapshot.account_id == account_id)
                .order_by(AccountMetricSnapshot.captured_at.asc())
            ).all()
        snapshot_series: list[dict[str, object]] = []
        snapshot_fields = (
            ("followers", "Followers"),
            ("total_views", "Total views"),
            ("total_likes", "Total likes"),
            ("content_count", "Content items"),
        )
        for field, label in snapshot_fields:
            before: list[tuple[datetime, int]] = []
            within: list[tuple[datetime, int]] = []
            for snap in snapshots:
                captured = snap.captured_at
                if captured.tzinfo is None:
                    captured = captured.replace(tzinfo=UTC)
                value = getattr(snap, field)
                if not isinstance(value, int):
                    continue
                if captured < snapshot_start:
                    before.append((captured, value))
                else:
                    within.append((captured, value))
            selected = before[-1:] + within
            if selected:
                snapshot_series.append(
                    {
                        "metric": field,
                        "label": label,
                        "source": "runway_sync_snapshots",
                        "points": [
                            {"captured_at": iso_utc(captured), "value": value}
                            for captured, value in selected
                        ],
                    }
                )
        notes: list[str] = []
        if account.platform == "youtube":
            notes.append(
                "Recent days may be incomplete; YouTube finalizes Analytics data "
                "with a delay of about two days."
            )
        elif account.platform == "instagram":
            notes.append(
                "Instagram insights are available only for the connected "
                "professional account and may lag the app."
            )
        elif account.platform == "tiktok":
            notes.append(
                "TikTok's Display API has no daily analytics; trends are built "
                "from Runway's own sync snapshots."
            )
        data_sources = []
        if "youtube_analytics_api" in sources:
            data_sources.append(
                {
                    "label": "YouTube Analytics API",
                    "url": "https://developers.google.com/youtube/analytics",
                }
            )
        if "instagram_insights_api" in sources:
            data_sources.append(
                {
                    "label": "Instagram Insights API",
                    "url": "https://developers.facebook.com/docs/instagram-platform",
                }
            )
        return {
            "account": {
                "id": account.id,
                "platform": account.platform,
                "handle": account.handle,
            },
            "range": days,
            "kpis": kpis,
            "series": [
                {"metric": metric, "label": metric_label(metric), "points": pts}
                for metric, pts in series_by_metric.items()
            ],
            "snapshot_series": snapshot_series,
            "top_content": [
                {
                    "id": item.id,
                    "platform": item.platform,
                    "content_type": item.content_type,
                    "title": item.title,
                    "caption": item.caption,
                    "thumbnail_url": item.thumbnail_url,
                    "view_count": item.view_count,
                    "like_count": item.like_count,
                    "comment_count": item.comment_count,
                    "published_at": (iso_utc(item.published_at)),
                    "permalink": item.permalink,
                }
                for item in top
            ],
            "notes": notes,
            "data_sources": data_sources,
            "latest_snapshot": (
                {
                    "followers": snapshot.followers,
                    "captured_at": iso_utc(snapshot.captured_at),
                }
                if snapshot
                else None
            ),
        }

    @router.get("/inbox")
    def inbox_list(
        platform: str | None = None,
        account_id: int | None = Query(default=None, ge=1),
        status: str | None = None,
        unanswered: bool = False,
        content_id: int | None = Query(default=None, ge=1),
        q: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        return inbox_service.list_comments(
            platform=platform,
            account_id=account_id,
            status=status,
            unanswered=unanswered,
            content_id=content_id,
            query=q,
            limit=limit,
            offset=offset,
        )

    @router.get("/inbox/{comment_id}")
    def inbox_detail(comment_id: int) -> dict[str, object]:
        view = inbox_service.get_comment(comment_id)
        if view is None:
            raise HTTPException(status_code=404, detail="comment not found")
        return view

    @router.patch("/inbox/{comment_id}")
    def inbox_set_status(comment_id: int, payload: InboxStatusRequest) -> dict[str, object]:
        try:
            return inbox_service.set_status(comment_id, payload.status)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.put("/inbox/{comment_id}/draft")
    def inbox_set_draft(comment_id: int, payload: DraftTextRequest) -> dict[str, object]:
        try:
            return inbox_service.set_draft(comment_id, payload.text)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/inbox/{comment_id}/draft")
    def inbox_delete_draft(comment_id: int) -> dict[str, object]:
        return inbox_service.delete_draft(comment_id)

    @router.post("/inbox/{comment_id}/reply")
    def inbox_reply(comment_id: int, payload: ReplyRequest) -> dict[str, object]:
        try:
            return inbox_service.reply(comment_id, payload.text)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InboxReplyForbidden as exc:
            raise HTTPException(status_code=403, detail=exc.detail) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=exc.detail) from exc

    @router.get("/drafts")
    def drafts_list(status: str | None = None) -> list[dict[str, object]]:
        return draft_service.list_drafts(status=status)

    @router.post("/drafts")
    def drafts_create(payload: DraftCreateRequest) -> dict[str, object]:
        return draft_service.create_draft(
            title=payload.title,
            body=payload.body,
            scheduled_for=payload.scheduled_for,
            notes=payload.notes,
            targets=[target.model_dump() for target in payload.targets],
        )

    @router.patch("/drafts/{draft_id}")
    def drafts_update(draft_id: int, payload: DraftUpdateRequest) -> dict[str, object]:
        try:
            return draft_service.update_draft(
                draft_id,
                title=payload.title,
                body=payload.body,
                scheduled_for=payload.scheduled_for,
                clear_schedule=payload.clear_schedule,
                notes=payload.notes,
                status=payload.status,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/drafts/{draft_id}")
    def drafts_archive(draft_id: int) -> dict[str, object]:
        try:
            return draft_service.archive_draft(draft_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/drafts/{draft_id}/targets/{target_id}/mark-published")
    def drafts_mark_published(
        draft_id: int, target_id: int, payload: MarkPublishedRequest
    ) -> dict[str, object]:
        try:
            return draft_service.mark_published(draft_id, target_id, payload.published_url)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
