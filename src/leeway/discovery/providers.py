from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Protocol
from urllib.parse import quote_plus, urlparse

import httpx
from playwright.async_api import async_playwright

from leeway.analysis.schemas import SearchPlan
from leeway.config import Settings
from leeway.discovery.schemas import ImageSearchResult, SearchPage


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
    challenge_markers = ("captcha", "unusual traffic", "verify", "consent", "choose an account")

    def __init__(self, settings: Settings, *, live: bool):
        if not settings.enable_browser_search:
            raise ValueError("browser discovery requires LEEWAY_ENABLE_BROWSER_SEARCH=true")
        if not live:
            raise ValueError("browser discovery requires the explicit --live flag")
        self.settings = settings

    async def search(self, plan: SearchPlan, cursor: str | None = None) -> SearchPage:
        results: list[ImageSearchResult] = []
        profile = self.settings.browser_profile_dir / "discovery"
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile), headless=False, viewport={"width": 1440, "height": 1000}
            )
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                for query in _queries(plan):
                    url = self.settings.browser_search_url.format(query=quote_plus(query))
                    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    sample = (page.url + " " + (await page.title())).lower()
                    body = (await page.locator("body").inner_text())[:2000].lower()
                    if any(marker in sample + body for marker in self.challenge_markers):
                        await asyncio.to_thread(
                            input,
                            "Search challenge detected. Resolve it manually, then press Enter: ",
                        )
                    images = await page.locator("img").evaluate_all(
                        """nodes => nodes.map((img, index) => ({
                          src: img.currentSrc || img.src,
                          width: img.naturalWidth,
                          height: img.naturalHeight,
                          page: img.closest('a')?.href || location.href,
                          index
                        })).filter(
                          x => /^https?:/.test(x.src) && x.width >= 300 && x.height >= 300
                        )"""
                    )
                    for item in images[:20]:
                        direct = str(item["src"])
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
                                provider_metadata={"headed_browser": True},
                            )
                        )
            finally:
                await context.close()
        return SearchPage(results=results)


class ApiSearchProvider:
    name = "api"

    def __init__(self, settings: Settings):
        if not settings.search_api_url or not settings.search_api_key:
            raise ValueError("API discovery requires LEEWAY_SEARCH_API_URL and key")
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
