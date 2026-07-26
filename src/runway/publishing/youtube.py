from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import threading
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

from PIL import Image
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    AuditEvent,
    CandidateImage,
    CaptionFeedback,
    MediaAsset,
    Proposal,
    ProposalEvent,
    PublishAttempt,
)
from runway.db.repositories import audit, get_channel
from runway.domain.enums import ProposalStatus
from runway.domain.state_machine import require_transition
from runway.publishing.base import (
    AssistedPost,
    AssistedPreparation,
    PreparedPost,
    PublisherSessionStatus,
    PublishPreparation,
    PublishResult,
    VerificationResult,
)


def normalize_connector_channel_url(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError("channel URL is required")
    if candidate.startswith("@"):
        candidate = f"https://www.youtube.com/{candidate}"
    elif candidate.startswith("UC") and "/" not in candidate:
        candidate = f"https://www.youtube.com/channel/{candidate}"
    elif "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or hostname not in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
    }:
        raise ValueError("Use a secure youtube.com channel URL, @handle, or channel ID.")

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise ValueError("YouTube channel path is missing")
    if parts[0] == "channel":
        if len(parts) < 2 or not parts[1].startswith("UC"):
            raise ValueError("YouTube channel ID is invalid")
        channel_path = f"/channel/{parts[1]}"
    elif parts[0].startswith("@") and len(parts[0]) > 1:
        channel_path = f"/{parts[0]}"
    else:
        raise ValueError("Use a youtube.com/@handle URL or a youtube.com/channel/UC… URL.")

    return urlunparse(("https", "www.youtube.com", f"{channel_path}/posts", "", "", ""))


class BrowserScheduleReceipt(BaseModel):
    submitted: bool
    verified: bool
    external_id: str | None = None
    external_url: str | None = None
    screenshot_paths: list[str] = Field(default_factory=list)
    detail: str


class BrowserMutationReceipt(BaseModel):
    applied: bool
    verified: bool
    screenshot_paths: list[str] = Field(default_factory=list)
    detail: str


class PublishingPreflight(BaseModel):
    post: PreparedPost
    payload_hash: str
    image_url: str
    rights_status: str
    warnings: list[str] = Field(default_factory=list)


class StalePublishAttempt(ValueError):
    """A queued attempt was superseded before the browser touched YouTube."""


class YouTubeBrowserAdapter(Protocol):
    async def validate_session(self) -> PublisherSessionStatus: ...

    async def validate_channel_access(
        self,
        channel_url: str,
    ) -> PublisherSessionStatus: ...

    async def schedule(self, post: PreparedPost) -> BrowserScheduleReceipt: ...

    async def verify(self, post: PreparedPost) -> BrowserScheduleReceipt: ...

    async def edit(
        self,
        current: PreparedPost,
        updated: PreparedPost,
    ) -> BrowserMutationReceipt: ...

    async def remove(self, post: PreparedPost) -> BrowserMutationReceipt: ...


class PlaywrightYouTubeAdapter:
    """Visible, persistent-profile YouTube adapter with no challenge bypass."""

    # Retain the previous private entry point for tests and diagnostic callers while
    # sharing one normalized implementation across connector and publisher checks.
    _normalize_connector_channel_url = staticmethod(normalize_connector_channel_url)
    _browser_lock = threading.Lock()
    _challenge_markers = (
        "captcha",
        "verify you are human",
        "unusual traffic",
        "challenge",
    )

    def __init__(self, settings: Settings):
        self.settings = settings
        self.capture_dir = settings.resolved_data_dir / "captures" / "publisher"
        self.capture_dir.mkdir(parents=True, exist_ok=True)

    async def validate_session(self) -> PublisherSessionStatus:
        return await asyncio.to_thread(self._validate_session_sync)

    async def validate_channel_access(
        self,
        channel_url: str,
    ) -> PublisherSessionStatus:
        normalized = normalize_connector_channel_url(channel_url)
        return await asyncio.to_thread(
            self._validate_channel_access_sync,
            normalized,
        )

    async def schedule(self, post: PreparedPost) -> BrowserScheduleReceipt:
        return await asyncio.to_thread(self._schedule_sync, post)

    async def verify(self, post: PreparedPost) -> BrowserScheduleReceipt:
        return await asyncio.to_thread(self._verify_sync, post)

    async def edit(
        self,
        current: PreparedPost,
        updated: PreparedPost,
    ) -> BrowserMutationReceipt:
        return await asyncio.to_thread(self._edit_sync, current, updated)

    async def remove(self, post: PreparedPost) -> BrowserMutationReceipt:
        return await asyncio.to_thread(self._remove_sync, post)

    def login_interactive(self) -> None:
        """Open ordinary Google Chrome for manual sign-in without browser automation."""
        chrome = self.chrome_executable()
        self.settings.publisher_profile_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(chrome),
            f"--user-data-dir={self.settings.publisher_profile_dir}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            self.settings.publisher_channel_url,
        ]
        subprocess.Popen(command)
        input(
            "A normal Google Chrome window opened with Runway's isolated profile. "
            f"Sign into the owner-declared {self.settings.channel_name} publisher account, "
            "confirm the "
            f"{self.settings.channel_name} Posts page is visible, "
            "then CLOSE that Chrome window and press Enter here..."
        )

    def chrome_executable(self) -> Path:
        configured = self.settings.publisher_chrome_path
        if configured is not None:
            resolved = configured.expanduser().resolve()
            if not resolved.is_file():
                raise RuntimeError(f"configured Google Chrome was not found: {resolved}")
            return resolved

        candidates: list[Path] = []
        if os.name == "nt":
            for root_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
                root = os.environ.get(root_name)
                if root:
                    candidates.append(
                        Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
                    )
        else:
            for name in ("google-chrome", "google-chrome-stable", "chrome"):
                found = shutil.which(name)
                if found:
                    candidates.append(Path(found))
            candidates.extend(
                [
                    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                    Path("/usr/bin/google-chrome"),
                    Path("/usr/bin/google-chrome-stable"),
                ]
            )
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        raise RuntimeError(
            "Google Chrome is required for publisher sign-in. Install Chrome or set "
            "RUNWAY_PUBLISHER_CHROME_PATH."
        )

    def _launch_publisher_context(self, playwright: Any) -> Any:
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.settings.publisher_profile_dir),
            channel=self.settings.publisher_browser_channel,
            headless=False,
            timezone_id=self.settings.timezone,
            viewport={"width": 1440, "height": 1000},
        )

    def _validate_session_sync(self) -> PublisherSessionStatus:
        from playwright.sync_api import sync_playwright

        with self._browser_lock, sync_playwright() as playwright:
            context = self._launch_publisher_context(playwright)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(
                    self.settings.publisher_channel_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                page.wait_for_timeout(1500)
                self._stop_on_challenge(page)
                checks = self._session_contract_checks(page)
                valid = all(checks.values())
                missing = [label for label, matched in checks.items() if not matched]
                if not valid:
                    diagnostic = self.capture_dir / (
                        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-session-not-ready.png"
                    )
                    page.screenshot(path=str(diagnostic), full_page=True)
                return PublisherSessionStatus(
                    valid=valid,
                    publisher="youtube-visible-browser",
                    checks=checks,
                    detail=(
                        f"{self.settings.channel_name} channel identity and Community posting "
                        "capability verified. Runway observed the controls; it did not read an "
                        "exact delegated role from an API."
                        if valid
                        else f"{self.settings.channel_name} publishing session is not "
                        "ready; missing "
                        + ", ".join(missing)
                        + f". Reconnect the declared {self.settings.channel_name} "
                        "publisher identity."
                    ),
                )
            finally:
                context.close()

    def _validate_channel_access_sync(
        self,
        channel_url: str,
    ) -> PublisherSessionStatus:
        from playwright.sync_api import sync_playwright

        with self._browser_lock, sync_playwright() as playwright:
            context = self._launch_publisher_context(playwright)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(
                    channel_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                page.wait_for_timeout(1500)
                self._stop_on_challenge(page)

                checks = self._session_contract_checks(page)
                valid = all(checks.values())
                missing = [label for label, matched in checks.items() if not matched]
                if not valid:
                    diagnostic = self.capture_dir / (
                        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-connector-not-ready.png"
                    )
                    page.screenshot(path=str(diagnostic), full_page=True)
                return PublisherSessionStatus(
                    valid=valid,
                    publisher="youtube-visible-browser",
                    checks=checks,
                    detail=(
                        "Community posting capability verified. Runway observed the configured "
                        "channel controls; it did not verify an exact delegated role. No post "
                        "was created."
                        if valid
                        else "The invitation is not ready yet; missing "
                        + ", ".join(missing)
                        + ". Confirm that the owner-declared invitation was accepted, then retry."
                    ),
                )
            finally:
                context.close()

    def _schedule_sync(self, post: PreparedPost) -> BrowserScheduleReceipt:
        from playwright.sync_api import sync_playwright

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        screenshots: list[str] = []
        submitted = False
        with self._browser_lock, sync_playwright() as playwright:
            context = self._launch_publisher_context(playwright)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(
                    self.settings.publisher_channel_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                page.wait_for_timeout(1500)
                self._stop_on_challenge(page)
                if not self._session_contract_valid(page):
                    raise RuntimeError(
                        f"{self.settings.channel_name} publishing capability check failed"
                    )

                composer = page.locator("ytd-backstage-post-dialog-renderer:visible")
                editor = composer.locator('#contenteditable-root[contenteditable="true"]')
                self._require_one_visible(editor, "caption editor")
                editor.fill(post.caption)

                image_button = self._one_visible(
                    composer.get_by_role("button", name="Add an image", exact=True),
                    "Add an image button",
                )
                image_button.click()
                image_selector = composer.locator(
                    "ytd-backstage-multi-image-select-renderer:visible"
                )
                image_selector.wait_for(state="visible", timeout=10_000)
                upload = image_selector.locator('input[type="file"][accept="image/*"][multiple]')
                if upload.count() != 1:
                    raise RuntimeError(
                        f"expected one multi-image upload input; found {upload.count()}"
                    )
                upload.set_input_files(post.local_image_path)
                preview = image_selector.locator("#thumbnail-images-container img")
                preview.first.wait_for(state="visible", timeout=20_000)

                action_menu_locator = composer.locator('button[aria-label="Action menu"]:visible')
                action_menu_locator.first.wait_for(state="visible", timeout=20_000)
                action_menu = self._one_visible(
                    action_menu_locator,
                    "schedule action menu",
                )
                if not action_menu.is_enabled():
                    raise RuntimeError("YouTube schedule action menu did not become enabled")
                action_menu.click()

                schedule_item = self._first_visible_named(
                    page,
                    ("Schedule post", "Schedule Post"),
                    "Schedule post menu item",
                )
                schedule_item.click()

                picker = page.locator("ytd-date-time-picker-renderer:visible")
                with suppress(Exception):
                    picker.wait_for(state="visible", timeout=5_000)
                if picker.count() == 1:
                    self._fill_inline_schedule_picker(picker, post)
                    schedule_root = composer
                else:
                    dialog = page.locator(
                        "ytd-backstage-post-schedule-dialog-renderer, "
                        "tp-yt-paper-dialog:has-text('Schedule post')"
                    )
                    self._require_one_visible(dialog, "schedule dialog")
                    self._fill_schedule_inputs(dialog, post)
                    schedule_root = dialog

                before = self.capture_dir / f"{timestamp}-before-schedule.png"
                page.screenshot(path=str(before), full_page=True)
                screenshots.append(str(before))

                schedule_button = self._one_visible(
                    schedule_root.get_by_role("button", name="Schedule", exact=True),
                    "final Schedule button",
                )
                if not schedule_button.is_enabled():
                    raise RuntimeError("final Schedule button is disabled")
                # From this point onward a browser/process failure is ambiguous. Mark it as
                # possibly submitted before clicking so Runway never offers an unsafe retry.
                submitted = True
                schedule_button.click()
                page.wait_for_timeout(1800)
                self._stop_on_challenge(page)

                after = self.capture_dir / f"{timestamp}-after-schedule.png"
                page.screenshot(path=str(after), full_page=True)
                screenshots.append(str(after))
                receipt = self._verify_on_page(page, post, screenshots)
                return receipt.model_copy(update={"submitted": True})
            except Exception as exc:
                failure = self.capture_dir / f"{timestamp}-failure.png"
                try:
                    page.screenshot(path=str(failure), full_page=True)
                    screenshots.append(str(failure))
                except Exception:
                    pass
                if submitted:
                    return BrowserScheduleReceipt(
                        submitted=True,
                        verified=False,
                        screenshot_paths=screenshots,
                        detail=(
                            "YouTube submission occurred but verification was inconclusive: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    )
                raise
            finally:
                context.close()

    def _verify_sync(self, post: PreparedPost) -> BrowserScheduleReceipt:
        from playwright.sync_api import sync_playwright

        with self._browser_lock, sync_playwright() as playwright:
            context = self._launch_publisher_context(playwright)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(
                    self.settings.publisher_channel_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                page.wait_for_timeout(1500)
                self._stop_on_challenge(page)
                receipt = self._verify_on_page(page, post, [])
                screenshot = self.capture_dir / (
                    datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-scheduled-verification.png"
                )
                page.screenshot(path=str(screenshot), full_page=True)
                return receipt.model_copy(update={"screenshot_paths": [str(screenshot)]})
            finally:
                context.close()

    def _edit_sync(
        self,
        current: PreparedPost,
        updated: PreparedPost,
    ) -> BrowserMutationReceipt:
        from playwright.sync_api import sync_playwright

        if current.proposal_id != updated.proposal_id:
            raise ValueError("a YouTube edit cannot change proposal identity")
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        screenshots: list[str] = []
        applied = False
        with self._browser_lock, sync_playwright() as playwright:
            context = self._launch_publisher_context(playwright)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(
                    self.settings.publisher_channel_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                page.wait_for_timeout(1500)
                self._stop_on_challenge(page)
                if not self._session_contract_valid(page, open_composer=False):
                    raise RuntimeError(
                        f"{self.settings.channel_name} publishing capability check failed"
                    )

                card = self._scheduled_card(page, current)
                menu = self._first_visible(
                    card,
                    (
                        'button[aria-label*="Action menu"]',
                        'button[aria-label*="More actions"]',
                        'button[aria-label*="More"]',
                    ),
                    "scheduled-post action menu",
                )
                menu.click()
                edit_item = self._first_visible_named(
                    page,
                    ("Edit post", "Edit"),
                    "scheduled-post Edit action",
                )
                edit_item.click()
                page.wait_for_timeout(800)

                composer = page.locator("ytd-backstage-post-dialog-renderer")
                self._require_one_visible(composer, "post editor")
                editor = composer.locator('#contenteditable-root[contenteditable="true"]')
                self._require_one_visible(editor, "caption editor")
                editor.fill(updated.caption)

                action_menu_locator = composer.locator('button[aria-label="Action menu"]:visible')
                action_menu_locator.first.wait_for(state="visible", timeout=20_000)
                action_menu = self._one_visible(
                    action_menu_locator,
                    "schedule action menu",
                )
                action_menu.click()
                schedule_item = self._first_visible_named(
                    page,
                    ("Edit scheduled time", "Change scheduled time", "Schedule post"),
                    "scheduled-time action",
                )
                schedule_item.click()

                picker = page.locator("ytd-date-time-picker-renderer:visible")
                with suppress(Exception):
                    picker.wait_for(state="visible", timeout=5_000)
                if picker.count() == 1:
                    self._fill_inline_schedule_picker(picker, updated)
                    schedule_root = composer
                else:
                    dialog = page.locator(
                        "ytd-backstage-post-schedule-dialog-renderer, "
                        "tp-yt-paper-dialog:has-text('Schedule post')"
                    )
                    self._require_one_visible(dialog, "schedule dialog")
                    self._fill_schedule_inputs(dialog, updated)
                    schedule_root = dialog

                before = self.capture_dir / f"{timestamp}-before-lineup-edit.png"
                page.screenshot(path=str(before), full_page=True)
                screenshots.append(str(before))

                save_button = self._first_visible_named(
                    schedule_root,
                    ("Save", "Schedule"),
                    "final scheduled-post save button",
                )
                if not save_button.is_enabled():
                    raise RuntimeError("final scheduled-post save button is disabled")
                applied = True
                save_button.click()
                page.wait_for_timeout(1800)
                self._stop_on_challenge(page)

                after = self.capture_dir / f"{timestamp}-after-lineup-edit.png"
                page.screenshot(path=str(after), full_page=True)
                screenshots.append(str(after))
                receipt = self._verify_on_page(page, updated, screenshots)
                return BrowserMutationReceipt(
                    applied=True,
                    verified=receipt.verified,
                    screenshot_paths=screenshots,
                    detail=(
                        f"Scheduled {self.settings.channel_name} post was edited and verified."
                        if receipt.verified
                        else receipt.detail
                    ),
                )
            except Exception as exc:
                failure = self.capture_dir / f"{timestamp}-lineup-edit-failure.png"
                try:
                    page.screenshot(path=str(failure), full_page=True)
                    screenshots.append(str(failure))
                except Exception:
                    pass
                if applied:
                    return BrowserMutationReceipt(
                        applied=True,
                        verified=False,
                        screenshot_paths=screenshots,
                        detail=(
                            "YouTube accepted the edit action, but verification was "
                            f"inconclusive: {type(exc).__name__}: {exc}"
                        ),
                    )
                raise
            finally:
                context.close()

    def _remove_sync(self, post: PreparedPost) -> BrowserMutationReceipt:
        from playwright.sync_api import sync_playwright

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        screenshots: list[str] = []
        applied = False
        with self._browser_lock, sync_playwright() as playwright:
            context = self._launch_publisher_context(playwright)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(
                    self.settings.publisher_channel_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                page.wait_for_timeout(1500)
                self._stop_on_challenge(page)
                if not self._session_contract_valid(page, open_composer=False):
                    raise RuntimeError(
                        f"{self.settings.channel_name} publishing capability check failed"
                    )

                card = self._scheduled_card(page, post)
                menu = self._first_visible(
                    card,
                    (
                        'button[aria-label*="Action menu"]',
                        'button[aria-label*="More actions"]',
                        'button[aria-label*="More"]',
                    ),
                    "scheduled-post action menu",
                )
                menu.click()
                delete_item = self._first_visible_named(
                    page,
                    ("Delete post", "Delete"),
                    "scheduled-post Delete action",
                )
                delete_item.click()
                page.wait_for_timeout(500)

                confirmation = page.locator(
                    "tp-yt-paper-dialog:has-text('Delete'), "
                    "yt-confirm-dialog-renderer:has-text('Delete')"
                )
                self._require_one_visible(confirmation, "Delete confirmation")
                before = self.capture_dir / f"{timestamp}-before-lineup-remove.png"
                page.screenshot(path=str(before), full_page=True)
                screenshots.append(str(before))
                confirm_delete = self._first_visible_named(
                    confirmation,
                    ("Delete", "Delete post"),
                    "final Delete button",
                )
                if not confirm_delete.is_enabled():
                    raise RuntimeError("final Delete button is disabled")
                applied = True
                confirm_delete.click()
                page.wait_for_timeout(1800)
                self._stop_on_challenge(page)

                after = self.capture_dir / f"{timestamp}-after-lineup-remove.png"
                page.screenshot(path=str(after), full_page=True)
                screenshots.append(str(after))
                removed = not self._scheduled_cards(page, post)
                return BrowserMutationReceipt(
                    applied=True,
                    verified=removed,
                    screenshot_paths=screenshots,
                    detail=(
                        f"Scheduled {self.settings.channel_name} post was removed and "
                        "its absence verified."
                        if removed
                        else "Delete was submitted, but the matching scheduled post remains."
                    ),
                )
            except Exception as exc:
                failure = self.capture_dir / f"{timestamp}-lineup-remove-failure.png"
                try:
                    page.screenshot(path=str(failure), full_page=True)
                    screenshots.append(str(failure))
                except Exception:
                    pass
                if applied:
                    return BrowserMutationReceipt(
                        applied=True,
                        verified=False,
                        screenshot_paths=screenshots,
                        detail=(
                            "YouTube accepted the delete action, but verification was "
                            f"inconclusive: {type(exc).__name__}: {exc}"
                        ),
                    )
                raise
            finally:
                context.close()

    def _verify_on_page(
        self,
        page: Any,
        post: PreparedPost,
        screenshots: list[str],
    ) -> BrowserScheduleReceipt:
        cards = self._scheduled_cards(page, post)
        caption = page.get_by_text(post.caption, exact=True)
        visible_caption = any(
            caption.nth(index).is_visible() for index in range(min(caption.count(), 100))
        )
        external_url = None
        external_id = None
        unique_card = len(cards) == 1
        date_match = bool(cards)
        time_match = bool(cards)
        image_match = False
        if unique_card:
            card = cards[0]
            images = card.locator("img")
            image_match = any(
                images.nth(index).is_visible() for index in range(min(images.count(), 10))
            )
            links = card.locator('a[href*="/post/"]')
            if links.count() > 0:
                raw_url = links.first.get_attribute("href")
                external_url = urljoin("https://www.youtube.com", raw_url) if raw_url else None
                if external_url and "/post/" in external_url:
                    external_id = external_url.split("/post/", 1)[1].split("?", 1)[0]
        verified = visible_caption and unique_card and date_match and time_match and image_match
        missing = [
            label
            for label, matched in (
                ("exact caption", visible_caption),
                ("unique matching scheduled post", unique_card),
                ("scheduled date", date_match),
                ("scheduled time", time_match),
                ("image thumbnail", image_match),
            )
            if not matched
        ]
        return BrowserScheduleReceipt(
            submitted=True,
            verified=verified,
            external_id=external_id,
            external_url=external_url,
            screenshot_paths=screenshots,
            detail=(
                "Scheduled post with matching caption, date, time, and image found "
                f"in {self.settings.channel_name}'s Scheduled tab."
                if verified
                else f"{self.settings.channel_name} Scheduled-tab verification was "
                "incomplete; missing " + ", ".join(missing) + ". Do not submit again."
            ),
        )

    def _scheduled_card(self, page: Any, post: PreparedPost) -> Any:
        cards = self._scheduled_cards(page, post)
        if len(cards) != 1:
            raise RuntimeError(
                f"expected one scheduled {self.settings.channel_name} post matching "
                "the exact caption, date, "
                f"and time; found {len(cards)}"
            )
        return cards[0]

    def _scheduled_cards(self, page: Any, post: PreparedPost) -> list[Any]:
        scheduled_tab = page.get_by_role("tab", name="Scheduled", exact=True)
        self._require_one_visible(scheduled_tab, "Scheduled posts tab")
        scheduled_tab.click()
        page.wait_for_timeout(1200)

        date_markers, time_markers = self._schedule_markers(post)
        captions = page.get_by_text(post.caption, exact=True)
        cards: list[Any] = []
        seen: set[str] = set()
        for index in range(min(captions.count(), 100)):
            caption = captions.nth(index)
            if not caption.is_visible():
                continue
            card = caption.locator(
                "xpath=ancestor::*[self::ytd-backstage-post-thread-renderer "
                "or self::ytd-post-renderer][1]"
            )
            if card.count() != 1:
                continue
            card_text = " ".join(card.inner_text().lower().split())
            if not any(marker in card_text for marker in date_markers):
                continue
            if not any(marker in card_text for marker in time_markers):
                continue
            key = str(
                card.evaluate(
                    """
                    element => {
                      if (!element.__runwayLocatorKey) {
                        element.__runwayLocatorKey =
                          `runway-${Date.now()}-${Math.random().toString(36).slice(2)}`;
                      }
                      return element.__runwayLocatorKey;
                    }
                    """
                )
            )
            if key not in seen:
                seen.add(key)
                cards.append(card)
        return cards

    def _schedule_markers(self, post: PreparedPost) -> tuple[set[str], set[str]]:
        planned = datetime.fromisoformat(post.planned_publish_at)
        if planned.tzinfo is None:
            raise RuntimeError("the Runway slot does not include a timezone")
        month_short = planned.strftime("%b").lower()
        month_long = planned.strftime("%B").lower()
        date_markers = {
            f"{month_short} {planned.day}",
            f"{month_long} {planned.day}",
            planned.date().isoformat(),
            f"{planned.month}/{planned.day}/{planned.year}",
        }
        hour_12 = planned.strftime("%I").lstrip("0") or "12"
        time_markers = {
            f"{hour_12}:{planned.strftime('%M')} {planned.strftime('%p').lower()}",
            planned.strftime("%H:%M"),
        }
        return date_markers, time_markers

    def _fill_schedule_inputs(self, dialog: Any, post: PreparedPost) -> None:
        planned = self._planned_time_in_system_timezone(post)
        date_input = self._first_visible(
            dialog,
            (
                'input[aria-label*="Date"]',
                'input[type="date"]',
                "#datepicker input",
            ),
            "schedule date",
        )
        time_input = self._first_visible(
            dialog,
            (
                'input[aria-label*="Time"]',
                'input[type="time"]',
                "#time-input input",
            ),
            "schedule time",
        )
        date_input.fill(
            planned.date().isoformat()
            if date_input.get_attribute("type") == "date"
            else planned.strftime("%b %d, %Y")
        )
        time_input.fill(
            planned.strftime("%H:%M")
            if time_input.get_attribute("type") == "time"
            else planned.strftime("%I:%M %p").lstrip("0")
        )

    def _fill_inline_schedule_picker(self, picker: Any, post: PreparedPost) -> None:
        planned = self._planned_time_in_system_timezone(post)
        expected_date = f"{planned.strftime('%b')} {planned.day}, {planned.year}"
        date_label = picker.locator("#date-label-text")
        date_label.wait_for(state="visible", timeout=10_000)
        if self._normalized_text(date_label.inner_text()) != expected_date:
            date_picker = picker.locator("#date-picker")
            self._require_one_visible(date_picker, "schedule date picker")
            date_picker.click()
            calendar = picker.locator("#calendar-dialog:visible")
            calendar.wait_for(state="visible", timeout=10_000)
            textbox = calendar.locator("#textbox")
            self._require_one_visible(textbox, "schedule date textbox")
            textbox.fill(expected_date)
            textbox.press("Enter")
            date_label.wait_for(state="visible", timeout=10_000)
        if self._normalized_text(date_label.inner_text()) != expected_date:
            raise RuntimeError("YouTube did not retain the requested schedule date")

        expected_time = planned.strftime("%I:%M %p").lstrip("0")
        time_label = picker.locator("#time-label-text")
        time_label.wait_for(state="visible", timeout=10_000)
        if self._normalized_text(time_label.inner_text()) != expected_time:
            time_picker = picker.locator("#time-picker")
            self._require_one_visible(time_picker, "schedule time picker")
            time_picker.click()
            options = picker.locator('tp-yt-paper-item[role="option"]:visible')
            options.first.wait_for(state="visible", timeout=10_000)
            matches = [
                options.nth(index)
                for index in range(options.count())
                if self._normalized_text(options.nth(index).inner_text()) == expected_time
            ]
            if len(matches) != 1:
                raise RuntimeError(f"could not locate one visible {expected_time} schedule option")
            matches[0].click()
        if self._normalized_text(time_label.inner_text()) != expected_time:
            raise RuntimeError("YouTube did not retain the requested schedule time")

        timezone_label = picker.locator("#timezone-label-text")
        timezone_label.wait_for(state="visible", timeout=10_000)
        expected_offset = planned.strftime("%z")
        if f"GMT{expected_offset}" not in self._normalized_text(timezone_label.inner_text()):
            raise RuntimeError("YouTube's visible schedule timezone does not match Runway")

    @staticmethod
    def _planned_time_in_system_timezone(post: PreparedPost) -> datetime:
        planned = datetime.fromisoformat(post.planned_publish_at)
        if planned.tzinfo is None:
            raise RuntimeError("the Runway slot does not include a timezone")
        system_time = planned.astimezone()
        if (
            planned.replace(tzinfo=None) != system_time.replace(tzinfo=None)
            or planned.utcoffset() != system_time.utcoffset()
        ):
            raise RuntimeError(
                "the visible browser's system timezone does not match the timezone "
                "encoded in the Runway slot"
            )
        return planned

    @staticmethod
    def _normalized_text(value: str) -> str:
        return " ".join(value.split())

    @staticmethod
    def _first_visible_named(root: Any, names: tuple[str, ...], label: str) -> Any:
        for role in ("button", "menuitem"):
            for name in names:
                locator = root.get_by_role(role, name=name, exact=True)
                visible = [
                    locator.nth(index)
                    for index in range(locator.count())
                    if locator.nth(index).is_visible()
                ]
                if len(visible) == 1:
                    return visible[0]
        for name in names:
            locator = root.get_by_text(name, exact=True)
            visible = [
                locator.nth(index)
                for index in range(locator.count())
                if locator.nth(index).is_visible()
            ]
            if len(visible) == 1:
                return visible[0]
        raise RuntimeError(f"could not locate one visible {label}")

    def _session_contract_valid(
        self,
        page: Any,
        *,
        open_composer: bool = True,
    ) -> bool:
        return all(self._session_contract_checks(page, open_composer=open_composer).values())

    def _session_contract_checks(
        self,
        page: Any,
        *,
        open_composer: bool = True,
    ) -> dict[str, bool]:
        channel_loaded = self._configured_channel_loaded(page)
        management_access = self._management_controls_visible(page)
        posting_access = False
        composer_ready = False
        if channel_loaded and management_access:
            composer = page.locator("ytd-backstage-post-dialog-renderer:visible")
            with suppress(Exception):
                composer.wait_for(state="visible", timeout=10_000)
            editor = composer.locator('#contenteditable-root[contenteditable="true"]')
            if composer.count() == 1 and editor.count() == 1 and not editor.is_visible():
                placeholder = composer.locator("#placeholder-area")
                if placeholder.count() == 1 and placeholder.is_visible():
                    placeholder.click()
                    page.wait_for_timeout(300)
            if composer.count() == 1 and editor.count() == 1 and editor.is_visible():
                posting_access = True
                composer_ready = True
            else:
                try:
                    create_button = page.get_by_role("button", name="Create", exact=True)
                    self._require_one_visible(create_button, "YouTube Create button")
                    create_button.click()
                    create_post = page.get_by_text("Create post", exact=True)
                    self._require_one_visible(create_post, "Create post action")
                    posting_access = True
                    if open_composer:
                        create_post.click()
                        composer = page.locator("ytd-backstage-post-dialog-renderer:visible")
                        composer.wait_for(state="visible", timeout=10_000)
                        editor = composer.locator('#contenteditable-root[contenteditable="true"]')
                        composer_ready = editor.count() == 1 and editor.is_visible()
                    else:
                        page.keyboard.press("Escape")
                except Exception:
                    page.keyboard.press("Escape")
        checks = {
            "configured channel": channel_loaded,
            "channel management controls": management_access,
            "Community post controls": posting_access,
        }
        if open_composer:
            checks["Community composer"] = composer_ready
        return checks

    def _configured_channel_loaded(self, page: Any) -> bool:
        current = urlparse(page.url)
        if (current.hostname or "").casefold() not in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
        }:
            return False
        parts = [part.casefold() for part in current.path.split("/") if part]
        channel_id = self.settings.publisher_channel_id.casefold()
        handle = self.settings.channel_handle.strip().lstrip("@").casefold()
        return bool(
            (len(parts) >= 2 and parts[0] == "channel" and parts[1] == channel_id)
            or (parts and bool(handle) and parts[0] == f"@{handle}")
        )

    @staticmethod
    def _management_controls_visible(page: Any) -> bool:
        try:
            manage_videos = page.get_by_text("Manage videos", exact=True)
            manage_videos.wait_for(state="visible", timeout=10_000)
            return bool(manage_videos.count() == 1 and manage_videos.is_visible())
        except Exception:
            return False

    def _stop_on_challenge(self, page: Any) -> None:
        sample = f"{page.url} {page.title()}".lower()
        body = page.locator("body").inner_text(timeout=10_000)[:2500].lower()
        if any(marker in sample + body for marker in self._challenge_markers):
            raise RuntimeError("Google/YouTube challenge detected; resolve it manually and retry")

    @staticmethod
    def _require_one_visible(locator: Any, label: str) -> None:
        count = locator.count()
        if count != 1 or not locator.is_visible():
            raise RuntimeError(f"expected one visible {label}; found {count}")

    @staticmethod
    def _one_visible(locator: Any, label: str) -> Any:
        visible = [
            locator.nth(index)
            for index in range(locator.count())
            if locator.nth(index).is_visible()
        ]
        if len(visible) != 1:
            raise RuntimeError(
                f"expected one visible {label}; found {locator.count()} total, "
                f"{len(visible)} visible"
            )
        return visible[0]

    @classmethod
    def _first_visible(
        cls,
        root: Any,
        selectors: tuple[str, ...],
        label: str,
    ) -> Any:
        for selector in selectors:
            locator = root.locator(selector)
            count = locator.count()
            if count == 1 and locator.is_visible():
                return locator
        raise RuntimeError(f"could not locate a unique visible {label} input")


class YouTubeBrowserPublisher:
    """Visible-browser publisher for explicit confirmed Lineup actions."""

    publisher_name = "youtube-visible-browser-v1"

    def __init__(
        self,
        database: Database,
        settings: Settings,
        adapter: YouTubeBrowserAdapter | None = None,
    ):
        self.database = database
        self.settings = settings
        self.adapter = adapter or PlaywrightYouTubeAdapter(settings)
        self._confirm_lock = threading.Lock()

    async def validate_session(self) -> PublisherSessionStatus:
        if not self.settings.authorized_browser_ready:
            return PublisherSessionStatus(
                valid=False,
                publisher=self.publisher_name,
                detail=(
                    "Authorized browser publishing is disabled. Assisted preparation remains "
                    "available and does not require a saved YouTube session."
                ),
            )
        return await self._validated_adapter_session(source="manual_connection_check")

    async def validate_channel_access(
        self,
        channel_url: str,
    ) -> PublisherSessionStatus:
        normalized = normalize_connector_channel_url(channel_url)
        target = urlparse(normalized)
        parts = [part for part in target.path.split("/") if part]
        expected_handle = self.settings.channel_handle.strip().lstrip("@").casefold()
        matches_configured = False
        if len(parts) >= 2 and parts[0] == "channel":
            matches_configured = (
                parts[1].casefold() == self.settings.publisher_channel_id.casefold()
            )
        elif parts and parts[0].startswith("@"):
            matches_configured = parts[0][1:].casefold() == expected_handle
        if not matches_configured:
            raise ValueError(
                f"Runway is pinned to {self.settings.channel_name}; verify the configured "
                "channel ID or handle rather than another managed channel"
            )
        try:
            status = await self.adapter.validate_channel_access(self.settings.publisher_channel_url)
        except Exception as exc:
            self._record_connector_status(
                PublisherSessionStatus(
                    valid=False,
                    publisher=self.publisher_name,
                    detail=f"{type(exc).__name__}: {exc}",
                ),
                source="connector_access_check",
                error_type=type(exc).__name__,
            )
            raise
        self._record_connector_status(status, source="connector_access_check")
        return status

    def _record_connector_status(
        self,
        status: PublisherSessionStatus,
        *,
        source: str,
        error_type: str | None = None,
    ) -> None:
        with self.database.session() as session:
            audit(
                session,
                (
                    "youtube_connector_validated"
                    if status.valid
                    else "youtube_connector_validation_failed"
                ),
                "publisher",
                None,
                {
                    "valid": status.valid,
                    "publisher": status.publisher,
                    "detail": status.detail,
                    "checks": status.checks,
                    "source": source,
                    "error_type": error_type,
                    "checked_channel_id": self.settings.publisher_channel_id,
                },
            )

    async def _validated_adapter_session(self, *, source: str) -> PublisherSessionStatus:
        try:
            status = await self.adapter.validate_session()
        except Exception as exc:
            self._record_session_status(
                PublisherSessionStatus(
                    valid=False,
                    publisher=self.publisher_name,
                    detail=f"{type(exc).__name__}: {exc}",
                ),
                source=source,
                error_type=type(exc).__name__,
            )
            raise
        self._record_session_status(status, source=source)
        return status

    def _record_session_status(
        self,
        status: PublisherSessionStatus,
        *,
        source: str,
        error_type: str | None = None,
    ) -> None:
        with self.database.session() as session:
            audit(
                session,
                (
                    "youtube_session_validated"
                    if status.valid
                    else "youtube_session_validation_failed"
                ),
                "publisher",
                None,
                {
                    "valid": status.valid,
                    "publisher": status.publisher,
                    "detail": status.detail,
                    "checks": status.checks,
                    "source": source,
                    "error_type": error_type,
                    "checked_channel_id": self.settings.publisher_channel_id,
                },
            )

    def connection_status(self) -> dict[str, object]:
        """Return the last durable read-only publisher check without opening Chrome."""
        stale_after = timedelta(hours=24)
        if not self.settings.authorized_browser_ready:
            return {
                "state": "disabled",
                "valid": None,
                "detail": (
                    "Assisted publishing is ready without a browser connection. Switch to the "
                    "separately authorized browser mode before checking a saved session."
                ),
                "publisher": self.publisher_name,
                "checked_at": None,
                "stale": False,
                "stale_after_hours": 24,
                "checks": {},
                "last_verified_publish_at": None,
            }

        with self.database.session() as session:
            validations = session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.event_type.in_(
                        [
                            "youtube_session_validated",
                            "youtube_session_validation_failed",
                        ]
                    )
                )
                .order_by(desc(AuditEvent.created_at), desc(AuditEvent.id))
                .limit(100)
            ).all()
            last_verified = session.scalar(
                select(AuditEvent)
                .where(AuditEvent.event_type == "youtube_schedule_verified")
                .order_by(desc(AuditEvent.created_at), desc(AuditEvent.id))
                .limit(1)
            )

        validation = None
        for candidate in validations:
            try:
                candidate_details = json.loads(candidate.details_json)
            except (TypeError, ValueError):
                continue
            if candidate_details.get("checked_channel_id") == (self.settings.publisher_channel_id):
                validation = candidate
                break

        last_verified_at = (
            last_verified.created_at.isoformat() if last_verified is not None else None
        )
        if validation is None:
            return {
                "state": "unchecked",
                "valid": None,
                "detail": "No saved-session check has been recorded yet.",
                "publisher": self.publisher_name,
                "checked_at": None,
                "stale": False,
                "stale_after_hours": 24,
                "checks": {},
                "last_verified_publish_at": last_verified_at,
            }

        details = json.loads(validation.details_json)
        valid = bool(details.get("valid"))
        checked_at = validation.created_at
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=UTC)
        stale = valid and datetime.now(UTC) - checked_at > stale_after
        raw_checks = details.get("checks")
        checks = (
            {
                str(label): bool(passed)
                for label, passed in raw_checks.items()
                if isinstance(label, str) and isinstance(passed, bool)
            }
            if isinstance(raw_checks, dict)
            else {}
        )
        return {
            "state": "stale" if stale else ("connected" if valid else "needs_attention"),
            "valid": valid,
            "detail": str(details.get("detail") or "Publisher connection status recorded."),
            "publisher": str(details.get("publisher") or self.publisher_name),
            "checked_at": checked_at.isoformat(),
            "stale": stale,
            "stale_after_hours": 24,
            "checks": checks,
            "last_verified_publish_at": last_verified_at,
        }

    def prepare_assisted_batch(self, proposal_ids: list[int]) -> AssistedPreparation:
        """Validate exact Lineup payloads without creating queue or browser work."""
        unique_ids = list(dict.fromkeys(proposal_ids))
        if not unique_ids:
            raise ValueError("select at least one Lineup post to prepare")
        preflights = [self._publishing_preflight(proposal_id) for proposal_id in unique_ids]
        preflights.sort(key=lambda result: datetime.fromisoformat(result.post.planned_publish_at))
        with self.database.session() as session:
            audit(
                session,
                "assisted_workspace_prepared",
                "publisher",
                None,
                {
                    "proposal_ids": [result.post.proposal_id for result in preflights],
                    "network_action": False,
                    "queue_action": False,
                },
            )
        return AssistedPreparation(
            channel_name=self.settings.channel_name,
            channel_id=self.settings.publisher_channel_id,
            timezone=self.settings.timezone,
            youtube_url=self.settings.publisher_channel_url,
            items=[
                AssistedPost(
                    proposal_id=result.post.proposal_id,
                    planned_publish_at=result.post.planned_publish_at,
                    caption=result.post.caption,
                    image_url=result.image_url,
                    rights_status=result.rights_status,
                    warnings=result.warnings,
                )
                for result in preflights
            ],
        )

    async def prepare_attempt(self, proposal_id: int) -> PublishPreparation:
        self._require_enabled()
        status = await self._validated_adapter_session(source="publish_preparation")
        if not status.valid:
            raise ValueError(status.detail)
        post, payload_hash = self._prepared_post(proposal_id)
        token = secrets.token_urlsafe(32)
        token_hash = self._token_hash(token)
        now = datetime.now(UTC)
        expires = now + timedelta(minutes=self.settings.publisher_confirmation_ttl_minutes)
        with self.database.session() as session:
            for attempt in session.scalars(
                select(PublishAttempt).where(
                    PublishAttempt.proposal_id == proposal_id,
                    PublishAttempt.status == "prepared",
                )
            ):
                attempt.status = "superseded"
                attempt.completed_at = now
            attempt = PublishAttempt(
                proposal_id=proposal_id,
                publisher=self.publisher_name,
                status="prepared",
                confirmation_token_hash=token_hash,
                payload_hash=payload_hash,
                planned_publish_at=post.planned_publish_at,
                expires_at=expires,
            )
            session.add(attempt)
            session.flush()
            attempt_id = attempt.id
            audit(
                session,
                "youtube_publish_prepared",
                "proposal",
                proposal_id,
                {
                    "attempt_id": attempt.id,
                    "expires_at": expires.isoformat(),
                    "network_submission": False,
                },
            )
        return PublishPreparation(
            attempt_id=attempt_id,
            proposal_id=proposal_id,
            status="prepared",
            confirmation_token=token,
            confirmation_phrase=self.confirmation_phrase(proposal_id),
            expires_at=expires,
            planned_publish_at=post.planned_publish_at,
            caption=post.caption,
            local_image_path=post.local_image_path,
            channel_name=self.settings.channel_name,
        )

    async def confirm_schedule(
        self,
        attempt_id: int,
        *,
        confirmation_token: str,
        confirmation_phrase: str,
    ) -> PublishResult:
        self._require_enabled()
        if not self._confirm_lock.acquire(blocking=False):
            raise ValueError("another YouTube scheduling confirmation is already running")
        try:
            post = self._consume_confirmation(
                attempt_id,
                confirmation_token=confirmation_token,
                confirmation_phrase=confirmation_phrase,
            )
            try:
                receipt = await self.adapter.schedule(post)
            except Exception as exc:
                self._record_failure(attempt_id, str(exc))
                raise
            if not receipt.submitted:
                detail = "publisher adapter returned without a confirmed submission attempt"
                self._record_failure(attempt_id, detail)
                raise RuntimeError(detail)
            return self._record_receipt(attempt_id, receipt)
        finally:
            self._confirm_lock.release()

    def queue_attempt(
        self,
        proposal_id: int,
        *,
        trigger: str = "explicit_lineup_action",
    ) -> dict[str, object]:
        """Persist an explicit Lineup scheduling request without waiting for the browser."""
        self._require_enabled()
        preflight = self._publishing_preflight(proposal_id)
        now = datetime.now(UTC)
        with self.database.session() as session:
            attempt = self._queue_preflight_in_session(
                session,
                preflight,
                trigger=trigger,
                now=now,
            )
            return self._attempt_dict(attempt)

    def queue_attempts(self, proposal_ids: list[int]) -> list[dict[str, object]]:
        """Validate an ordered Lineup batch before persisting any queue requests."""
        unique_ids = list(dict.fromkeys(proposal_ids))
        self._require_enabled()
        preflights = [self._publishing_preflight(proposal_id) for proposal_id in unique_ids]
        now = datetime.now(UTC)
        with self.database.session() as session:
            attempts = [
                self._queue_preflight_in_session(
                    session,
                    preflight,
                    trigger="human_lineup_push",
                    now=now,
                )
                for preflight in preflights
            ]
            return [self._attempt_dict(attempt) for attempt in attempts]

    def _queue_preflight_in_session(
        self,
        session: Session,
        preflight: PublishingPreflight,
        *,
        trigger: str,
        now: datetime,
    ) -> PublishAttempt:
        proposal_id = preflight.post.proposal_id
        existing = session.scalar(
            select(PublishAttempt)
            .where(
                PublishAttempt.proposal_id == proposal_id,
                PublishAttempt.publisher == self.publisher_name,
                PublishAttempt.status.in_(["queued", "submitting"]),
            )
            .order_by(desc(PublishAttempt.id))
            .limit(1)
        )
        if existing is not None:
            return existing
        for prepared in session.scalars(
            select(PublishAttempt).where(
                PublishAttempt.proposal_id == proposal_id,
                PublishAttempt.publisher == self.publisher_name,
                PublishAttempt.status == "prepared",
            )
        ):
            prepared.status = "superseded"
            prepared.completed_at = now
        attempt = PublishAttempt(
            proposal_id=proposal_id,
            publisher=self.publisher_name,
            status="queued",
            confirmation_token_hash=self._token_hash(secrets.token_urlsafe(32)),
            payload_hash=preflight.payload_hash,
            planned_publish_at=preflight.post.planned_publish_at,
            expires_at=now + timedelta(days=1),
        )
        session.add(attempt)
        session.flush()
        audit(
            session,
            "youtube_publish_queued",
            "proposal",
            proposal_id,
            {
                "attempt_id": attempt.id,
                "planned_publish_at": preflight.post.planned_publish_at,
                "trigger": trigger,
            },
        )
        return attempt

    def next_queued_attempt_id(self) -> int | None:
        with self.database.session() as session:
            return session.scalar(
                select(PublishAttempt.id)
                .where(
                    PublishAttempt.publisher == self.publisher_name,
                    PublishAttempt.status == "queued",
                )
                .order_by(PublishAttempt.id)
                .limit(1)
            )

    def requeue_blocked_session_attempts(self) -> int:
        """Retry only attempts that stopped before the YouTube composer was touched."""
        self._require_enabled()
        now = datetime.now(UTC)
        requeued = 0
        with self.database.session() as session:
            attempts = session.scalars(
                select(PublishAttempt)
                .where(
                    PublishAttempt.publisher == self.publisher_name,
                    PublishAttempt.status == "blocked_session",
                )
                .order_by(PublishAttempt.id)
            ).all()
            for attempt in attempts:
                proposal = session.get(Proposal, attempt.proposal_id)
                latest_attempt_id = session.scalar(
                    select(PublishAttempt.id)
                    .where(
                        PublishAttempt.proposal_id == attempt.proposal_id,
                        PublishAttempt.publisher == self.publisher_name,
                    )
                    .order_by(desc(PublishAttempt.id))
                    .limit(1)
                )
                if (
                    proposal is None
                    or proposal.status != ProposalStatus.INTERNALLY_SCHEDULED.value
                    or latest_attempt_id != attempt.id
                ):
                    continue
                attempt.status = "queued"
                attempt.error_summary = None
                attempt.completed_at = None
                attempt.expires_at = now + timedelta(days=1)
                requeued += 1
                audit(
                    session,
                    "youtube_queue_resumed",
                    "proposal",
                    proposal.id,
                    {"attempt_id": attempt.id, "trigger": "human_resume_after_sign_in"},
                )
        return requeued

    async def process_queued_attempt(self, attempt_id: int) -> PublishResult:
        """Schedule one explicitly queued Lineup item; callers serialize the queue."""
        self._require_enabled()
        await asyncio.to_thread(self._confirm_lock.acquire)
        try:
            with self.database.session() as session:
                attempt = session.get(PublishAttempt, attempt_id)
                if attempt is None:
                    raise LookupError(f"publish attempt {attempt_id} not found")
                if attempt.status != "queued":
                    raise StalePublishAttempt(
                        f"publish attempt {attempt_id} was superseded before submission"
                    )
            session_status = await self._validated_adapter_session(source="queue_preflight")
            if not session_status.valid:
                self._record_preflight_failure(attempt_id, session_status.detail)
                raise ValueError(session_status.detail)
            post = self._consume_queued_attempt(attempt_id)
            try:
                receipt = await self.adapter.schedule(post)
            except Exception as exc:
                self._record_failure(attempt_id, str(exc))
                raise
            if not receipt.submitted:
                detail = "publisher adapter returned without a confirmed submission attempt"
                self._record_failure(attempt_id, detail)
                raise RuntimeError(detail)
            return self._record_receipt(attempt_id, receipt)
        finally:
            self._confirm_lock.release()

    async def update_lineup(
        self,
        proposal_id: int,
        *,
        final_caption: str | None,
        new_scheduled_publish_at: datetime | None = None,
        new_date: date | None = None,
    ) -> dict[str, object]:
        """Edit local desired state only, swapping occupied daily slots when needed."""
        if not self._confirm_lock.acquire(blocking=False):
            raise ValueError("another YouTube or Lineup operation is already running")
        try:
            with self.database.session() as session:
                proposal = session.get(Proposal, proposal_id)
                if proposal is None:
                    raise LookupError(f"proposal {proposal_id} not found")
                self._require_lineup_mutable(
                    proposal,
                    allow_overdue_internal=(
                        new_scheduled_publish_at is not None or new_date is not None
                    ),
                )
                current = self._lineup_post(session, proposal)
                old_caption = proposal.final_caption
                cleaned_caption = (
                    final_caption.strip() if final_caption is not None else old_caption
                )
                if not cleaned_caption:
                    raise ValueError("caption cannot be empty")

                current_slot = datetime.fromisoformat(current.planned_publish_at)
                channel = get_channel(session, self.settings.channel_handle)
                timezone = ZoneInfo(channel.timezone)
                target_slot = current_slot
                if new_scheduled_publish_at is not None and new_date is not None:
                    raise ValueError("send either a full timestamp or legacy new_date, not both")
                if new_scheduled_publish_at is not None:
                    target_slot = self._validate_channel_timestamp(
                        new_scheduled_publish_at,
                        timezone,
                    )
                elif new_date is not None:
                    current_local = current_slot.astimezone(timezone)
                    target_slot = datetime(
                        new_date.year,
                        new_date.month,
                        new_date.day,
                        current_local.hour,
                        current_local.minute,
                        current_local.second,
                        tzinfo=timezone,
                    )
                    target_slot = self._validate_channel_timestamp(target_slot, timezone)
                if target_slot.astimezone(UTC) <= datetime.now(UTC) + timedelta(minutes=5):
                    raise ValueError("Lineup times must be at least five minutes in the future")

                occupant = None
                if target_slot.isoformat() != current.planned_publish_at:
                    possible_occupants = session.scalars(
                        select(Proposal).where(
                            Proposal.id != proposal.id,
                            Proposal.channel_id == proposal.channel_id,
                            Proposal.scheduled_publish_at.is_not(None),
                            Proposal.status.not_in(
                                [
                                    ProposalStatus.REJECTED.value,
                                    ProposalStatus.CANCELLED.value,
                                ]
                            ),
                        )
                    ).all()
                    target_date = target_slot.astimezone(timezone).date()
                    target_occupants = [
                        item
                        for item in possible_occupants
                        if item.scheduled_publish_at
                        and datetime.fromisoformat(item.scheduled_publish_at)
                        .astimezone(timezone)
                        .date()
                        == target_date
                    ]
                    if len(target_occupants) > 1:
                        raise ValueError(
                            "the target date contains multiple active RunWay posts; "
                            "resolve the Lineup conflict before moving another post"
                        )
                    occupant = target_occupants[0] if target_occupants else None
                if occupant is not None:
                    if current_slot.astimezone(UTC) <= datetime.now(UTC) + timedelta(minutes=5):
                        raise ValueError(
                            "an overdue Lineup post can only move to an empty future date"
                        )
                    self._require_lineup_mutable(occupant)

                if (
                    cleaned_caption == old_caption
                    and target_slot.isoformat() == current.planned_publish_at
                ):
                    raise ValueError("nothing changed")

                records: list[dict[str, object]] = [
                    {
                        "id": proposal.id,
                        "status": proposal.status,
                        "current": current,
                        "updated": current.model_copy(
                            update={
                                "caption": cleaned_caption,
                                "planned_publish_at": target_slot.isoformat(),
                            }
                        ),
                    }
                ]
                if occupant is not None:
                    occupant_current = self._lineup_post(session, occupant)
                    records.append(
                        {
                            "id": occupant.id,
                            "status": occupant.status,
                            "current": occupant_current,
                            "updated": occupant_current.model_copy(
                                update={"planned_publish_at": current.planned_publish_at}
                            ),
                        }
                    )

            now = datetime.now(UTC)
            with self.database.session() as session:
                for record in records:
                    record_id = cast(int, record["id"])
                    loaded = session.get(Proposal, record_id)
                    current_post = record["current"]
                    updated_post = record["updated"]
                    if loaded is None:
                        raise LookupError(f"proposal {record_id} not found")
                    if not isinstance(current_post, PreparedPost) or not isinstance(
                        updated_post, PreparedPost
                    ):
                        raise RuntimeError("Lineup mutation payload is invalid")
                    if (
                        loaded.status != record["status"]
                        or loaded.scheduled_publish_at != current_post.planned_publish_at
                        or loaded.final_caption != current_post.caption
                    ):
                        raise RuntimeError(
                            "Lineup changed concurrently; refresh before trying again"
                        )

                    old_values = {
                        "final_caption": loaded.final_caption,
                        "scheduled_publish_at": loaded.scheduled_publish_at,
                    }
                    loaded.final_caption = updated_post.caption
                    loaded.scheduled_publish_at = updated_post.planned_publish_at
                    invalidated = self._invalidate_active_attempts(session, loaded.id, now)
                    session.add(
                        ProposalEvent(
                            proposal_id=loaded.id,
                            event_type=(
                                "lineup_slot_swapped" if len(records) > 1 else "lineup_updated"
                            ),
                            old_value_json=json.dumps(old_values, sort_keys=True),
                            new_value_json=json.dumps(
                                {
                                    "final_caption": loaded.final_caption,
                                    "scheduled_publish_at": loaded.scheduled_publish_at,
                                    "externally_synced": False,
                                },
                                sort_keys=True,
                            ),
                        )
                    )
                    audit(
                        session,
                        "lineup_post_updated",
                        "proposal",
                        loaded.id,
                        {
                            "externally_synced": False,
                            "invalidated_attempts": invalidated,
                            "swapped": len(records) > 1,
                        },
                    )

                if cleaned_caption != old_caption:
                    selected = session.get(Proposal, proposal_id)
                    if selected is None:
                        raise LookupError(f"proposal {proposal_id} not found")
                    session.add(
                        CaptionFeedback(
                            proposal_id=selected.id,
                            candidate_image_id=selected.candidate_image_id,
                            verdict="edited",
                            generated_caption=old_caption,
                            preferred_caption=cleaned_caption,
                            preferred_structure=None,
                            reason_codes_json=json.dumps(["human_lineup_edit"]),
                            image_verdict="good",
                            note="Edited after acceptance in Lineup.",
                        )
                    )

            return {
                "proposal_id": proposal_id,
                "affected_proposal_ids": [cast(int, record["id"]) for record in records],
                "swapped_with": cast(int, records[1]["id"]) if len(records) > 1 else None,
                "externally_synced": False,
                "ready_for_explicit_action": True,
                "requeue_proposal_ids": [],
            }
        finally:
            self._confirm_lock.release()

    async def remove_from_lineup(self, proposal_id: int) -> dict[str, object]:
        """Cancel one unsubmitted local Lineup slot without touching YouTube."""
        if not self._confirm_lock.acquire(blocking=False):
            raise ValueError("another YouTube or Lineup operation is already running")
        try:
            with self.database.session() as session:
                proposal = session.get(Proposal, proposal_id)
                if proposal is None:
                    raise LookupError(f"proposal {proposal_id} not found")
                self._require_lineup_mutable(proposal, allow_overdue_internal=True)
                current = self._lineup_post(session, proposal)
                original_status = proposal.status

            now = datetime.now(UTC)
            with self.database.session() as session:
                proposal = session.get(Proposal, proposal_id)
                if proposal is None:
                    raise LookupError(f"proposal {proposal_id} not found")
                if (
                    proposal.status != original_status
                    or proposal.scheduled_publish_at != current.planned_publish_at
                    or proposal.final_caption != current.caption
                ):
                    raise RuntimeError("Lineup changed concurrently; refresh before trying again")
                old_slot = proposal.scheduled_publish_at
                require_transition(proposal.status, ProposalStatus.CANCELLED)
                proposal.status = ProposalStatus.CANCELLED.value
                proposal.scheduled_publish_at = None
                invalidated = self._invalidate_active_attempts(session, proposal.id, now)
                session.add(
                    ProposalEvent(
                        proposal_id=proposal.id,
                        event_type="lineup_post_removed",
                        old_value_json=json.dumps(
                            {
                                "status": original_status,
                                "scheduled_publish_at": old_slot,
                            },
                            sort_keys=True,
                        ),
                        new_value_json=json.dumps(
                            {
                                "status": ProposalStatus.CANCELLED.value,
                                "scheduled_publish_at": None,
                                "externally_synced": False,
                            },
                            sort_keys=True,
                        ),
                    )
                )
                audit(
                    session,
                    "lineup_post_removed",
                    "proposal",
                    proposal.id,
                    {
                        "externally_synced": False,
                        "invalidated_attempts": invalidated,
                    },
                )
            return {
                "proposal_id": proposal_id,
                "status": ProposalStatus.CANCELLED.value,
                "externally_synced": False,
            }
        finally:
            self._confirm_lock.release()

    async def verify_scheduled_post(self, proposal_id: int) -> VerificationResult:
        self._require_enabled()
        post, _payload_hash = self._prepared_post(
            proposal_id,
            allowed_statuses={
                ProposalStatus.PUBLISHING.value,
                ProposalStatus.PUBLISH_UNVERIFIED.value,
                ProposalStatus.EXTERNALLY_SCHEDULED.value,
            },
        )
        receipt = await self.adapter.verify(post)
        with self.database.session() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                raise LookupError(f"proposal {proposal_id} not found")
            if receipt.verified:
                if proposal.status in {
                    ProposalStatus.PUBLISHING.value,
                    ProposalStatus.PUBLISH_UNVERIFIED.value,
                }:
                    require_transition(
                        proposal.status,
                        ProposalStatus.EXTERNALLY_SCHEDULED,
                    )
                    proposal.status = ProposalStatus.EXTERNALLY_SCHEDULED.value
                proposal.external_post_id = receipt.external_id or proposal.external_post_id
                proposal.external_post_url = receipt.external_url or proposal.external_post_url
                proposal.scheduled_verified_at = datetime.now(UTC)
                audit(
                    session,
                    "youtube_schedule_verified",
                    "proposal",
                    proposal.id,
                    {
                        "external_id": proposal.external_post_id,
                        "screenshots": receipt.screenshot_paths,
                    },
                )
            elif proposal.status == ProposalStatus.PUBLISHING.value:
                require_transition(
                    proposal.status,
                    ProposalStatus.PUBLISH_UNVERIFIED,
                )
                proposal.status = ProposalStatus.PUBLISH_UNVERIFIED.value
                audit(
                    session,
                    "youtube_submission_unverified",
                    "proposal",
                    proposal.id,
                    {"recovery_check": True},
                )
            final_status = proposal.status
        return VerificationResult(
            proposal_id=proposal_id,
            verified=receipt.verified,
            status=final_status,
            detail=receipt.detail,
        )

    def attempt_status(self, attempt_id: int) -> dict[str, object]:
        with self.database.session() as session:
            attempt = session.get(PublishAttempt, attempt_id)
            if attempt is None:
                raise LookupError(f"publish attempt {attempt_id} not found")
            return self._attempt_dict(attempt)

    def latest_attempt(self, proposal_id: int) -> dict[str, object] | None:
        with self.database.session() as session:
            attempt = session.scalar(
                select(PublishAttempt)
                .where(PublishAttempt.proposal_id == proposal_id)
                .order_by(desc(PublishAttempt.id))
                .limit(1)
            )
            return self._attempt_dict(attempt) if attempt else None

    @staticmethod
    def _require_lineup_mutable(
        proposal: Proposal,
        *,
        allow_overdue_internal: bool = False,
    ) -> None:
        allowed = {
            ProposalStatus.INTERNALLY_SCHEDULED.value,
            ProposalStatus.PUBLISH_FAILED.value,
        }
        if proposal.status not in allowed:
            if proposal.status == ProposalStatus.EXTERNALLY_SCHEDULED.value:
                raise ValueError(
                    "this post is already confirmed on YouTube and is treated as immutable; "
                    "a normal Lineup edit cannot change external content"
                )
            if proposal.status in {
                ProposalStatus.PUBLISHING.value,
                ProposalStatus.PUBLISH_UNVERIFIED.value,
            }:
                raise ValueError(
                    "this post has an in-flight or unverified YouTube action; verify it "
                    "before changing Lineup"
                )
            raise ValueError("only future internally scheduled posts can be changed in Lineup")
        if not proposal.scheduled_publish_at:
            raise ValueError("scheduled proposal has no Lineup slot")
        planned = datetime.fromisoformat(proposal.scheduled_publish_at)
        if planned.tzinfo is None:
            raise ValueError("Lineup time must include a timezone")
        overdue = planned.astimezone(UTC) <= datetime.now(UTC) + timedelta(minutes=5)
        recoverable_local_status = proposal.status in {
            ProposalStatus.INTERNALLY_SCHEDULED.value,
            ProposalStatus.PUBLISH_FAILED.value,
        }
        if overdue and not (allow_overdue_internal and recoverable_local_status):
            raise ValueError("only future Lineup posts can be changed")

    def _lineup_post(self, session: Any, proposal: Proposal) -> PreparedPost:
        candidate = session.get(CandidateImage, proposal.candidate_image_id)
        media = session.get(MediaAsset, candidate.media_asset_id) if candidate else None
        if media is None:
            raise ValueError("scheduled proposal has no local image")
        if candidate and candidate.rights_status == "blocked":
            raise ValueError("a blocked image cannot be managed in Lineup")
        path = self.settings.resolved_data_dir / media.local_path
        if not path.is_file():
            raise ValueError("scheduled proposal image is missing from local storage")
        if not proposal.scheduled_publish_at:
            raise ValueError("scheduled proposal has no Lineup slot")
        planned = datetime.fromisoformat(proposal.scheduled_publish_at)
        if planned.tzinfo is None:
            raise ValueError("Lineup time must include a timezone")
        return PreparedPost(
            proposal_id=proposal.id,
            planned_publish_at=proposal.scheduled_publish_at,
            caption=proposal.final_caption,
            local_image_path=str(path),
        )

    @staticmethod
    def _invalidate_active_attempts(session: Any, proposal_id: int, now: datetime) -> int:
        attempts = session.scalars(
            select(PublishAttempt).where(
                PublishAttempt.proposal_id == proposal_id,
                PublishAttempt.status.in_(["queued", "blocked_session", "prepared"]),
            )
        ).all()
        for attempt in attempts:
            attempt.status = "superseded"
            attempt.error_summary = "Superseded by a confirmed Lineup change."
            attempt.completed_at = now
        return len(attempts)

    def _record_lineup_sync_issue(
        self,
        proposal_id: int,
        event_type: str,
        details: dict[str, object],
    ) -> None:
        with self.database.session() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is not None:
                proposal.scheduled_verified_at = None
            audit(session, event_type, "proposal", proposal_id, details)

    def _prepared_post(
        self,
        proposal_id: int,
        *,
        allowed_statuses: set[str] | None = None,
    ) -> tuple[PreparedPost, str]:
        result = self._publishing_preflight(
            proposal_id,
            allowed_statuses=allowed_statuses,
        )
        return result.post, result.payload_hash

    def _publishing_preflight(
        self,
        proposal_id: int,
        *,
        allowed_statuses: set[str] | None = None,
    ) -> PublishingPreflight:
        statuses = allowed_statuses or {
            ProposalStatus.INTERNALLY_SCHEDULED.value,
            ProposalStatus.PUBLISH_FAILED.value,
        }
        with self.database.session() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                raise LookupError(f"proposal {proposal_id} not found")
            if proposal.status not in statuses:
                raise ValueError("external scheduling requires an internally scheduled proposal")
            candidate = session.get(CandidateImage, proposal.candidate_image_id)
            media = session.get(MediaAsset, candidate.media_asset_id) if candidate else None
            if media is None:
                raise ValueError("proposal has no local image")
            if candidate is None:
                raise ValueError("proposal has no image candidate")
            if candidate.rights_status == "blocked" or proposal.rights_decision == "blocked":
                raise ValueError("a blocked image cannot be scheduled")
            if candidate.hard_rejection_reason:
                raise ValueError(
                    "the selected image is blocked: " + candidate.hard_rejection_reason
                )
            try:
                relative_media = Path(media.local_path).relative_to("media")
            except ValueError as exc:
                raise ValueError("proposal image is outside the approved media directory") from exc
            media_root = (self.settings.resolved_data_dir / "media").resolve()
            path = (media_root / relative_media).resolve()
            if not path.is_relative_to(media_root):
                raise ValueError("proposal image is outside the approved media directory")
            if not path.is_file():
                raise ValueError("proposal image is missing from local storage")
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise ValueError("proposal image cannot be read from local storage") from exc
            if size <= 0:
                raise ValueError("proposal image is empty")
            youtube_image_limit = 16 * 1024 * 1024
            if size > youtube_image_limit:
                raise ValueError("proposal image exceeds YouTube's 16 MB Community-post limit")
            supported_suffixes = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
            if path.suffix.casefold() not in supported_suffixes:
                raise ValueError("proposal image must be JPG, PNG, GIF, or WEBP")
            try:
                with Image.open(path) as image:
                    image_format = (image.format or "").upper()
                    image.verify()
            except OSError as exc:
                raise ValueError("proposal image cannot be opened as a valid image") from exc
            if image_format not in {"JPEG", "PNG", "GIF", "WEBP"}:
                raise ValueError("proposal image must be JPG, PNG, GIF, or WEBP")
            scheduled_at = proposal.scheduled_publish_at
            if not scheduled_at:
                raise ValueError("proposal has no assigned daily schedule slot")
            post = PreparedPost(
                proposal_id=proposal.id,
                planned_publish_at=scheduled_at,
                caption=proposal.final_caption,
                local_image_path=str(path),
            )
            planned = datetime.fromisoformat(post.planned_publish_at)
            channel = get_channel(session, self.settings.channel_handle)
            self._validate_channel_timestamp(planned, ZoneInfo(channel.timezone))
            if planned.astimezone(UTC) <= datetime.now(UTC) + timedelta(minutes=5):
                raise ValueError("planned publish time must be at least five minutes in the future")
            warnings: list[str] = []
            if candidate.rights_status == "unknown":
                warnings.append(
                    "Rights status is unknown. Confirm you are authorized to publish "
                    "this exact image."
                )
            for raw_warnings in (candidate.soft_warnings_json, proposal.warnings_json):
                try:
                    values = json.loads(raw_warnings or "[]")
                except json.JSONDecodeError:
                    values = []
                if isinstance(values, list):
                    warnings.extend(
                        str(value).strip()
                        for value in values
                        if isinstance(value, str) and value.strip()
                    )
            warnings = list(dict.fromkeys(warnings))
            try:
                image_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                raise ValueError("proposal image cannot be read from local storage") from exc
            return PublishingPreflight(
                post=post,
                payload_hash=self._payload_hash(post, image_sha256),
                image_url=f"/media/{relative_media.as_posix()}",
                rights_status=candidate.rights_status,
                warnings=warnings,
            )

    def _record_failure(self, attempt_id: int, detail: str) -> None:
        now = datetime.now(UTC)
        with self.database.session() as session:
            attempt = session.get(PublishAttempt, attempt_id)
            if attempt is None:
                return
            proposal = session.get(Proposal, attempt.proposal_id)
            attempt.status = "failed_before_submission"
            attempt.error_summary = detail[:2000]
            attempt.completed_at = now
            if proposal and proposal.status == ProposalStatus.PUBLISHING.value:
                require_transition(proposal.status, ProposalStatus.PUBLISH_FAILED)
                proposal.status = ProposalStatus.PUBLISH_FAILED.value
                session.add(
                    ProposalEvent(
                        proposal_id=proposal.id,
                        event_type="youtube_submission_failed",
                        old_value_json=json.dumps({"status": ProposalStatus.PUBLISHING.value}),
                        new_value_json=json.dumps({"status": ProposalStatus.PUBLISH_FAILED.value}),
                    )
                )
                audit(
                    session,
                    "youtube_submission_failed",
                    "proposal",
                    proposal.id,
                    {"attempt_id": attempt.id, "error": detail[:500]},
                )

    def _record_preflight_failure(self, attempt_id: int, detail: str) -> None:
        with self.database.session() as session:
            attempt = session.get(PublishAttempt, attempt_id)
            if attempt is None or attempt.status != "queued":
                return
            attempt.status = "blocked_session"
            attempt.error_summary = detail[:2000]
            attempt.completed_at = datetime.now(UTC)
            audit(
                session,
                "youtube_queue_blocked",
                "proposal",
                attempt.proposal_id,
                {"attempt_id": attempt.id, "error": detail[:500]},
            )

    def _consume_confirmation(
        self,
        attempt_id: int,
        *,
        confirmation_token: str,
        confirmation_phrase: str,
    ) -> PreparedPost:
        now = datetime.now(UTC)
        terminal_error: str | None = None
        post: PreparedPost | None = None
        with self.database.session() as session:
            attempt = session.get(PublishAttempt, attempt_id)
            if attempt is None:
                raise LookupError(f"publish attempt {attempt_id} not found")
            proposal = session.get(Proposal, attempt.proposal_id)
            if proposal is None:
                raise LookupError(f"proposal {attempt.proposal_id} not found")
            expires_at = self._aware(attempt.expires_at)
            if attempt.status != "prepared":
                raise ValueError(f"publish attempt is {attempt.status}, not prepared")
            if expires_at <= now:
                attempt.status = "expired"
                attempt.completed_at = now
                terminal_error = "publish confirmation expired; prepare a new attempt"
            elif not hmac.compare_digest(
                attempt.confirmation_token_hash,
                self._token_hash(confirmation_token),
            ):
                raise ValueError("publish confirmation token is invalid")
            else:
                expected_phrase = self.confirmation_phrase(proposal.id)
                if not hmac.compare_digest(confirmation_phrase.strip(), expected_phrase):
                    raise ValueError(f'type the exact confirmation phrase: "{expected_phrase}"')
                post, current_payload_hash = self._prepared_post(proposal.id)
                if not hmac.compare_digest(attempt.payload_hash, current_payload_hash):
                    attempt.status = "invalidated"
                    attempt.completed_at = now
                    terminal_error = "proposal changed after preparation; prepare a new attempt"
                else:
                    require_transition(proposal.status, ProposalStatus.PUBLISHING)
                    old_status = proposal.status
                    proposal.status = ProposalStatus.PUBLISHING.value
                    attempt.status = "submitting"
                    attempt.submitted_at = now
                    session.add(
                        ProposalEvent(
                            proposal_id=proposal.id,
                            event_type="youtube_submission_started",
                            old_value_json=json.dumps({"status": old_status}),
                            new_value_json=json.dumps(
                                {
                                    "status": ProposalStatus.PUBLISHING.value,
                                    "attempt_id": attempt.id,
                                }
                            ),
                        )
                    )
                    audit(
                        session,
                        "youtube_submission_started",
                        "proposal",
                        proposal.id,
                        {"attempt_id": attempt.id},
                    )
        if terminal_error:
            raise ValueError(terminal_error)
        if post is None:
            raise RuntimeError("publish confirmation did not produce a prepared post")
        return post

    def _consume_queued_attempt(self, attempt_id: int) -> PreparedPost:
        terminal_error: str | None = None
        post: PreparedPost | None = None
        with self.database.session() as session:
            attempt = session.get(PublishAttempt, attempt_id)
            if attempt is None:
                raise LookupError(f"publish attempt {attempt_id} not found")
            proposal = session.get(Proposal, attempt.proposal_id)
            if proposal is None:
                raise LookupError(f"proposal {attempt.proposal_id} not found")
            if attempt.status != "queued":
                raise ValueError(f"publish attempt is {attempt.status}, not queued")
            post, current_payload_hash = self._prepared_post(
                proposal.id,
                allowed_statuses={
                    ProposalStatus.INTERNALLY_SCHEDULED.value,
                    ProposalStatus.PUBLISH_FAILED.value,
                },
            )
            if not hmac.compare_digest(attempt.payload_hash, current_payload_hash):
                attempt.status = "invalidated"
                attempt.completed_at = datetime.now(UTC)
                terminal_error = "proposal changed after it entered the scheduling queue"
            else:
                require_transition(proposal.status, ProposalStatus.PUBLISHING)
                old_status = proposal.status
                proposal.status = ProposalStatus.PUBLISHING.value
                attempt.status = "submitting"
                attempt.submitted_at = datetime.now(UTC)
                session.add(
                    ProposalEvent(
                        proposal_id=proposal.id,
                        event_type="youtube_submission_started",
                        old_value_json=json.dumps({"status": old_status}),
                        new_value_json=json.dumps(
                            {
                                "status": ProposalStatus.PUBLISHING.value,
                                "attempt_id": attempt.id,
                                "trigger": "explicit_lineup_action",
                            }
                        ),
                    )
                )
                audit(
                    session,
                    "youtube_submission_started",
                    "proposal",
                    proposal.id,
                    {"attempt_id": attempt.id, "trigger": "explicit_lineup_action"},
                )
        if terminal_error:
            raise ValueError(terminal_error)
        if post is None:
            raise RuntimeError("queued publish attempt did not produce a prepared post")
        return post

    def _record_receipt(
        self,
        attempt_id: int,
        receipt: BrowserScheduleReceipt,
    ) -> PublishResult:
        now = datetime.now(UTC)
        with self.database.session() as session:
            attempt = session.get(PublishAttempt, attempt_id)
            if attempt is None:
                raise LookupError(f"publish attempt {attempt_id} not found")
            proposal = session.get(Proposal, attempt.proposal_id)
            if proposal is None:
                raise LookupError(f"proposal {attempt.proposal_id} not found")
            target = (
                ProposalStatus.EXTERNALLY_SCHEDULED
                if receipt.verified
                else ProposalStatus.PUBLISH_UNVERIFIED
            )
            require_transition(proposal.status, target)
            old_status = proposal.status
            proposal.status = target.value
            proposal.external_post_id = receipt.external_id
            proposal.external_post_url = receipt.external_url
            if receipt.verified:
                proposal.scheduled_verified_at = now
            attempt.status = "verified" if receipt.verified else "submitted_unverified"
            attempt.external_id = receipt.external_id
            attempt.external_url = receipt.external_url
            attempt.screenshot_paths_json = json.dumps(receipt.screenshot_paths)
            attempt.error_summary = None if receipt.verified else receipt.detail
            attempt.completed_at = now
            session.add(
                ProposalEvent(
                    proposal_id=proposal.id,
                    event_type=(
                        "youtube_schedule_verified"
                        if receipt.verified
                        else "youtube_submission_unverified"
                    ),
                    old_value_json=json.dumps({"status": old_status}),
                    new_value_json=json.dumps(
                        {
                            "status": target.value,
                            "attempt_id": attempt.id,
                            "external_id": receipt.external_id,
                        }
                    ),
                )
            )
            audit(
                session,
                (
                    "youtube_schedule_verified"
                    if receipt.verified
                    else "youtube_submission_unverified"
                ),
                "proposal",
                proposal.id,
                {
                    "attempt_id": attempt.id,
                    "external_id": receipt.external_id,
                    "screenshots": receipt.screenshot_paths,
                },
            )
            proposal_id = proposal.id
        return PublishResult(
            proposal_id=proposal_id,
            status=target.value,
            external_id=receipt.external_id,
            detail=receipt.detail,
        )

    def _require_enabled(self) -> None:
        if not self.settings.authorized_browser_ready:
            raise ValueError(
                "authorized browser publishing requires RUNWAY_PUBLISHING_MODE="
                "authorized_browser, RUNWAY_PUBLISHING_ENABLED=true, and "
                "RUNWAY_YOUTUBE_AUTOMATION_AUTHORIZED=true"
            )

    @staticmethod
    def confirmation_phrase(proposal_id: int) -> str:
        return f"SCHEDULE QLOB #{proposal_id}"

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _payload_hash(self, post: PreparedPost, image_sha256: str) -> str:
        value = json.dumps(
            {
                "proposal_id": post.proposal_id,
                "channel_id": self.settings.publisher_channel_id,
                "planned_publish_at": post.planned_publish_at,
                "caption": post.caption,
                "image_sha256": image_sha256,
            },
            sort_keys=True,
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_channel_timestamp(value: datetime, timezone: ZoneInfo) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Lineup timestamp must include an explicit UTC offset")
        configured = value.astimezone(timezone)
        if (
            configured.replace(tzinfo=None) != value.replace(tzinfo=None)
            or configured.utcoffset() != value.utcoffset()
        ):
            raise ValueError(
                f"Lineup timestamp must be expressed in {timezone.key} with the correct offset"
            )
        round_trip = configured.astimezone(UTC).astimezone(timezone)
        if (
            round_trip.replace(tzinfo=None) != configured.replace(tzinfo=None)
            or round_trip.utcoffset() != configured.utcoffset()
        ):
            raise ValueError("Lineup timestamp is not a real local time in the channel timezone")
        return configured

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    @staticmethod
    def _attempt_dict(attempt: PublishAttempt) -> dict[str, object]:
        return {
            "id": attempt.id,
            "proposal_id": attempt.proposal_id,
            "publisher": attempt.publisher,
            "status": attempt.status,
            "planned_publish_at": attempt.planned_publish_at,
            "screenshot_paths": json.loads(attempt.screenshot_paths_json),
            "external_id": attempt.external_id,
            "external_url": attempt.external_url,
            "error_summary": attempt.error_summary,
            "prepared_at": attempt.prepared_at.isoformat(),
            "expires_at": attempt.expires_at.isoformat(),
            "submitted_at": (attempt.submitted_at.isoformat() if attempt.submitted_at else None),
            "completed_at": (attempt.completed_at.isoformat() if attempt.completed_at else None),
        }
