"""Account views and per-account analytics shared by the API routes and the MCP tools.

Moved unchanged from `runway.api.platform_routes` so the agent interface (spec 06
section 7) calls the same functions the web app's routes call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    AccountMetricDay,
    AccountMetricSnapshot,
    ConnectedAccount,
    PlatformContent,
)
from runway.platforms.base import iso_utc
from runway.platforms.capabilities import (
    YOUTUBE_ENGAGE_SCOPE,
    Capability,
    Platform,
    capability_matrix,
    capability_ready,
)
from runway.platforms.overview import account_freshness, metric_window_start

METRIC_LABELS = {
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


def blocked_by_setting(
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


def account_view(
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
        "freshness": account_freshness(account.last_sync_at),
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
                "blocked_by_setting": blocked_by_setting(
                    entry, Platform(account.platform), granted_scopes, settings
                ),
                "detail": entry.detail,
            }
            for entry in entries
        ],
        "latest_snapshot": latest_snapshot,
    }


def account_analytics_view(
    database: Database, account: ConnectedAccount, days: int
) -> dict[str, object]:
    """The `/api/analytics/accounts/{id}` document for one account over `days` days."""
    account_id = account.id
    with database.session() as session:
        start_day = metric_window_start(days)
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
        return METRIC_LABELS.get(metric, metric.replace("_", " ").title())

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
