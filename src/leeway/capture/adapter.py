from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from leeway.capture.schemas import (
    ExtractedPost,
    ExtractionBatch,
    ExtractionDiagnostic,
    ImageReference,
)
from leeway.domain.enums import DatePrecision, PostType

POST_ID_RE = re.compile(r"/(?:post|channel/[^/]+/community)\/([A-Za-z0-9_-]+)")
RELATIVE_DATE_RE = re.compile(
    r"\b(?P<count>\d+|an?|one)\s+"
    r"(?P<unit>minute|hour|day|week|month|year)s?\s+ago\b",
    re.IGNORECASE,
)


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


def canonicalize_youtube_image_url(url: str) -> str:
    """Request the uncropped source asset while retaining the displayed URL separately."""
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {
        "yt3.ggpht.com",
        "yt3.googleusercontent.com",
    }:
        return url
    return re.sub(r"=s\d+.*$", "=s0", url)


def _media_image_tags(container: Tag) -> list[Tag]:
    if container.name == "ytd-backstage-post-thread-renderer":
        return [
            image
            for image in container.select("ytd-backstage-image-renderer:not([hidden]) img")
            if isinstance(image, Tag)
        ]
    return [image for image in container.select("img") if isinstance(image, Tag)]


def _parse_published_date(
    exact_value: object,
    displayed_date: str | None,
    observed_at: datetime,
) -> tuple[datetime | None, DatePrecision]:
    if isinstance(exact_value, str) and exact_value:
        try:
            return (
                datetime.fromisoformat(exact_value.replace("Z", "+00:00")),
                DatePrecision.EXACT,
            )
        except ValueError:
            pass
    if not displayed_date:
        return None, DatePrecision.UNKNOWN

    lowered = displayed_date.lower().strip()
    match = RELATIVE_DATE_RE.search(lowered)
    if match:
        raw_count = match.group("count").lower()
        count = 1 if raw_count in {"a", "an", "one"} else int(raw_count)
        unit_days = {
            "minute": 1 / (24 * 60),
            "hour": 1 / 24,
            "day": 1,
            "week": 7,
            "month": 30,
            "year": 365,
        }
        return observed_at - timedelta(days=count * unit_days[match.group("unit").lower()]), (
            DatePrecision.RELATIVE
        )
    if lowered == "yesterday":
        return observed_at - timedelta(days=1), DatePrecision.RELATIVE
    if re.search(r"\b\d{4}\b", displayed_date) and re.search(r"\b\d{1,2}\b", displayed_date):
        return None, DatePrecision.DAY
    if re.search(r"\b\d{4}\b", displayed_date):
        return None, DatePrecision.MONTH
    return None, DatePrecision.UNKNOWN


class YouTubeCommunityPostsAdapterV1:
    version = "youtube-community-v3"

    def extract_file(self, path: Path) -> ExtractionBatch:
        return self.extract_html(path.read_text(encoding="utf-8"))

    def extract_html(
        self,
        html: str,
        base_url: str = "https://www.youtube.com",
        observed_at: datetime | None = None,
    ) -> ExtractionBatch:
        observed_at = observed_at or datetime.now(UTC)
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
                post = self._extract_container(container, base_url, observed_at)
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
            if post.raw.get("media_pending"):
                diagnostics.append(
                    ExtractionDiagnostic(
                        code="lazy_media_pending",
                        message=(
                            "Post attachment exists but at least one image URL has not "
                            "loaded yet; the record was deferred."
                        ),
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

    def _extract_container(
        self,
        container: Tag,
        base_url: str,
        observed_at: datetime,
    ) -> ExtractedPost | None:
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
        published_at, precision = _parse_published_date(exact_value, displayed_date, observed_at)

        like_tag = container.select_one("[data-testid='likes'], #vote-count-middle")
        comment_tag = container.select_one(
            "[data-testid='comments'], #comments, "
            "#reply-button-end a[aria-label*='comment' i], #comment-count"
        )
        raw_like = like_tag.get_text(" ", strip=True) if like_tag else None
        raw_comment: str | None = None
        if comment_tag:
            aria_label = comment_tag.get("aria-label")
            raw_comment = (
                str(aria_label).strip()
                if isinstance(aria_label, str) and aria_label.strip()
                else comment_tag.get_text(" ", strip=True) or None
            )
        comment_count = parse_visible_count(raw_comment)
        if raw_comment and raw_comment.strip().lower() in {"comment", "comments"}:
            comment_count = 0

        images: list[ImageReference] = []
        image_tags = _media_image_tags(container)
        missing_image_sources = 0
        for index, image in enumerate(image_tags):
            display_url = _best_image_url(image)
            if not display_url:
                missing_image_sources += 1
                continue
            absolute_display_url = (
                display_url
                if display_url.startswith("fixture://")
                else urljoin(base_url, display_url)
            )
            absolute_url = canonicalize_youtube_image_url(absolute_display_url)
            images.append(
                ImageReference(
                    url=absolute_url,
                    display_url=(
                        absolute_display_url if absolute_display_url != absolute_url else None
                    ),
                    alt_text=str(image.get("alt")) if image.get("alt") else None,
                    position=index,
                )
            )

        raw = {
            "adapter_version": self.version,
            "attributes": {key: value for key, value in container.attrs.items()},
            "caption_collapsed": bool(container.select_one("[data-testid='read-more']")),
            "observed_at": observed_at.isoformat(),
            "image_count": len(images),
            "expected_image_count": len(image_tags),
            "media_pending": missing_image_sources > 0,
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
            comment_count=comment_count,
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
        image_count = len(_media_image_tags(container))
        if container.select_one("ytd-backstage-poll-renderer:not([hidden]), [data-poll]"):
            return PostType.POLL
        if container.select_one("ytd-backstage-quiz-renderer:not([hidden]), [data-quiz]"):
            return PostType.QUIZ
        if container.select_one(
            "ytd-post-uploaded-video-renderer:not([hidden]), "
            "ytd-video-renderer:not([hidden]), [data-video-share]"
        ):
            return PostType.VIDEO_SHARE
        if image_count > 1:
            return PostType.MULTI_IMAGE
        if image_count == 1:
            return PostType.IMAGE
        return PostType.TEXT


def fixture_dom_path() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "community_fixture.html"
