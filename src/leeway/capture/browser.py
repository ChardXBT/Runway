from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, sync_playwright

from leeway.capture.adapter import YouTubeCommunityPostsAdapterV1
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
                seen: set[str] = set()
                cycle = 0
                while idle_cycles < self.settings.capture_idle_cycles_before_stop:
                    cycle += 1
                    self._pause_for_challenge(page, progress)
                    self._expand_read_more(page)
                    html = page.content()
                    batch = self.adapter.extract_html(html, base_url=page.url)
                    fresh = [
                        post
                        for post in batch.posts
                        if post.stable_key() and post.stable_key() not in seen
                    ]
                    for post in fresh:
                        seen.add(post.stable_key())
                    if max_posts is not None:
                        fresh = fresh[: max(0, max_posts - len(seen) + len(fresh))]
                    latest = self.capture.append_records(
                        fresh,
                        mode=mode,
                        channel_url=channel_url,
                        resume=resume,
                        finalize=False,
                    )
                    progress(
                        f"cycle={cycle} unique={len(seen)} new={len(fresh)} "
                        f"media={latest.media_downloaded} checkpoint={latest.run_id}"
                    )
                    if batch.diagnostics:
                        self._save_diagnostics(
                            page, latest.run_id or 0, cycle, batch.model_dump(), save_all_snapshots
                        )
                    idle_cycles = 0 if fresh else idle_cycles + 1
                    if max_posts is not None and len(seen) >= max_posts:
                        break
                    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                    page.wait_for_timeout(delay)
                latest = self.capture.append_records(
                    [], mode=mode, channel_url=channel_url, resume=True, finalize=True
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

    @staticmethod
    def _expand_read_more(page: Page) -> None:
        # The selector is scoped to post containers and only clicks literal read-more controls.
        controls = page.locator(
            "ytd-backstage-post-thread-renderer button:has-text('Read more'), "
            "ytd-backstage-post-thread-renderer tp-yt-paper-button:has-text('Read more')"
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
