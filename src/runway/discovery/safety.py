from __future__ import annotations

import re
from urllib.parse import urlparse

from runway.discovery.schemas import ImageSearchResult

ADULT_DOMAIN_MARKERS = {
    "4tube.com",
    "beeg.com",
    "brazzers.com",
    "eporner.com",
    "freeones.com",
    "hclips.com",
    "imagefap.com",
    "motherless.com",
    "pimpandhost.com",
    "pornhub.com",
    "redtube.com",
    "spankbang.com",
    "thumbzilla.com",
    "xhamster.com",
    "xnxx.com",
    "xvideos.com",
    "youporn.com",
}


def normalized_domain(value: str | None) -> str:
    raw = (value or "").strip().casefold()
    if "://" in raw:
        raw = urlparse(raw).netloc
    return raw.split(":", 1)[0].removeprefix("www.")


def is_known_adult_domain(value: str | None) -> bool:
    domain = normalized_domain(value)
    return any(domain == marker or domain.endswith(f".{marker}") for marker in ADULT_DOMAIN_MARKERS)


def is_known_adult_result(result: ImageSearchResult) -> bool:
    return any(
        is_known_adult_domain(value)
        for value in (
            result.source_domain,
            result.source_page_url,
            result.direct_image_url,
        )
    )


def _tokens(value: object) -> set[str]:
    return {
        token.removesuffix("s")
        for token in re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).split()
        if len(token) >= 4
    }


def is_likely_query_match(result: ImageSearchResult) -> bool:
    """Reject obviously unrelated search-engine cards before download/model usage."""
    metadata = result.provider_metadata
    combined = " ".join(
        (
            result.source_page_url,
            result.direct_image_url,
            str(metadata.get("result_title") or ""),
        )
    )
    result_tokens = _tokens(combined)
    query_tokens = _tokens(result.search_query).difference(
        {
            "animated",
            "frame",
            "image",
            "scene",
            "screencap",
            "screenshot",
            "still",
        }
    )
    if "simpson" in query_tokens:
        return "simpson" in result_tokens
    return bool(query_tokens.intersection(result_tokens))
