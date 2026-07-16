from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from leeway.capture.schemas import (
    ExtractedPost,
    ExtractionBatch,
    ExtractionDiagnostic,
    ImageReference,
)
from leeway.domain.enums import DatePrecision, PostType

POST_ID_RE = re.compile(r"/(?:post|channel/[^/]+/community)\/([A-Za-z0-9_-]+)")


def parse_visible_count(value: str | None) -> int | None:
    if not value:
        return None
    normalized = value.strip().upper().replace(",", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMB]?)", normalized)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2)]
    return int(number * multiplier)


def _best_image_url(image: Tag) -> str | None:
    srcset = image.get("srcset") or image.get("data-srcset")
    if isinstance(srcset, str):
        choices: list[tuple[int, str]] = []
        for part in srcset.split(","):
            bits = part.strip().split()
            if not bits:
                continue
            width = int(bits[1][:-1]) if len(bits) > 1 and bits[1].endswith("w") else 0
            choices.append((width, bits[0]))
        if choices:
            return max(choices)[1]
    for attribute in ("data-src", "data-original", "src"):
        value = image.get(attribute)
        if isinstance(value, str) and value and not value.startswith("data:image/gif"):
            return value
    return None


class YouTubeCommunityPostsAdapterV1:
    version = "youtube-community-v1"

    def extract_file(self, path: Path) -> ExtractionBatch:
        return self.extract_html(path.read_text(encoding="utf-8"))

    def extract_html(self, html: str, base_url: str = "https://www.youtube.com") -> ExtractionBatch:
        soup = BeautifulSoup(html, "html.parser")
        containers = soup.select(
            "article[data-testid='community-post'], "
            "ytd-backstage-post-thread-renderer, "
            "[data-post-type][data-post-id]"
        )
        posts: list[ExtractedPost] = []
        diagnostics: list[ExtractionDiagnostic] = []
        for container in containers:
            try:
                post = self._extract_container(container, base_url)
            except Exception as exc:
                diagnostics.append(
                    ExtractionDiagnostic(
                        code="container_parse_error",
                        message=f"{type(exc).__name__}: {exc}",
                        snippet=str(container)[:500],
                    )
                )
                continue
            if post is None:
                diagnostics.append(
                    ExtractionDiagnostic(
                        code="unmatched_layout",
                        message="Post-like container had neither a stable ID nor permalink.",
                        snippet=str(container)[:500],
                    )
                )
                continue
            posts.append(post)

        for unexpected in soup.select("[data-testid='unexpected-community-layout']"):
            diagnostics.append(
                ExtractionDiagnostic(
                    code="unexpected_layout",
                    message="Sanitized fixture intentionally contains an unsupported layout.",
                    snippet=str(unexpected)[:500],
                )
            )
        return ExtractionBatch(posts=posts, diagnostics=diagnostics)

    def _extract_container(self, container: Tag, base_url: str) -> ExtractedPost | None:
        permalink_tag = container.select_one("a[data-post-permalink], a[href*='/post/']")
        href = permalink_tag.get("href") if permalink_tag else None
        permalink = urljoin(base_url, href) if isinstance(href, str) else None
        external_post_id = container.get("data-post-id")
        if not isinstance(external_post_id, str) or not external_post_id:
            match = POST_ID_RE.search(permalink or "")
            external_post_id = match.group(1) if match else None
        if not external_post_id and not permalink:
            return None

        type_value = container.get("data-post-type")
        post_type = self._post_type(str(type_value or ""), container)

        caption_tag = container.select_one(
            "[data-full-caption], [data-testid='caption'], #content-text, "
            "yt-formatted-string#content-text"
        )
        caption: str | None = None
        if caption_tag:
            full = caption_tag.get("data-full-caption")
            caption = str(full).strip() if full else caption_tag.get_text(" ", strip=True)
            caption = caption or None

        date_tag = container.select_one("time, [data-testid='date'], #published-time-text")
        displayed_date = date_tag.get_text(" ", strip=True) if date_tag else None
        exact_value = None
        if date_tag:
            exact_value = date_tag.get("datetime") or date_tag.get("data-timestamp")
        published_at: datetime | None = None
        precision = DatePrecision.UNKNOWN
        if isinstance(exact_value, str) and exact_value:
            published_at = datetime.fromisoformat(exact_value.replace("Z", "+00:00"))
            precision = DatePrecision.EXACT
        elif displayed_date:
            lowered = displayed_date.lower()
            if any(unit in lowered for unit in ("ago", "hour", "minute", "day", "week")):
                precision = DatePrecision.RELATIVE
            elif re.search(r"\b\d{4}\b", displayed_date) and re.search(
                r"\b\d{1,2}\b", displayed_date
            ):
                precision = DatePrecision.DAY
            elif re.search(r"\b\d{4}\b", displayed_date):
                precision = DatePrecision.MONTH

        like_tag = container.select_one("[data-testid='likes'], #vote-count-middle")
        comment_tag = container.select_one("[data-testid='comments'], #comments")
        raw_like = like_tag.get_text(" ", strip=True) if like_tag else None
        raw_comment = comment_tag.get_text(" ", strip=True) if comment_tag else None

        images: list[ImageReference] = []
        for index, image in enumerate(container.select("img")):
            url = _best_image_url(image)
            classes = image.get("class")
            class_text = (
                " ".join(str(item) for item in classes)
                if isinstance(classes, list)
                else str(classes or "")
            )
            if not url or "avatar" in class_text.lower():
                continue
            absolute_url = url if url.startswith("fixture://") else urljoin(base_url, url)
            images.append(
                ImageReference(
                    url=absolute_url,
                    alt_text=str(image.get("alt")) if image.get("alt") else None,
                    position=index,
                )
            )

        raw = {
            "adapter_version": self.version,
            "attributes": {key: value for key, value in container.attrs.items()},
            "caption_collapsed": bool(container.select_one("[data-testid='read-more']")),
            "image_count": len(images),
        }
        return ExtractedPost(
            external_post_id=external_post_id,
            permalink=permalink,
            post_type=post_type,
            caption=caption,
            displayed_date_text=displayed_date,
            published_at=published_at,
            date_precision=precision,
            like_count=parse_visible_count(raw_like),
            comment_count=parse_visible_count(raw_comment),
            raw_like_text=raw_like,
            raw_comment_text=raw_comment,
            images=images,
            raw=raw,
        )

    @staticmethod
    def _post_type(value: str, container: Tag) -> PostType:
        aliases = {
            "text": PostType.TEXT,
            "image": PostType.IMAGE,
            "multi_image": PostType.MULTI_IMAGE,
            "poll": PostType.POLL,
            "quiz": PostType.QUIZ,
            "video_share": PostType.VIDEO_SHARE,
        }
        if value in aliases:
            return aliases[value]
        image_count = len(container.select("img"))
        if container.select_one("ytd-backstage-poll-renderer, [data-poll]"):
            return PostType.POLL
        if container.select_one("ytd-video-renderer, [data-video-share]"):
            return PostType.VIDEO_SHARE
        if image_count > 1:
            return PostType.MULTI_IMAGE
        if image_count == 1:
            return PostType.IMAGE
        return PostType.TEXT


def fixture_dom_path() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "community_fixture.html"
