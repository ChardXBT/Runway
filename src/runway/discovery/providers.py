from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from contextlib import suppress
from typing import Any, Protocol
from urllib.parse import quote_plus, urlparse

import httpx
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from runway.analysis.schemas import SearchPlan
from runway.config import Settings
from runway.discovery.safety import is_known_adult_result, is_likely_query_match
from runway.discovery.schemas import ImageSearchResult, SearchPage


class SearchProvider(Protocol):
    name: str

    async def search(self, plan: SearchPlan, cursor: str | None = None) -> SearchPage: ...


class EnsembleSearchProvider:
    """Merge independent providers without hiding failures or duplicate provenance."""

    name = "ensemble"

    def __init__(self, providers: Sequence[SearchProvider], *, max_results: int = 100):
        if not providers:
            raise ValueError("search ensemble requires at least one provider")
        names = [provider.name for provider in providers]
        if len(set(names)) != len(names):
            raise ValueError("search ensemble provider names must be unique")
        self.providers = list(providers)
        self.max_results = max(1, max_results)
        self.last_diagnostics: dict[str, object] = {}

    async def search(self, plan: SearchPlan, cursor: str | None = None) -> SearchPage:
        outcomes = await asyncio.gather(
            *(provider.search(plan, cursor=cursor) for provider in self.providers),
            return_exceptions=True,
        )
        merged: dict[str, ImageSearchResult] = {}
        contributions: dict[str, int] = {}
        errors: dict[str, str] = {}
        for provider, outcome in zip(self.providers, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                errors[provider.name] = f"{type(outcome).__name__}: {outcome}"
                continue
            contributions[provider.name] = len(outcome.results)
            for result in outcome.results:
                key = result.direct_image_url.strip()
                if not key:
                    continue
                existing = merged.get(key)
                if existing is None:
                    metadata = dict(result.provider_metadata)
                    metadata.update(
                        {
                            "ensemble_primary_provider": provider.name,
                            "ensemble_contributors": [provider.name],
                            "provider_original_rank": result.result_rank,
                        }
                    )
                    merged[key] = result.model_copy(update={"provider_metadata": metadata})
                else:
                    metadata = dict(existing.provider_metadata)
                    raw_contributors = metadata.get("ensemble_contributors", [])
                    contributor_values = (
                        raw_contributors if isinstance(raw_contributors, (list, tuple, set)) else []
                    )
                    contributors = {str(value) for value in contributor_values if str(value)}
                    contributors.add(provider.name)
                    metadata["ensemble_contributors"] = sorted(contributors)
                    merged[key] = existing.model_copy(update={"provider_metadata": metadata})
        if not merged and errors:
            raise RuntimeError(
                "all search ensemble providers failed: "
                + "; ".join(f"{name}: {error}" for name, error in sorted(errors.items()))
            )
        ordered = sorted(
            merged.values(),
            key=lambda row: (
                self._rank(row),
                str(row.provider_metadata.get("ensemble_primary_provider", "")),
                row.direct_image_url,
            ),
        )[: self.max_results]
        results = [
            row.model_copy(update={"result_rank": index}) for index, row in enumerate(ordered, 1)
        ]
        self.last_diagnostics = {
            "providers_attempted": [provider.name for provider in self.providers],
            "provider_contributions": contributions,
            "provider_errors": errors,
            "deduplicated_result_count": len(results),
        }
        return SearchPage(results=results)

    @staticmethod
    def _rank(result: ImageSearchResult) -> int:
        value = result.provider_metadata.get("provider_original_rank", result.result_rank)
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return result.result_rank
        try:
            return int(value)
        except ValueError:
            return result.result_rank


class PageImageExtractor(Protocol):
    async def extract(self, page_url: str) -> list[ImageSearchResult]: ...


def _queries(plan: SearchPlan) -> list[str]:
    return [query for family in plan.query_families for query in family.queries]


def _diverse_queries(plan: SearchPlan) -> list[str]:
    """Round-robin query families so a small live search still varies composition."""
    families = [list(family.queries) for family in plan.query_families if family.queries]
    queries: list[str] = []
    depth = 0
    while any(depth < len(family) for family in families):
        for family in families:
            if depth < len(family):
                queries.append(family[depth])
        depth += 1
    return queries


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
                for query in _diverse_queries(plan)[: self.settings.browser_search_max_queries]:
                    url = self.settings.browser_search_url.format(query=quote_plus(query))
                    if "bing.com/" in url and "safesearch=" not in url.casefold():
                        separator = "&" if "?" in url else "?"
                        url = f"{url}{separator}safeSearch=Strict"
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
                    result_links = page.locator("a.iusc[m]")
                    with suppress(PlaywrightTimeoutError):
                        await result_links.first.wait_for(state="attached", timeout=15_000)
                    bing_metadata = await result_links.evaluate_all(
                        """nodes => nodes.map(node => node.getAttribute('m')).filter(Boolean)"""
                    )
                    structured_images = self._parse_bing_metadata(bing_metadata)
                    generic_images: list[dict[str, Any]] = []
                    if not structured_images:
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
                    images = structured_images or generic_images
                    remaining = self.settings.browser_search_max_results - len(results)
                    if remaining <= 0:
                        break
                    per_query = min(
                        self.settings.browser_search_results_per_query,
                        remaining,
                    )
                    added = 0
                    for item in images:
                        direct = str(item["src"])
                        if direct in seen_direct_urls:
                            continue
                        seen_direct_urls.add(direct)
                        source_page = str(item["page"])
                        result = ImageSearchResult(
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
                                "result_title": str(item.get("title", "")),
                            },
                        )
                        if is_known_adult_result(result) or not is_likely_query_match(result):
                            continue
                        results.append(result)
                        added += 1
                        if added >= per_query:
                            break
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
                    "title": str(item.get("t") or item.get("desc") or ""),
                }
            )
        return images


class FrinkiacSearchProvider:
    """Public Simpsons-frame fallback; all returned frames still pass canonical safeguards."""

    name = "frinkiac"
    api_url = "https://frinkiac.com/api/search"
    character_markers = (
        "Homer",
        "Marge",
        "Bart",
        "Lisa",
        "Maggie",
        "Krusty",
        "Burns",
        "Smithers",
        "Flanders",
        "Milhouse",
        "Nelson",
        "Moe",
    )
    action_markers = (
        "arguing",
        "barbecue",
        "dancing",
        "eating",
        "fishing",
        "grocery",
        "laughing",
        "reading",
        "running",
        "saxophone",
        "shopping",
        "skateboard",
        "television",
    )

    def __init__(self, settings: Settings, *, sample_offset: int = 0):
        self.settings = settings
        self.sample_offset = max(0, sample_offset)

    async def search(self, plan: SearchPlan, cursor: str | None = None) -> SearchPage:
        del cursor
        results: list[ImageSearchResult] = []
        seen_frames: set[tuple[str, int]] = set()
        queries = _diverse_queries(plan)[: self.settings.browser_search_max_queries]
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for query_index, original_query in enumerate(queries):
                compact_query = self._compact_query(original_query)
                response = await client.get(self.api_url, params={"q": compact_query})
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    continue
                added = 0
                for raw in self._spread_sample(
                    payload,
                    self.settings.browser_search_results_per_query,
                    offset=self.sample_offset + query_index,
                ):
                    if not isinstance(raw, dict):
                        continue
                    episode = str(raw.get("Episode") or "").strip()
                    timestamp = raw.get("Timestamp")
                    if (
                        not re.fullmatch(r"S\d{2}E\d{2}", episode)
                        or isinstance(timestamp, bool)
                        or not isinstance(timestamp, int)
                        or timestamp < 0
                        or (episode, timestamp) in seen_frames
                    ):
                        continue
                    seen_frames.add((episode, timestamp))
                    source_page = f"https://frinkiac.com/caption/{episode}/{timestamp}"
                    direct = (
                        f"https://frinkiac.com/img/{episode}/{timestamp}.jpg"
                        f"?cb={episode}-{timestamp}"
                    )
                    results.append(
                        ImageSearchResult(
                            search_query=original_query,
                            result_rank=len(results) + 1,
                            source_page_url=source_page,
                            direct_image_url=direct,
                            source_domain="frinkiac.com",
                            rights_status="unknown",
                            provider_metadata={
                                "source_adapter": "frinkiac-public-search",
                                "frinkiac_query": compact_query,
                                "episode": episode,
                                "timestamp": timestamp,
                                "subtitle": str(raw.get("Content") or ""),
                                "episode_title": str(raw.get("Title") or ""),
                            },
                        )
                    )
                    added += 1
                    if (
                        added >= self.settings.browser_search_results_per_query
                        or len(results) >= self.settings.browser_search_max_results
                    ):
                        break
                if len(results) >= self.settings.browser_search_max_results:
                    break
        return SearchPage(results=results)

    @staticmethod
    def _spread_sample(
        values: list[Any],
        count: int,
        *,
        offset: int = 0,
    ) -> list[Any]:
        if count <= 0 or not values:
            return []
        if len(values) <= count:
            return list(values)
        if count == 1:
            return [values[offset % len(values)]]
        indexes = [round(position * (len(values) - 1) / (count - 1)) for position in range(count)]
        shift = (max(0, offset) * count) % len(values)
        indexes = [(index + shift) % len(values) for index in indexes]
        return [values[index] for index in indexes]

    @classmethod
    def _compact_query(cls, value: str) -> str:
        words = set(re.sub(r"[^a-z]+", " ", value.casefold()).split())
        characters = [marker for marker in cls.character_markers if marker.casefold() in words]
        actions = [marker for marker in cls.action_markers if marker in words]
        selected = [*characters[:1], *actions[:1]]
        if selected:
            return " ".join(selected)
        fallback = [
            word
            for word in re.sub(r"[^a-z0-9]+", " ", value.casefold()).split()
            if word
            not in {
                "animated",
                "frame",
                "scene",
                "screencap",
                "screenshot",
                "simpson",
                "simpsons",
                "still",
                "the",
            }
        ]
        return " ".join(fallback[:2]) or "Springfield"


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
