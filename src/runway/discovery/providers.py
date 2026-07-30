from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from contextlib import suppress
from typing import Any, ClassVar, Protocol
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


class DuckDuckGoSearchProvider:
    """Keyless public-web image discovery without a persistent browser window."""

    name = "duckduckgo"
    search_url = "https://duckduckgo.com/"
    image_api_url = "https://duckduckgo.com/i.js"
    token_patterns = (
        re.compile(r"""vqd=["']([\d-]+)"""),
        re.compile(r"vqd=([\d-]+)"),
    )

    def __init__(self, settings: Settings, *, sample_offset: int = 0):
        if not settings.enable_browser_search:
            raise ValueError("public-web discovery requires RUNWAY_ENABLE_BROWSER_SEARCH=true")
        self.settings = settings
        self.sample_offset = max(0, sample_offset)
        self.last_diagnostics: dict[str, object] = {}

    async def search(self, plan: SearchPlan, cursor: str | None = None) -> SearchPage:
        del cursor
        results: list[ImageSearchResult] = []
        seen_direct_urls: set[str] = set()
        queries = self._balanced_queries(plan)
        query_errors: dict[str, str] = {}
        headers = {
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/126 Safari/537.36"
            ),
        }
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers=headers,
        ) as client:
            for query_index, query in enumerate(queries):
                request_query = self._request_query(query)
                try:
                    token = await self._token(client, request_query)
                    response = await client.get(
                        self.image_api_url,
                        params={
                            "l": "us-en",
                            "o": "json",
                            "q": request_query,
                            "vqd": token,
                        },
                        headers={"Referer": "https://duckduckgo.com/"},
                    )
                    response.raise_for_status()
                except (httpx.HTTPError, RuntimeError) as exc:
                    query_errors[query] = f"{type(exc).__name__}: {exc}"
                    continue
                try:
                    payload = response.json()
                except json.JSONDecodeError as exc:
                    raise RuntimeError("public image search returned malformed JSON") from exc
                raw_results = payload.get("results", []) if isinstance(payload, dict) else []
                if not isinstance(raw_results, list):
                    raise RuntimeError("public image search returned an invalid result list")
                per_query = min(
                    self.settings.browser_search_results_per_query,
                    self.settings.browser_search_max_results - len(results),
                )
                if per_query <= 0:
                    break
                ordered = self._rotated_results(
                    raw_results,
                    per_query=per_query,
                    offset=self.sample_offset + query_index,
                )
                added = 0
                for item in ordered:
                    if not isinstance(item, dict):
                        continue
                    direct = str(item.get("image") or "").strip()
                    source_page = str(item.get("url") or "").strip()
                    if (
                        not direct.startswith(("http://", "https://"))
                        or not source_page.startswith(("http://", "https://"))
                        or direct in seen_direct_urls
                    ):
                        continue
                    seen_direct_urls.add(direct)
                    result = ImageSearchResult(
                        search_query=query,
                        result_rank=len(results) + 1,
                        source_page_url=source_page,
                        direct_image_url=direct,
                        source_domain=urlparse(source_page).netloc,
                        original_width=self._positive_int(item.get("width")),
                        original_height=self._positive_int(item.get("height")),
                        rights_status="unknown",
                        provider_metadata={
                            "source_adapter": "duckduckgo-images-json",
                            "result_title": str(item.get("title") or ""),
                            "thumbnail_url": str(item.get("thumbnail") or ""),
                            "query_offset": self.sample_offset,
                            "provider_request_query": request_query,
                        },
                    )
                    if is_known_adult_result(result) or not is_likely_query_match(result):
                        continue
                    results.append(result)
                    added += 1
                    if added >= per_query:
                        break
        self.last_diagnostics = {
            "queries_requested": queries,
            "query_errors": query_errors,
            "result_count": len(results),
            "sample_offset": self.sample_offset,
        }
        if not results and query_errors:
            raise RuntimeError(
                "all public image queries failed: "
                + "; ".join(f"{query}: {error}" for query, error in sorted(query_errors.items()))
            )
        return SearchPage(results=results)

    async def _token(self, client: httpx.AsyncClient, query: str) -> str:
        response = await client.get(self.search_url, params={"q": query, "kp": "1"})
        response.raise_for_status()
        for pattern in self.token_patterns:
            match = pattern.search(response.text)
            if match:
                return match.group(1)
        raise RuntimeError("public image search did not provide a request token")

    def _balanced_queries(self, plan: SearchPlan) -> list[str]:
        queries = _diverse_queries(plan)
        limit = min(self.settings.browser_search_max_queries, len(queries))
        if limit <= 0:
            return []
        topics = [
            value.strip()
            for value in [
                *(plan.desired_topics[:1]),
                *self.settings.discovery_secondary_topic_list,
            ]
            if value.strip()
        ]
        if len(topics) <= 1:
            return queries[:limit]
        primary, *secondary_topics = topics
        primary_queries = [query for query in queries if self._mentions_topic(query, primary)]
        secondary_queries = {
            topic: [query for query in queries if self._mentions_topic(query, topic)]
            for topic in secondary_topics
        }
        primary_count = max(
            1,
            min(limit, round(limit * self.settings.discovery_primary_topic_query_share)),
        )
        chosen = self._rotated_unique(
            primary_queries,
            primary_count,
            self.sample_offset,
        )
        remaining = limit - len(chosen)
        for index in range(remaining):
            topic = secondary_topics[(self.sample_offset + index) % len(secondary_topics)]
            pool = secondary_queries.get(topic, [])
            candidate_rows = self._rotated_unique(pool, 1, self.sample_offset + index)
            if candidate_rows and candidate_rows[0] not in chosen:
                chosen.append(candidate_rows[0])
        for query in queries:
            if len(chosen) >= limit:
                break
            if query not in chosen:
                chosen.append(query)
        return chosen[:limit]

    @staticmethod
    def _request_query(value: str) -> str:
        words = [
            word
            for word in re.sub(r"[^a-zA-Z0-9]+", " ", value).split()
            if word.casefold() not in {"a", "an", "in", "of", "on", "the"}
        ]
        if len(words) <= 9:
            return " ".join(words)
        format_markers = {"frame", "screencap", "screenshot", "still"}
        tail = next(
            (word for word in reversed(words) if word.casefold() in format_markers),
            None,
        )
        body = [word for word in words if tail is None or word != tail][: 8 if tail else 9]
        return " ".join([*body, *([tail] if tail else [])])

    @staticmethod
    def _mentions_topic(query: str, topic: str) -> bool:
        normalized_query = re.sub(r"[^a-z0-9]+", " ", query.casefold())
        normalized_topic = re.sub(r"[^a-z0-9]+", " ", topic.casefold()).strip()
        return bool(normalized_topic and normalized_topic in normalized_query)

    @staticmethod
    def _rotated_unique(values: Sequence[str], count: int, offset: int) -> list[str]:
        unique = list(dict.fromkeys(values))
        if not unique or count <= 0:
            return []
        start = (max(0, offset) * max(1, count)) % len(unique)
        return [unique[(start + index) % len(unique)] for index in range(min(count, len(unique)))]

    @staticmethod
    def _rotated_results(
        values: list[Any],
        *,
        per_query: int,
        offset: int,
    ) -> list[Any]:
        if not values or per_query <= 0:
            return []
        start = (max(0, offset) * per_query) % len(values)
        return [values[(start + index) % len(values)] for index in range(len(values))]

    @staticmethod
    def _positive_int(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if not isinstance(value, (str, int, float)):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None


class FrinkiacSearchProvider:
    """Public Simpsons-frame fallback; all returned frames still pass canonical safeguards."""

    name = "frinkiac"
    api_url = "https://frinkiac.com/api/search"
    base_url = "https://frinkiac.com"
    source_domain = "frinkiac.com"
    source_adapter = "frinkiac-public-search"
    franchise_markers: ClassVar[tuple[str, ...]] = ("simpson",)
    character_markers: ClassVar[tuple[str, ...]] = (
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
    action_markers: ClassVar[tuple[str, ...]] = (
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
        eligible_queries = [
            query for query in _diverse_queries(plan) if self._supports_query(query)
        ]
        queries = eligible_queries[: self.settings.browser_search_max_queries]
        if not queries:
            return SearchPage(results=[])
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
                    source_page = f"{self.base_url}/caption/{episode}/{timestamp}"
                    direct = (
                        f"{self.base_url}/img/{episode}/{timestamp}.jpg?cb={episode}-{timestamp}"
                    )
                    results.append(
                        ImageSearchResult(
                            search_query=original_query,
                            result_rank=len(results) + 1,
                            source_page_url=source_page,
                            direct_image_url=direct,
                            source_domain=self.source_domain,
                            rights_status="unknown",
                            provider_metadata={
                                "source_adapter": self.source_adapter,
                                "archive_query": compact_query,
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

    @classmethod
    def _supports_query(cls, value: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold())
        words = set(normalized.split())
        return any(marker in normalized for marker in cls.franchise_markers) or any(
            marker.casefold() in words for marker in cls.character_markers
        )

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


class MorbotronSearchProvider(FrinkiacSearchProvider):
    """Public Futurama-frame fallback with the same provenance and safety contract."""

    name = "morbotron"
    api_url = "https://morbotron.com/api/search"
    base_url = "https://morbotron.com"
    source_domain = "morbotron.com"
    source_adapter = "morbotron-public-search"
    franchise_markers: ClassVar[tuple[str, ...]] = ("futurama",)
    character_markers: ClassVar[tuple[str, ...]] = (
        "Fry",
        "Leela",
        "Bender",
        "Zoidberg",
        "Farnsworth",
        "Hermes",
        "Amy",
        "Nibbler",
        "Kif",
        "Zapp",
    )

    @classmethod
    def _compact_query(cls, value: str) -> str:
        compact = super()._compact_query(value)
        return "Bender" if compact == "Springfield" else compact


class FamilyGuyWikiSearchProvider:
    """Family Guy image search through the public, keyless MediaWiki API."""

    name = "family-guy-wiki"
    api_url = "https://familyguy.fandom.com/api.php"
    screenshot_category = "Category:Screenshots"
    category_prefixes = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")

    def __init__(self, settings: Settings, *, sample_offset: int = 0):
        self.settings = settings
        self.sample_offset = max(0, sample_offset)

    async def search(self, plan: SearchPlan, cursor: str | None = None) -> SearchPage:
        del cursor
        planned_queries = [
            query
            for query in _diverse_queries(plan)
            if "family guy" in re.sub(r"[^a-z0-9]+", " ", query.casefold())
        ]
        if not planned_queries:
            return SearchPage(results=[])
        results: list[ImageSearchResult] = []
        seen: set[str] = set()
        headers = {
            "User-Agent": "RunwayEditorial/0.1 (private local editorial assistant)",
        }
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers=headers,
        ) as client:
            for prefix in self._rotated_category_prefixes():
                response = await client.get(
                    self.api_url,
                    params={
                        "action": "query",
                        "format": "json",
                        "generator": "categorymembers",
                        "gcmtitle": self.screenshot_category,
                        "gcmnamespace": 6,
                        "gcmlimit": 50,
                        "gcmstartsortkeyprefix": prefix,
                        "prop": "imageinfo",
                        "iiprop": "url|size|mime",
                    },
                )
                response.raise_for_status()
                payload = response.json()
                pages = (
                    payload.get("query", {}).get("pages", {}) if isinstance(payload, dict) else {}
                )
                rows = list(pages.values()) if isinstance(pages, dict) else []
                rows.sort(
                    key=lambda row: (
                        int(row.get("index") or 0) if isinstance(row, dict) else 0,
                        str(row.get("title") or "") if isinstance(row, dict) else "",
                    )
                )
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    image_rows = row.get("imageinfo", [])
                    info = image_rows[0] if isinstance(image_rows, list) and image_rows else {}
                    if not isinstance(info, dict):
                        continue
                    direct = str(info.get("url") or "").strip()
                    source_page = str(info.get("descriptionurl") or "").strip()
                    mime_type = str(info.get("mime") or "").casefold()
                    width = DuckDuckGoSearchProvider._positive_int(info.get("width"))
                    height = DuckDuckGoSearchProvider._positive_int(info.get("height"))
                    if (
                        not direct.startswith(("http://", "https://"))
                        or not source_page.startswith(("http://", "https://"))
                        or direct in seen
                        or not self._is_usable_image_info(
                            mime_type,
                            width,
                            height,
                            minimum_dimension=self.settings.minimum_image_dimension,
                        )
                    ):
                        continue
                    seen.add(direct)
                    result = ImageSearchResult(
                        search_query=planned_queries[0],
                        result_rank=len(results) + 1,
                        source_page_url=source_page,
                        direct_image_url=direct,
                        source_domain=urlparse(source_page).netloc,
                        original_width=width,
                        original_height=height,
                        rights_status="unknown",
                        provider_metadata={
                            "source_adapter": "family-guy-fandom-mediawiki",
                            "result_title": str(row.get("title") or ""),
                            "page_id": row.get("pageid"),
                            "mime_type": mime_type,
                            "query_offset": self.sample_offset,
                            "provider_request_query": (
                                f"{self.screenshot_category} prefix {prefix}"
                            ),
                        },
                    )
                    if is_known_adult_result(result):
                        continue
                    results.append(result)
                    if len(results) >= self.settings.browser_search_results_per_query:
                        break
                if len(results) >= self.settings.browser_search_results_per_query:
                    break
        return SearchPage(results=results)

    def _rotated_category_prefixes(self) -> list[str]:
        count = min(
            self.settings.browser_search_max_queries,
            len(self.category_prefixes),
        )
        start = (self.sample_offset * count) % len(self.category_prefixes)
        return [
            self.category_prefixes[(start + index) % len(self.category_prefixes)]
            for index in range(count)
        ]

    @staticmethod
    def _is_usable_image_info(
        mime_type: str,
        width: int | None,
        height: int | None,
        *,
        minimum_dimension: int,
    ) -> bool:
        return (
            mime_type.casefold() in {"image/jpeg", "image/png", "image/webp"}
            and width is not None
            and height is not None
            and min(width, height) >= minimum_dimension
        )

    @staticmethod
    def _compact_query(value: str) -> str:
        words = [
            word
            for word in re.sub(r"[^a-zA-Z0-9]+", " ", value).split()
            if word.casefold()
            not in {
                "family",
                "guy",
                "animated",
                "frame",
                "screencap",
                "screenshot",
                "still",
            }
        ]
        return " ".join(words[:5]) or "Peter Griffin"


class TopicMixArchiveSearchProvider:
    """Creator-configured Qlob mix across stable public frame/image archives."""

    name = "archives"

    def __init__(self, settings: Settings, *, sample_offset: int = 0):
        self.settings = settings
        self.sample_offset = max(0, sample_offset)
        self.providers: list[SearchProvider] = [
            FrinkiacSearchProvider(settings, sample_offset=self.sample_offset),
            FamilyGuyWikiSearchProvider(settings, sample_offset=self.sample_offset),
            MorbotronSearchProvider(settings, sample_offset=self.sample_offset),
        ]
        self.last_diagnostics: dict[str, object] = {}

    async def search(self, plan: SearchPlan, cursor: str | None = None) -> SearchPage:
        outcomes = await asyncio.gather(
            *(provider.search(plan, cursor=cursor) for provider in self.providers),
            return_exceptions=True,
        )
        pools: dict[str, list[ImageSearchResult]] = {}
        errors: dict[str, str] = {}
        for provider, outcome in zip(self.providers, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                errors[provider.name] = f"{type(outcome).__name__}: {outcome}"
                pools[provider.name] = []
            else:
                pools[provider.name] = list(outcome.results)
        if not any(pools.values()) and errors:
            raise RuntimeError(
                "all topic archives failed: "
                + "; ".join(f"{name}: {error}" for name, error in sorted(errors.items()))
            )
        maximum = self.settings.browser_search_max_results
        primary_quota = max(
            1,
            min(maximum, round(maximum * self.settings.discovery_primary_topic_query_share)),
        )
        secondary_total = maximum - primary_quota
        family_quota = (secondary_total + 1) // 2
        futurama_quota = secondary_total - family_quota
        quotas = {
            "frinkiac": primary_quota,
            "family-guy-wiki": family_quota,
            "morbotron": futurama_quota,
        }
        balanced_pools = {name: self._round_robin_queries(rows) for name, rows in pools.items()}
        selected = {name: rows[: quotas.get(name, 0)] for name, rows in balanced_pools.items()}
        leftovers = [
            row for name, rows in balanced_pools.items() for row in rows[quotas.get(name, 0) :]
        ]
        ordered: list[ImageSearchResult] = []
        while len(ordered) < maximum and any(selected.values()):
            for name, take in (("frinkiac", 2), ("family-guy-wiki", 1), ("morbotron", 1)):
                for _index in range(take):
                    rows = selected.get(name, [])
                    if rows and len(ordered) < maximum:
                        ordered.append(rows.pop(0))
        for row in leftovers:
            if len(ordered) >= maximum:
                break
            if row.direct_image_url not in {item.direct_image_url for item in ordered}:
                ordered.append(row)
        results = [
            row.model_copy(update={"result_rank": index})
            for index, row in enumerate(ordered[:maximum], start=1)
        ]
        self.last_diagnostics = {
            "provider_contributions": {name: len(rows) for name, rows in pools.items()},
            "provider_errors": errors,
            "target_quotas": quotas,
            "selected_distribution": {
                "frinkiac": sum(row.source_domain == "frinkiac.com" for row in results),
                "family-guy-wiki": sum(
                    "familyguy.fandom.com" in row.source_domain for row in results
                ),
                "morbotron": sum(row.source_domain == "morbotron.com" for row in results),
            },
        }
        return SearchPage(results=results)

    @staticmethod
    def _round_robin_queries(rows: list[ImageSearchResult]) -> list[ImageSearchResult]:
        groups: dict[str, list[ImageSearchResult]] = {}
        for row in rows:
            key = str(row.provider_metadata.get("provider_request_query") or row.search_query)
            groups.setdefault(key, []).append(row)
        ordered: list[ImageSearchResult] = []
        depth = 0
        while any(depth < len(group) for group in groups.values()):
            for group in groups.values():
                if depth < len(group):
                    ordered.append(group[depth])
            depth += 1
        return ordered


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
