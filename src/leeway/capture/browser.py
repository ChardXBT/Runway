from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Locator, Page, sync_playwright

from leeway.capture.adapter import YouTubeCommunityPostsAdapterV1
from leeway.capture.schemas import ExtractedPost, ExtractionDiagnostic
from leeway.capture.service import CaptureResult, CaptureService
from leeway.config import Settings
from leeway.domain.enums import CaptureMode


class CapturePaused(RuntimeError):
    pass


class BrowserCaptureService:
    """Explicit headed capture with no stealth, credential extraction, or write actions."""

    CHALLENGE_MARKERS = (
        "captcha",
        "unusual traffic",
        "verify it's you",
        "choose an account",
        "consent.google",
    )

    def __init__(self, capture: CaptureService, settings: Settings):
        self.capture = capture
        self.settings = settings
        self.adapter = YouTubeCommunityPostsAdapterV1()

    def run(
        self,
        channel_url: str,
        *,
        cdp_url: str | None = None,
        resume: bool = True,
        max_posts: int | None = None,
        delay_ms: int | None = None,
        save_all_snapshots: bool = False,
        progress: Callable[[str], None] = print,
    ) -> CaptureResult:
        parsed = urlparse(channel_url)
        if parsed.scheme != "https" or "youtube.com" not in parsed.netloc:
            raise ValueError("capture requires an explicit HTTPS youtube.com channel URL")
        mode = CaptureMode.CDP if cdp_url else CaptureMode.MANAGED_BROWSER
        delay = delay_ms or self.settings.capture_scroll_delay_ms
        latest: CaptureResult | None = None

        with sync_playwright() as playwright:
            context: BrowserContext
            if cdp_url:
                browser = playwright.chromium.connect_over_cdp(cdp_url)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
            else:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.settings.browser_profile_dir),
                    headless=False,
                    viewport={"width": 1440, "height": 1000},
                )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(channel_url, wait_until="domcontentloaded", timeout=60_000)
                self._verify_surface(page, channel_url)
                progress(f"Verified read-only Posts surface: {page.url}")
                idle_cycles = 0
                seen = self.capture.active_seen_keys(mode) if resume else set()
                cycle = 0
                previous_surface: tuple[int, int, str] | None = None
                limit_reached = max_posts is not None and len(seen) >= max_posts
                unresolved_media = 0
                unresolved_storage = 0
                blocking_diagnostics = 0
                while idle_cycles < self.settings.capture_idle_cycles_before_stop:
                    if limit_reached:
                        break
                    cycle += 1
                    self._pause_for_challenge(page, progress)
                    remaining = None if max_posts is None else max(0, max_posts - len(seen))
                    fresh, diagnostics, card_count = self._extract_new_cards(
                        page,
                        seen=seen,
                        max_new=remaining,
                        delay_ms=delay,
                        progress=progress,
                    )
                    unresolved_media = sum(
                        diagnostic.code == "lazy_media_pending" for diagnostic in diagnostics
                    )
                    surface = self._surface_state(page, card_count)
                    surface_grew = previous_surface is None or surface != previous_surface
                    previous_surface = surface
                    seen_before = len(seen)
                    latest = self.capture.append_records(
                        fresh,
                        mode=mode,
                        channel_url=channel_url,
                        resume=resume,
                        finalize=False,
                        diagnostics=diagnostics,
                        cursor_updates={
                            "surface_card_count": surface[0],
                            "surface_scroll_height": surface[1],
                            "surface_tail_key": surface[2],
                            "idle_cycles": idle_cycles,
                            "last_checkpoint_at": datetime.now(UTC).isoformat(),
                        },
                    )
                    seen = self.capture.active_seen_keys(mode)
                    persisted_new = len(seen) - seen_before
                    unresolved_storage = max(0, len(fresh) - persisted_new)
                    blocking_diagnostics = sum(
                        diagnostic.code
                        in {
                            "card_capture_error",
                            "card_parse_count",
                            "lazy_media_pending",
                        }
                        for diagnostic in diagnostics
                    )
                    progress(
                        f"cycle={cycle} captured={len(seen)} prepared={len(fresh)} "
                        f"stored={persisted_new} "
                        f"cards={card_count} pending_media={unresolved_media} "
                        f"media={latest.media_downloaded} checkpoint={latest.run_id}"
                    )
                    if diagnostics:
                        self._save_diagnostics(
                            page,
                            latest.run_id or 0,
                            cycle,
                            {
                                "diagnostics": [
                                    diagnostic.model_dump() for diagnostic in diagnostics
                                ],
                                "surface": {
                                    "card_count": surface[0],
                                    "scroll_height": surface[1],
                                    "tail_key": surface[2],
                                },
                            },
                            save_all_snapshots,
                        )
                    idle_cycles = 0 if persisted_new > 0 or surface_grew else idle_cycles + 1
                    if max_posts is not None and len(seen) >= max_posts:
                        limit_reached = True
                        break
                    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                    page.wait_for_timeout(delay)
                completed = bool(seen) and all(
                    (
                        not limit_reached,
                        unresolved_media == 0,
                        unresolved_storage == 0,
                        blocking_diagnostics == 0,
                        idle_cycles >= self.settings.capture_idle_cycles_before_stop,
                    )
                )
                completion_reason = (
                    "max_posts_checkpoint"
                    if limit_reached
                    else (
                        "unresolved_lazy_media"
                        if unresolved_media
                        else (
                            "media_ingest_incomplete"
                            if unresolved_storage
                            else (
                                "blocking_capture_diagnostic"
                                if blocking_diagnostics
                                else (
                                    "stable_surface_after_scroll"
                                    if completed
                                    else "empty_or_unverified_surface"
                                )
                            )
                        )
                    )
                )
                latest = self.capture.append_records(
                    [],
                    mode=mode,
                    channel_url=channel_url,
                    resume=True,
                    finalize=completed,
                    cursor_updates={
                        "idle_cycles": idle_cycles,
                        "completion_reason": completion_reason,
                        "capture_finished_at": datetime.now(UTC).isoformat(),
                    },
                )
                return latest
            except KeyboardInterrupt as exc:
                progress("Capture paused safely. Re-run the same command with --resume.")
                raise CapturePaused("capture paused by user") from exc
            except Exception:
                run_id = latest.run_id if latest else 0
                self._save_diagnostics(page, run_id or 0, 0, {"url": page.url}, True)
                raise
            finally:
                context.close()

    @staticmethod
    def _verify_surface(page: Page, requested_url: str) -> None:
        requested = urlparse(requested_url)
        current = urlparse(page.url)
        surface = (current.path + " " + page.title()).lower()
        if current.netloc != requested.netloc or not any(
            marker in surface for marker in ("/posts", "/community", "community")
        ):
            raise CapturePaused("page is not the requested channel Posts/Community surface")

    def _pause_for_challenge(self, page: Page, progress: Callable[[str], None]) -> None:
        sample = (
            page.url + " " + page.title() + " " + page.locator("body").inner_text()[:2000]
        ).lower()
        if any(marker in sample for marker in self.CHALLENGE_MARKERS):
            progress(
                "Authentication/consent/challenge detected. Handle it manually in the browser."
            )
            input("Press Enter only after the requested channel Posts page is visible: ")
            self._verify_surface(page, page.url)

    def _extract_new_cards(
        self,
        page: Page,
        *,
        seen: set[str],
        max_new: int | None,
        delay_ms: int,
        progress: Callable[[str], None],
    ) -> tuple[list[ExtractedPost], list[ExtractionDiagnostic], int]:
        cards = page.locator("ytd-backstage-post-thread-renderer")
        card_count = cards.count()
        records: list[ExtractedPost] = []
        diagnostics: list[ExtractionDiagnostic] = []
        per_card_delay = min(max(delay_ms // 4, 250), 750)
        for index in range(card_count):
            if max_new is not None and len(records) >= max_new:
                break
            card = cards.nth(index)
            href = card.locator("a[href*='/post/']").first.get_attribute("href")
            candidate_key = self._key_from_href(href)
            if candidate_key and candidate_key in seen:
                continue
            try:
                card.scroll_into_view_if_needed(timeout=5_000)
                page.wait_for_timeout(per_card_delay)
                self._expand_read_more(card)
                if not self._wait_for_card_media(card, page, timeout_ms=5_000):
                    diagnostics.append(
                        ExtractionDiagnostic(
                            code="lazy_media_pending",
                            message=(
                                f"Card {candidate_key or index} still had an attachment "
                                "without a source URL after entering the viewport."
                            ),
                        )
                    )
                    continue
                observed_at = datetime.now(UTC)
                outer_html = str(card.evaluate("(element) => element.outerHTML"))
                batch = self.adapter.extract_html(
                    outer_html,
                    base_url=page.url,
                    observed_at=observed_at,
                )
                diagnostics.extend(batch.diagnostics)
                if len(batch.posts) != 1:
                    if not batch.diagnostics:
                        diagnostics.append(
                            ExtractionDiagnostic(
                                code="card_parse_count",
                                message=(
                                    f"Expected one parsed post for card "
                                    f"{candidate_key or index}; got {len(batch.posts)}."
                                ),
                                snippet=outer_html[:500],
                            )
                        )
                    continue
                post = batch.posts[0]
                if post.stable_key() in seen:
                    continue
                post.raw.update(
                    {
                        "card_index_at_capture": index,
                        "capture_page_url": page.url,
                    }
                )
                post.raw_dom_snapshot_path = self._save_post_snapshot(post, outer_html)
                records.append(post)
                if len(records) % 25 == 0:
                    progress(f"Prepared {len(records)} new post cards in this checkpoint.")
            except Exception as exc:
                diagnostics.append(
                    ExtractionDiagnostic(
                        code="card_capture_error",
                        message=(f"Card {candidate_key or index}: {type(exc).__name__}: {exc}"),
                    )
                )
        return records, diagnostics, card_count

    @staticmethod
    def _key_from_href(href: str | None) -> str:
        if not href:
            return ""
        match = re.search(r"/post/([A-Za-z0-9_-]+)", href)
        return match.group(1) if match else ""

    @staticmethod
    def _wait_for_card_media(card: Locator, page: Page, *, timeout_ms: int) -> bool:
        images = card.locator("ytd-backstage-image-renderer:not([hidden]) img")
        if images.count() == 0:
            return True
        deadline = datetime.now(UTC).timestamp() + timeout_ms / 1000
        while datetime.now(UTC).timestamp() < deadline:
            ready = True
            for index in range(images.count()):
                image = images.nth(index)
                if not any(
                    image.get_attribute(attribute)
                    for attribute in ("src", "data-src", "srcset", "data-srcset")
                ):
                    ready = False
                    break
            if ready:
                return True
            page.wait_for_timeout(250)
        return False

    def _save_post_snapshot(self, post: ExtractedPost, html: str) -> str:
        key = post.stable_key() or f"unkeyed-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
        safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:120]
        path = self.settings.resolved_data_dir / "captures" / "posts" / f"{safe_key}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(html, encoding="utf-8")
        return path.relative_to(self.settings.resolved_data_dir).as_posix()

    @staticmethod
    def _surface_state(page: Page, card_count: int) -> tuple[int, int, str]:
        scroll_height = int(page.evaluate("() => document.documentElement.scrollHeight") or 0)
        tail_href = ""
        if card_count:
            tail_href = (
                page.locator("ytd-backstage-post-thread-renderer")
                .nth(card_count - 1)
                .locator("a[href*='/post/']")
                .first.get_attribute("href")
                or ""
            )
        return card_count, scroll_height, BrowserCaptureService._key_from_href(tail_href)

    @staticmethod
    def _expand_read_more(scope: Page | Locator) -> None:
        # The selector is scoped to post containers and only clicks literal read-more controls.
        controls = scope.locator(
            "ytd-backstage-post-thread-renderer button:has-text('Read more'), "
            "ytd-backstage-post-thread-renderer tp-yt-paper-button:has-text('Read more'), "
            "button:has-text('Read more'), tp-yt-paper-button:has-text('Read more')"
        )
        for index in range(min(controls.count(), 100)):
            control = controls.nth(index)
            if control.is_visible():
                control.click(timeout=2_000)

    def _save_diagnostics(
        self,
        page: Page,
        run_id: int,
        cycle: int,
        details: dict[str, object],
        save_html: bool,
    ) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = self.settings.resolved_data_dir / "snapshots" / f"capture-{run_id}-{cycle}-{stamp}"
        base.with_suffix(".json").write_text(
            json.dumps(details, indent=2, default=str), encoding="utf-8"
        )
        page.screenshot(path=str(base.with_suffix(".png")), full_page=False)
        if save_html:
            base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
