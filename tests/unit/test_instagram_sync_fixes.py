"""Instagram sync fixes (research/recheck-20261123 §3).

Two Meta behaviors the sync path must respect:

- `total_*` values (insights `total_interactions`, `total_like_count`,
  `total_comments_count`, `total_views_count`) aggregate Instagram,
  Facebook crossposts and boosted media — they must never be stored as an
  Instagram metric, so they are neither requested nor accepted.
- Since 2026-07-30 `media_url` is omitted for videos with copyrighted or
  licensed audio; the sync parser treats it as optional and falls back to
  `thumbnail_url`, then `permalink`.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx

from runway.config import Settings
from runway.platforms.base import TokenSet
from runway.platforms.instagram import InstagramProvider


def _provider(handler) -> InstagramProvider:
    settings = Settings(instagram_app_id="ig-app", instagram_app_secret="ig-secret")
    return InstagramProvider(
        settings, httpx.Client(transport=httpx.MockTransport(handler), timeout=20.0)
    )


def _token() -> TokenSet:
    return TokenSet(access_token="ig-access", extra={"user_id": "17841400923817"})


# --------------------------------------------------------------- total_* rule


def test_content_insights_neither_request_nor_store_total_metrics() -> None:
    seen_metrics: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)
        if request.url.path.endswith("/insights") and "metric" in query:
            seen_metrics.extend(query["metric"])
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"name": "views", "values": [{"value": 10}]},
                        # Meta answers it if asked; a total_* value must still
                        # never become an Instagram metric.
                        {"name": "total_interactions", "values": [{"value": 99}]},
                        {"name": "total_like_count", "values": [{"value": 7}]},
                    ]
                },
            )
        return httpx.Response(200, json={"media_type": "REELS", "media_product_type": "REELS"})

    provider = _provider(handler)
    metrics = provider.fetch_content_metrics(_token(), ["m1"])
    assert metrics["m1"] == {"views": 10}
    for metric in seen_metrics:
        assert "total_" not in metric


def test_account_totals_exclude_total_star_names() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)
        assert "total_" not in " ".join(query.get("metric", []))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"name": "views", "total_value": {"value": 500}},
                    {"name": "profile_views", "total_value": {"value": 40}},
                    {"name": "total_interactions", "total_value": {"value": 12}},
                ]
            },
        )

    provider = _provider(handler)
    totals = provider.fetch_account_totals(_token())
    assert totals == {"views": 500, "profile_views": 40}
    assert not any(name.startswith("total_") for name in totals)


def test_daily_metrics_drop_total_star_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "reach",
                        "values": [{"value": 12, "end_time": "2026-11-30T00:00:00+0000"}],
                    },
                    {
                        "name": "total_views_count",
                        "values": [{"value": 900, "end_time": "2026-11-30T00:00:00+0000"}],
                    },
                ]
            },
        )

    provider = _provider(handler)
    metrics = provider.fetch_account_daily_metrics(
        _token(), since=date(2026, 11, 29), until=date(2026, 11, 30)
    )
    assert [metric.metric for metric in metrics] == ["reach"]


# ----------------------------------------------------------- media_url fallback


def test_media_url_falls_back_to_thumbnail_and_permalink() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "v1",
                        "media_type": "VIDEO",
                        "media_product_type": "REELS",
                        # licensed audio: media_url omitted, no thumbnail either
                        "permalink": "https://www.instagram.com/reel/v1/",
                        "timestamp": "2026-11-30T10:00:00+0000",
                    },
                    {
                        "id": "v2",
                        "media_type": "VIDEO",
                        "media_product_type": "REELS",
                        "thumbnail_url": "https://img/thumb2.jpg",
                        "permalink": "https://www.instagram.com/reel/v2/",
                        "timestamp": "2026-11-30T11:00:00+0000",
                    },
                ]
            },
        )

    provider = _provider(handler)
    page = provider.list_content(_token(), cursor=None, limit=10)
    first, second = page.items
    assert second.thumbnail_url == "https://img/thumb2.jpg"
    assert first.thumbnail_url == "https://www.instagram.com/reel/v1/"
