from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import threading
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import (
    CandidateImage,
    MediaAsset,
    Proposal,
    ProposalEvent,
    PublishAttempt,
)
from leeway.db.repositories import audit
from leeway.domain.enums import ProposalStatus
from leeway.domain.state_machine import require_transition
from leeway.publishing.base import (
    PreparedPost,
    PublisherSessionStatus,
    PublishPreparation,
    PublishResult,
    VerificationResult,
)


class BrowserScheduleReceipt(BaseModel):
    submitted: bool
    verified: bool
    external_id: str | None = None
    external_url: str | None = None
    screenshot_paths: list[str] = Field(default_factory=list)
    detail: str


class YouTubeBrowserAdapter(Protocol):
    async def validate_session(self) -> PublisherSessionStatus: ...

    async def schedule(self, post: PreparedPost) -> BrowserScheduleReceipt: ...

    async def verify(self, post: PreparedPost) -> BrowserScheduleReceipt: ...


class PlaywrightYouTubeAdapter:
    """Visible, persistent-profile YouTube adapter with no challenge bypass."""

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

    async def schedule(self, post: PreparedPost) -> BrowserScheduleReceipt:
        return await asyncio.to_thread(self._schedule_sync, post)

    async def verify(self, post: PreparedPost) -> BrowserScheduleReceipt:
        return await asyncio.to_thread(self._verify_sync, post)

    def login_interactive(self) -> None:
        from playwright.sync_api import sync_playwright

        with self._browser_lock, sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.settings.publisher_profile_dir),
                headless=False,
                viewport={"width": 1440, "height": 1000},
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(self.settings.publisher_channel_url, wait_until="domcontentloaded")
            input(
                "Sign into the Qlob Editor account in the visible browser. "
                "When the Qlob Posts composer is visible, return here and press Enter..."
            )
            context.close()

    def _validate_session_sync(self) -> PublisherSessionStatus:
        from playwright.sync_api import sync_playwright

        with self._browser_lock, sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.settings.publisher_profile_dir),
                headless=False,
                viewport={"width": 1440, "height": 1000},
            )
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
                    detail=(
                        "Qlob channel, Editor role, and Community composer verified."
                        if valid
                        else "Qlob Editor session is not ready; missing "
                        + ", ".join(missing)
                        + ". Run publisher login."
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
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.settings.publisher_profile_dir),
                headless=False,
                viewport={"width": 1440, "height": 1000},
            )
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
                    raise RuntimeError("Qlob Editor session validation failed")

                composer = page.locator("ytd-backstage-post-dialog-renderer")
                editor = composer.locator('#contenteditable-root[contenteditable="true"]')
                self._require_one_visible(editor, "caption editor")
                editor.fill(post.caption)

                upload = composer.locator('input[type="file"][accept="image/*"][multiple]')
                self._require_one_visible(upload, "multi-image upload input")
                upload.set_input_files(post.local_image_path)
                page.wait_for_timeout(1200)

                action_menu = composer.locator(
                    '#post-buttons-wrapper button[aria-label="Action menu"]'
                )
                self._require_one_visible(action_menu, "schedule action menu")
                if not action_menu.is_enabled():
                    raise RuntimeError("YouTube schedule action menu did not become enabled")
                action_menu.click()

                schedule_item = page.get_by_text("Schedule post", exact=True)
                self._require_one_visible(schedule_item, "Schedule post menu item")
                schedule_item.click()

                dialog = page.locator(
                    "ytd-backstage-post-schedule-dialog-renderer, "
                    "tp-yt-paper-dialog:has-text('Schedule post')"
                )
                self._require_one_visible(dialog, "schedule dialog")
                planned = datetime.fromisoformat(post.planned_publish_at)
                configured_time = planned.astimezone(ZoneInfo(self.settings.timezone))
                system_time = planned.astimezone()
                if (
                    configured_time.replace(tzinfo=None) != system_time.replace(tzinfo=None)
                    or configured_time.utcoffset() != system_time.utcoffset()
                ):
                    raise RuntimeError(
                        "the visible browser's system timezone does not match "
                        f"{self.settings.timezone}"
                    )
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
                date_type = date_input.get_attribute("type")
                date_input.fill(
                    planned.date().isoformat()
                    if date_type == "date"
                    else planned.strftime("%b %d, %Y")
                )
                time_type = time_input.get_attribute("type")
                time_input.fill(
                    planned.strftime("%H:%M")
                    if time_type == "time"
                    else planned.strftime("%I:%M %p").lstrip("0")
                )

                before = self.capture_dir / f"{timestamp}-before-schedule.png"
                page.screenshot(path=str(before), full_page=True)
                screenshots.append(str(before))

                schedule_button = dialog.get_by_role("button", name="Schedule", exact=True)
                self._require_one_visible(schedule_button, "final Schedule button")
                if not schedule_button.is_enabled():
                    raise RuntimeError("final Schedule button is disabled")
                # From this point onward a browser/process failure is ambiguous. Mark it as
                # possibly submitted before clicking so LeeWay never offers an unsafe retry.
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
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.settings.publisher_profile_dir),
                headless=False,
                viewport={"width": 1440, "height": 1000},
            )
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

    def _verify_on_page(
        self,
        page: Any,
        post: PreparedPost,
        screenshots: list[str],
    ) -> BrowserScheduleReceipt:
        scheduled_tab = page.get_by_role("tab", name="Scheduled", exact=True)
        self._require_one_visible(scheduled_tab, "Scheduled posts tab")
        scheduled_tab.click()
        page.wait_for_timeout(1200)
        caption = page.get_by_text(post.caption, exact=True)
        count = caption.count()
        external_url = None
        external_id = None
        caption_match = count == 1 and caption.is_visible()
        date_match = False
        time_match = False
        image_match = False
        if caption_match:
            card = caption.locator(
                "xpath=ancestor::*[self::ytd-backstage-post-thread-renderer "
                "or self::ytd-post-renderer][1]"
            )
            if card.count() != 1:
                caption_match = False
        if caption_match:
            card_text = " ".join(card.inner_text().lower().split())
            planned = datetime.fromisoformat(post.planned_publish_at).astimezone(
                ZoneInfo(self.settings.timezone)
            )
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
            date_match = any(marker in card_text for marker in date_markers)
            time_match = any(marker in card_text for marker in time_markers)
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
        verified = caption_match and date_match and time_match and image_match
        missing = [
            label
            for label, matched in (
                ("exact caption", caption_match),
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
                "in Qlob's Scheduled tab."
                if verified
                else "Qlob Scheduled-tab verification was incomplete; missing "
                + ", ".join(missing)
                + ". Do not submit again."
            ),
        )

    def _session_contract_valid(self, page: Any) -> bool:
        return all(self._session_contract_checks(page).values())

    def _session_contract_checks(self, page: Any) -> dict[str, bool]:
        heading = page.get_by_role("heading", name="Qlob, Verified", exact=True)
        editor = page.get_by_text("You're an editor", exact=True)
        composer = page.locator(
            'ytd-backstage-post-dialog-renderer #contenteditable-root[contenteditable="true"]'
        )
        return {
            "configured channel URL": self.settings.publisher_channel_id in page.url,
            "Qlob heading": heading.count() == 1 and heading.is_visible(),
            "Editor badge": editor.count() == 1 and editor.is_visible(),
            "Community composer": composer.count() == 1 and composer.is_visible(),
        }

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
    """Visible-browser publisher for explicit editorial scheduling actions."""

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
        if not self.settings.publishing_enabled:
            return PublisherSessionStatus(
                valid=False,
                publisher=self.publisher_name,
                detail="Publishing is locked by LEEWAY_PUBLISHING_ENABLED=false.",
            )
        return await self.adapter.validate_session()

    async def prepare_attempt(self, proposal_id: int) -> PublishPreparation:
        self._require_enabled()
        status = await self.adapter.validate_session()
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

    def queue_attempt(self, proposal_id: int) -> dict[str, object]:
        """Persist an approve-and-schedule request without waiting for the browser."""
        self._require_enabled()
        post, payload_hash = self._prepared_post(proposal_id)
        now = datetime.now(UTC)
        with self.database.session() as session:
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
                return self._attempt_dict(existing)
            for prepared in session.scalars(
                select(PublishAttempt).where(
                    PublishAttempt.proposal_id == proposal_id,
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
                payload_hash=payload_hash,
                planned_publish_at=post.planned_publish_at,
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
                    "planned_publish_at": post.planned_publish_at,
                    "trigger": "human_approve_and_schedule",
                },
            )
            return self._attempt_dict(attempt)

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
        """Schedule one persisted editorial approval; callers serialize the queue."""
        self._require_enabled()
        await asyncio.to_thread(self._confirm_lock.acquire)
        try:
            session_status = await self.adapter.validate_session()
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

    def _prepared_post(
        self,
        proposal_id: int,
        *,
        allowed_statuses: set[str] | None = None,
    ) -> tuple[PreparedPost, str]:
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
            if candidate and candidate.rights_status == "blocked":
                raise ValueError("a blocked image cannot be scheduled")
            path = self.settings.resolved_data_dir / media.local_path
            if not path.is_file():
                raise ValueError("proposal image is missing from local storage")
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
            if planned.tzinfo is None:
                raise ValueError("planned publish time must include a timezone")
            configured = planned.astimezone(ZoneInfo(self.settings.timezone))
            if (
                configured.replace(tzinfo=None) != planned.replace(tzinfo=None)
                or configured.utcoffset() != planned.utcoffset()
            ):
                raise ValueError(
                    f"planned publish time must be expressed in {self.settings.timezone}"
                )
            if planned.astimezone(UTC) <= datetime.now(UTC) + timedelta(minutes=5):
                raise ValueError("planned publish time must be at least five minutes in the future")
            payload_hash = self._payload_hash(post, hashlib.sha256(path.read_bytes()).hexdigest())
            return post, payload_hash

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
                                "trigger": "approve_and_schedule",
                            }
                        ),
                    )
                )
                audit(
                    session,
                    "youtube_submission_started",
                    "proposal",
                    proposal.id,
                    {"attempt_id": attempt.id, "trigger": "approve_and_schedule"},
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
        if not self.settings.publishing_enabled:
            raise ValueError("publishing is locked by LEEWAY_PUBLISHING_ENABLED=false")

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
