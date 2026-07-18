from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Protocol
from urllib.parse import quote_plus, urlparse

import httpx
from playwright.async_api import async_playwright

from runway.analysis.schemas import SearchPlan
from runway.config import Settings
from runway.discovery.schemas import ImageSearchResult, SearchPage


class SearchProvider(Protocol):
    name: str

    async def search(self, plan: SearchPlan, cursor: str | None = None) -> SearchPage: ...


class PageImageExtractor(Protocol):
    async def extract(self, page_url: str) -> list[ImageSearchResult]: ...


def _queries(plan: SearchPlan) -> list[str]:
    return [query for family in plan.query_families for query in family.queries]


class FixtureSearchProvider:
    name = "fixture"

    def __init__(self, count: int = 30):
        self.count = count

    async def search(self, plan: SearchPlan, cursor: str | None = None) -> SearchPage:
        queries = _queries(plan) or ["synthetic fixture"]
        start = int(cursor or 0)
        results = [
            ImageSearchResult(
                search_query=queries[index % len(queries)],
                result_rank=index + 1,
                source_page_url=f"https://fixture.local/gallery/candidate-{index + 1:02d}",
                direct_image_url=f"fixture://candidate-{index + 1:02d}",
                source_domain="fixture.local",
                original_width=720 + ((index + 1) % 3) * 80,
                original_height=540 + ((index + 1) % 4) * 40,
                rights_status="creator_owned",
                provider_metadata={"fixture": True, "cursor": start},
            )
            for index in range(start, min(start + self.count, 36))
        ]
        return SearchPage(results=results, next_cursor=None)


class ManualUrlProvider:
    name = "manual"

    def __init__(self, urls: Sequence[str]):
        self.urls = list(urls)

    async def search(self, plan: SearchPlan, cursor: str | None = None) -> SearchPage:
        query = _queries(plan)[0] if _queries(plan) else "manual URL"
        results = []
        for index, url in enumerate(self.urls, start=1):
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                raise ValueError("manual image URLs must use HTTP or HTTPS")
            results.append(
                ImageSearchResult(
                    search_query=query,
                    result_rank=index,
                    source_page_url=url,
                    direct_image_url=url,
                    source_domain=parsed.netloc,
                    rights_status="unknown",
                    provider_metadata={"manual": True},
                )
            )
        return SearchPage(results=results)


class BrowserSearchProvider:
    """Experimental, headed, user-initiated provider with no stealth or challenge bypass."""

    name = "browser"
    challenge_url_markers = (
        "consent.google.",
        "/sorry/",
        "/challenge/",
    )
    challenge_text_markers = (
        "our systems have detected unusual traffic",
        "prove you're not a robot",
        "verify you are human",
        "complete the captcha",
        "before you continue to google",
    )

    def __init__(self, settings: Settings, *, live: bool):
        if not settings.enable_browser_search:
            raise ValueError("browser discovery requires RUNWAY_ENABLE_BROWSER_SEARCH=true")
        if not live:
            raise ValueError("browser discovery requires the explicit --live flag")
        self.settings = settings

    async def search(self, plan: SearchPlan, cursor: str | None = None) -> SearchPage:
        results: list[ImageSearchResult] = []
        seen_direct_urls: set[str] = set()
        profile = self.settings.browser_profile_dir / "discovery"
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile), headless=False, viewport={"width": 1440, "height": 1000}
            )
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                for query in _queries(plan)[: self.settings.browser_search_max_queries]:
                    url = self.settings.browser_search_url.format(query=quote_plus(query))
                    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    sample = (page.url + " " + (await page.title())).lower()
                    body = (await page.locator("body").inner_text())[:2000].lower()
                    if any(
                        marker in page.url.lower() for marker in self.challenge_url_markers
                    ) or any(marker in sample + body for marker in self.challenge_text_markers):
                        await page.screenshot(
                            path=str(
                                self.settings.resolved_data_dir
                                / "captures"
                                / "browser-search-challenge.png"
                            ),
                            full_page=True,
                        )
                        raise RuntimeError(
                            "search challenge or consent page detected; resolve it manually "
                            "in the persistent discovery browser profile, then rerun"
                        )
                    bing_metadata = await page.locator("a.iusc[m]").evaluate_all(
                        """nodes => nodes.map(node => node.getAttribute('m')).filter(Boolean)"""
                    )
                    structured_images = self._parse_bing_metadata(bing_metadata)
                    generic_images = await page.locator("img").evaluate_all(
                        """nodes => nodes.map((img, index) => ({
                          src: img.currentSrc || img.src,
                          width: img.naturalWidth,
                          height: img.naturalHeight,
                          page: img.closest('a')?.href || location.href,
                          index,
                          adapter: 'generic-img'
                        })).filter(
                          x => /^https?:/.test(x.src) && x.width >= 300 && x.height >= 300
                        )"""
                    )
                    images = [*structured_images, *generic_images]
                    remaining = self.settings.browser_search_max_results - len(results)
                    if remaining <= 0:
                        break
                    per_query = min(
                        self.settings.browser_search_results_per_query,
                        remaining,
                    )
                    for item in images[:per_query]:
                        direct = str(item["src"])
                        if direct in seen_direct_urls:
                            continue
                        seen_direct_urls.add(direct)
                        source_page = str(item["page"])
                        results.append(
                            ImageSearchResult(
                                search_query=query,
                                result_rank=len(results) + 1,
                                source_page_url=source_page,
                                direct_image_url=direct,
                                source_domain=urlparse(source_page).netloc,
                                original_width=int(item["width"]),
                                original_height=int(item["height"]),
                                rights_status="unknown",
                                provider_metadata={
                                    "headed_browser": True,
                                    "source_adapter": str(item.get("adapter", "unknown")),
                                },
                            )
                        )
                    if len(results) >= self.settings.browser_search_max_results:
                        break
            finally:
                await context.close()
        return SearchPage(results=results)

    @staticmethod
    def _parse_bing_metadata(values: list[str]) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        for index, raw in enumerate(values):
            try:
                item = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            direct = str(item.get("murl") or "")
            if not direct.startswith(("http://", "https://")):
                continue
            source_page = str(item.get("purl") or "")
            if not source_page.startswith(("http://", "https://")):
                source_page = direct
            images.append(
                {
                    "src": direct,
                    "page": source_page,
                    "width": int(item.get("ow") or 0),
                    "height": int(item.get("oh") or 0),
                    "index": index,
                    "adapter": "bing-metadata",
                }
            )
        return images


class ApiSearchProvider:
    name = "api"

    def __init__(self, settings: Settings):
        if not settings.search_api_url or not settings.search_api_key:
            raise ValueError("API discovery requires RUNWAY_SEARCH_API_URL and key")
        self.url = settings.search_api_url
        self.key = settings.search_api_key

    async def search(self, plan: SearchPlan, cursor: str | None = None) -> SearchPage:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.url,
                headers={"Authorization": f"Bearer {self.key}"},
                json={"queries": _queries(plan), "cursor": cursor},
            )
            response.raise_for_status()
            payload = response.json()
        return SearchPage.model_validate(payload)
