from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from runway.auth.principal import LOCAL_WORKSPACE_ID, Principal, get_principal
from runway.auth.problems import Problem
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    ConnectedAccount,
    OAuthState,
    SyncRun,
    utcnow,
)
from runway.hosted.quotas import require_connected_account_slot
from runway.jobs.queue import JobQueue
from runway.operations.instance_lease import StandbyError
from runway.platforms.account_views import account_analytics_view
from runway.platforms.account_views import account_view as _account_view
from runway.platforms.base import (
    ProviderError,
    TokenSet,
    iso_utc,
)
from runway.platforms.capabilities import (
    Platform,
)
from runway.platforms.content import ContentService
from runway.platforms.drafts import DraftService, DuplicateDraftTarget
from runway.platforms.inbox import InboxReplyForbidden, InboxService
from runway.platforms.instagram import InstagramProvider
from runway.platforms.instagram_unified import InstagramUnifiedProvider
from runway.platforms.oauth import OAuthStateError, OAuthStateService, read_state_storage
from runway.platforms.overview import OverviewService
from runway.platforms.registry import ProviderRegistry, parse_platform
from runway.platforms.sync import SyncAccountDisconnected, SyncRunInProgress, SyncService
from runway.platforms.tokens import pkce_challenge
from runway.realtime import subscriptions

logger = logging.getLogger(__name__)


INSTAGRAM_KNOWN_SCOPES = (
    "instagram_business_basic",
    "instagram_business_manage_insights",
    "instagram_business_manage_comments",
    "instagram_business_manage_messages",
    "instagram_business_content_publish",
)


class ConnectRequest(BaseModel):
    """Optional connect body; `adapter` picks a non-default connection flow."""

    adapter: Literal["instagram_unified"] | None = None


class UnifiedSelectRequest(BaseModel):
    state: str = Field(min_length=8, max_length=200)
    external_account_id: str = Field(min_length=1, max_length=200)


class ImportTokenRequest(BaseModel):
    access_token: str = Field(min_length=8, max_length=2000)
    granted_scopes: list[
        Literal[
            "instagram_business_basic",
            "instagram_business_manage_insights",
            "instagram_business_manage_comments",
            "instagram_business_manage_messages",
            "instagram_business_content_publish",
        ]
    ] = ["instagram_business_basic"]


class ConfirmedRequest(BaseModel):
    confirmed: Literal[True]


class GroupCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content_ids: list[int] = Field(default_factory=list, max_length=500)


class GroupAssignRequest(BaseModel):
    group_id: int | None = Field(default=None, ge=1)


class InboxStatusRequest(BaseModel):
    status: Literal["new", "read", "answered", "escalated", "ignored"]


class DraftTextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class ReplyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    confirmed: Literal[True]


class DraftTargetRequest(BaseModel):
    platform: Literal["youtube", "instagram", "tiktok"]
    account_id: int | None = Field(default=None, ge=1)
    caption_override: str | None = Field(default=None, max_length=8000)


class DraftCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=20000)
    scheduled_for: datetime | None = None
    notes: str | None = Field(default=None, max_length=4000)
    targets: list[DraftTargetRequest] = Field(default_factory=list, max_length=20)


class DraftUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = Field(default=None, max_length=20000)
    scheduled_for: datetime | None = None
    clear_schedule: bool = False
    notes: str | None = Field(default=None, max_length=4000)
    status: Literal["draft", "scheduled"] | None = None


class MarkPublishedRequest(BaseModel):
    published_url: str = Field(min_length=1, max_length=2000)
    confirmed: Literal[True]


REQUIRED_ENV: dict[Platform, list[str]] = {
    Platform.youtube: ["RUNWAY_YOUTUBE_CLIENT_ID", "RUNWAY_YOUTUBE_CLIENT_SECRET"],
    Platform.instagram: ["RUNWAY_INSTAGRAM_APP_ID", "RUNWAY_INSTAGRAM_APP_SECRET"],
    Platform.tiktok: ["RUNWAY_TIKTOK_CLIENT_KEY", "RUNWAY_TIKTOK_CLIENT_SECRET"],
}


def _run_view(run: SyncRun) -> dict[str, object]:
    try:
        steps = json.loads(run.steps_json or "[]")
    except json.JSONDecodeError:
        steps = []
    return {
        "id": run.id,
        "account_id": run.account_id,
        "trigger": run.trigger,
        "status": run.status,
        "started_at": iso_utc(run.started_at),
        "completed_at": iso_utc(run.completed_at),
        "steps": steps,
        "items_fetched": run.items_fetched,
        "quota_units_estimate": run.quota_units_estimate,
        "error_summary": run.error_summary,
    }


def _enrich_token(
    platform: Platform,
    token: TokenSet,
    identity_raw: dict[str, object],
    external_account_id: str,
) -> TokenSet:
    extra = dict(token.extra)
    if platform is Platform.youtube:
        extra["channel_id"] = external_account_id
        uploads = identity_raw.get("uploads_playlist")
        if isinstance(uploads, str) and uploads:
            extra["uploads_playlist"] = uploads
        else:
            details = identity_raw.get("contentDetails")
            if isinstance(details, dict):
                related = details.get("relatedPlaylists")
                if isinstance(related, dict) and isinstance(related.get("uploads"), str):
                    extra["uploads_playlist"] = str(related["uploads"])
    elif platform is Platform.instagram:
        extra["user_id"] = external_account_id
    return token.model_copy(update={"extra": extra})


def _state_meta(record: OAuthState) -> dict[str, object]:
    """The JSON payload a non-PKCE state stored (adapter choice, scopes)."""
    _verifier, payload = read_state_storage(record)
    if payload is None:
        return {}
    try:
        meta = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _state_requested_scopes(record: OAuthState) -> list[str] | None:
    """The scope list an incremental-consent state stored, when it has one."""
    scopes = _state_meta(record).get("scopes")
    if not isinstance(scopes, list):
        return None
    return [str(scope) for scope in scopes]


def build_platform_router(
    database: Database,
    settings: Settings,
    *,
    registry: ProviderRegistry | None = None,
    oauth_states: OAuthStateService | None = None,
    unified_provider: InstagramUnifiedProvider | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["platforms"], dependencies=[Depends(get_principal)])
    if registry is None:
        registry = ProviderRegistry(database, settings)
    queue = JobQueue(database)
    if oauth_states is None:
        oauth_states = OAuthStateService(database)
    sync_service = SyncService(database, settings, registry)
    content_service = ContentService(database)
    inbox_service = InboxService(database, settings, registry)
    draft_service = DraftService(database, settings)
    overview_service = OverviewService(database, settings)

    def _require_active_instance() -> None:
        # A standby machine (private instance lease held elsewhere) must not mint,
        # store or revoke tokens: TikTok rotation would break the active machine.
        try:
            registry.token_store.ensure_writable()
        except StandbyError as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc

    def _persist_connection(
        platform: Platform,
        token: TokenSet,
        scopes: list[str],
        scopes_source: str = "granted",
        *,
        workspace_id: int = LOCAL_WORKSPACE_ID,
    ) -> ConnectedAccount:
        registry.token_store.ensure_writable()
        provider = registry.provider(platform)
        identity = provider.fetch_identity(token)
        token = _enrich_token(platform, token, identity.raw, identity.external_account_id)
        now = utcnow()
        with database.session() as session:
            account = session.scalar(
                select(ConnectedAccount).where(
                    ConnectedAccount.platform == platform.value,
                    ConnectedAccount.external_account_id == identity.external_account_id,
                )
            )
            if account is None or account.status == "disconnected":
                # A new or disconnected account takes a connection slot.
                require_connected_account_slot(session, settings, workspace_id)
            if account is None:
                account = ConnectedAccount(
                    platform=platform.value,
                    external_account_id=identity.external_account_id,
                    token_ref=registry.token_store.new_token_ref(),
                )
                session.add(account)
            if account.token_ref is None:
                # Disconnect clears token_ref; a reconnect needs a fresh one.
                account.token_ref = registry.token_store.new_token_ref()
            account.handle = identity.handle
            account.display_name = identity.display_name
            account.avatar_url = identity.avatar_url
            account.profile_url = identity.profile_url
            account.account_type = identity.account_type
            account.status = "connected"
            account.granted_scopes_json = json.dumps(scopes)
            account.connected_at = now
            account.last_verified_at = now
            account.last_error = None
            account.metadata_json = json.dumps(
                {
                    "raw": identity.raw,
                    "scopes_source": scopes_source,
                    # The connection adapter decides the account's direct
                    # publisher (instagram_direct vs instagram_unified).
                    "connection": (
                        "instagram_unified"
                        if token.extra.get("login") == "unified"
                        else platform.value
                    ),
                }
            )
            session.flush()
            # Push subscriptions join the connect transaction (spec 04
            # section 2): a YouTube WebSub row plus its subscribe job, only
            # when a public callback base exists.
            subscriptions.on_account_connected(session, queue, settings, account)
            token_ref = str(account.token_ref)
            account_id = account.id
        registry.token_store.store(token_ref, token)
        with database.session() as session:
            persisted = session.get(ConnectedAccount, account_id)
            assert persisted is not None
            session.expunge(persisted)
            return persisted

    # ------------------------------------------------------------- read APIs

    @router.get("/platforms")
    def platforms() -> dict[str, object]:
        if settings.platform_provider == "mock":
            registry.ensure_mock_accounts()
        with database.session() as session:
            accounts = session.scalars(select(ConnectedAccount)).all()
            views = [_account_view(database, settings, account) for account in accounts]
        return {
            "provider_mode": settings.platform_provider,
            "platforms": [
                {
                    "platform": platform.value,
                    "configured": registry.provider(platform).configured(),
                    "required_env": REQUIRED_ENV.get(platform, []),
                    "capabilities": [
                        entry.model_dump(mode="json")
                        for entry in registry.provider(platform).capabilities()
                    ],
                    "accounts": [view for view in views if view["platform"] == platform.value],
                }
                for platform in Platform
            ],
        }

    @router.get("/platforms/accounts")
    def accounts() -> list[dict[str, object]]:
        if settings.platform_provider == "mock":
            registry.ensure_mock_accounts()
        with database.session() as session:
            records = session.scalars(select(ConnectedAccount)).all()
            return [_account_view(database, settings, account) for account in records]

    # ---------------------------------------------------------------- OAuth

    def _persist_bound_connection(
        record_platform: Platform,
        record_account_id: int,
        token: TokenSet,
        *,
        workspace_id: int,
    ) -> str | None:
        """Attach the token to the exact account a bound state was issued for.

        Returns the redirect's error short-code on refusal. The identity check
        (spec 08 section 1): the platform identity the token belongs to must
        equal the account it was issued for. The comparison is generic -
        each provider's `fetch_identity` reports its canonical identity
        (YouTube channel id, TikTok open_id, Instagram user id) as
        `external_account_id` - so a consent-screen account switch never
        attaches someone else's grant, on any platform.
        """
        with database.session() as session:
            account = session.get(ConnectedAccount, record_account_id)
            if (
                account is None
                or account.workspace_id != workspace_id
                or account.platform != record_platform.value
            ):
                return "unknown_account"
            account_id = account.id
            expected_external_id = account.external_account_id
        provider = registry.provider(record_platform)
        identity = provider.fetch_identity(token)
        if identity.external_account_id != expected_external_id:
            return "wrong_channel"
        token = _enrich_token(record_platform, token, identity.raw, identity.external_account_id)
        now = utcnow()
        with database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            assert account is not None
            if not account.token_ref:
                account.token_ref = registry.token_store.new_token_ref()
            account.handle = identity.handle
            account.display_name = identity.display_name
            account.avatar_url = identity.avatar_url
            account.profile_url = identity.profile_url
            account.account_type = identity.account_type
            account.status = "connected"
            # The response's `scope` is the authoritative current grant set.
            account.granted_scopes_json = json.dumps(list(token.scopes))
            account.last_verified_at = now
            account.last_error = None
            account.metadata_json = json.dumps({"raw": identity.raw, "scopes_source": "granted"})
            session.flush()
            token_ref = str(account.token_ref)
        registry.token_store.store(token_ref, token)
        return None

    def _issue_connect(
        platform: Platform,
        *,
        purpose: str = "connect",
        account_id: int | None = None,
        workspace_id: int = LOCAL_WORKSPACE_ID,
    ) -> dict[str, object]:
        if settings.platform_provider == "mock":
            raise HTTPException(
                status_code=400,
                detail="OAuth connect is unavailable in mock provider mode",
            )
        _require_active_instance()
        provider = registry.provider(platform)
        if not provider.configured():
            raise HTTPException(
                status_code=409,
                detail=f"{platform.value} OAuth is not configured",
            )
        use_pkce = platform is Platform.youtube
        issued = oauth_states.issue(
            platform,
            redirect_uri=registry.redirect_uri(platform),
            use_pkce=use_pkce,
            purpose=purpose,
            account_id=account_id,
            workspace_id=workspace_id,
        )
        authorization_url = provider.authorization_url(
            state=issued.state,
            redirect_uri=registry.redirect_uri(platform),
            code_challenge=(pkce_challenge(issued.code_verifier) if issued.code_verifier else None),
        )
        return {"authorization_url": authorization_url, "state": issued.state}

    def _unified() -> InstagramUnifiedProvider:
        return unified_provider or InstagramUnifiedProvider(settings)

    def _issue_unified_connect() -> dict[str, object]:
        if settings.platform_provider == "mock":
            raise HTTPException(
                status_code=400,
                detail="OAuth connect is unavailable in mock provider mode",
            )
        provider = _unified()
        if not provider.configured():
            raise HTTPException(
                status_code=409,
                detail=(
                    "instagram_unified OAuth is not configured "
                    "(RUNWAY_META_APP_ID, RUNWAY_META_APP_SECRET, "
                    "RUNWAY_INSTAGRAM_UNIFIED_CONFIG_ID)"
                ),
            )
        redirect_uri = registry.redirect_uri(Platform.instagram)
        issued = oauth_states.issue(
            Platform.instagram,
            redirect_uri=redirect_uri,
            use_pkce=False,
            payload=json.dumps({"adapter": "instagram_unified"}),
        )
        return {
            "authorization_url": provider.authorization_url(
                state=issued.state,
                redirect_uri=redirect_uri,
                code_challenge=None,
            ),
            "state": issued.state,
        }

    def _require_slot(principal: Principal) -> None:
        with database.session() as session:
            require_connected_account_slot(session, settings, principal.workspace_id)

    @router.post("/platforms/{platform}/connect")
    def connect(
        platform: str,
        payload: ConnectRequest | None = None,
        principal: Principal = Depends(get_principal),
    ) -> dict[str, object]:
        try:
            parsed = parse_platform(platform)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _require_slot(principal)
        adapter = payload.adapter if payload is not None else None
        if adapter == "instagram_unified":
            if parsed is not Platform.instagram:
                raise HTTPException(
                    status_code=400,
                    detail="instagram_unified is an Instagram connection adapter",
                )
            return _issue_unified_connect()
        return _issue_connect(parsed)

    def _unified_callback(
        code: str,
        record: OAuthState,
        principal: Principal,
        fail: Callable[[str], RedirectResponse],
    ) -> Response:
        """Unified Login callback (spec 08 section 3): exchange the code, list
        assigned accounts, persist the only one or defer to the owner pick."""
        web_base = settings.resolved_web_base_url
        provider = _unified()
        try:
            token = provider.exchange_code(
                code=code, redirect_uri=record.redirect_uri, code_verifier=None
            )
            token = provider.attach_business_id(token)
            candidates = provider.assigned_accounts(token)
        except ProviderError as exc:
            logger.warning("unified oauth callback failed: %s", exc.detail)
            return fail("exchange_failed")
        except Exception:  # noqa: BLE001
            logger.exception("unified oauth callback failed")
            return fail("exchange_failed")
        if not candidates:
            return fail("no_instagram_accounts")
        if len(candidates) == 1:
            token.extra["user_id"] = candidates[0]["id"]
            try:
                _persist_connection(
                    Platform.instagram,
                    token,
                    token.scopes,
                    workspace_id=principal.workspace_id,
                )
            except Problem as exc:
                return fail(exc.code)
            except ProviderError:
                return fail("exchange_failed")
            return RedirectResponse(
                url=f"{web_base}/accounts?connected=instagram",
                status_code=303,
            )
        # Several accounts are assigned: hold the token under a temporary
        # ref and let the owner pick through the select endpoints.
        token_ref = registry.token_store.new_token_ref()
        registry.token_store.store(token_ref, token)
        issued = oauth_states.issue(
            Platform.instagram,
            redirect_uri=record.redirect_uri,
            use_pkce=False,
            payload=json.dumps(
                {
                    "adapter": "instagram_unified",
                    "select": True,
                    "token_ref": token_ref,
                    "accounts": candidates,
                }
            ),
        )
        return RedirectResponse(
            url=f"{web_base}/accounts?select_instagram={issued.state}",
            status_code=303,
        )

    def _select_meta(state: str) -> dict[str, object] | None:
        record = oauth_states.peek(Platform.instagram, state=state)
        if record is None:
            return None
        meta = _state_meta(record)
        if meta.get("adapter") != "instagram_unified" or not meta.get("select"):
            return None
        return meta

    @router.get("/platforms/instagram/unified/select")
    def unified_select_list(state: str = Query(min_length=1)) -> dict[str, object]:
        meta = _select_meta(state)
        if meta is None:
            raise HTTPException(status_code=404, detail="no pending Instagram account pick")
        return {"state": state, "accounts": meta.get("accounts") or []}

    @router.post("/platforms/instagram/unified/select")
    def unified_select(
        payload: UnifiedSelectRequest,
        principal: Principal = Depends(get_principal),
    ) -> Response:
        web_base = settings.resolved_web_base_url

        def fail(short_code: str) -> RedirectResponse:
            return RedirectResponse(
                url=f"{web_base}/accounts?error={short_code}",
                status_code=303,
            )

        # Peek first: a pick the state never offered is rejected without
        # consuming it, so the owner can still choose a valid account.
        meta = _select_meta(payload.state)
        if meta is None:
            raise HTTPException(status_code=404, detail="no pending Instagram account pick")
        candidates = meta.get("accounts")
        accounts = (
            [a for a in candidates if isinstance(a, dict)] if isinstance(candidates, list) else []
        )
        chosen = next((a for a in accounts if a.get("id") == payload.external_account_id), None)
        if chosen is None:
            raise HTTPException(
                status_code=400,
                detail="that Instagram account is not among the assigned ones",
            )
        try:
            oauth_states.consume(Platform.instagram, state=payload.state)
        except OAuthStateError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        token_ref = str(meta.get("token_ref") or "")
        token = registry.token_store.load(token_ref) if token_ref else None
        if token is None:
            raise HTTPException(status_code=410, detail="the held token is gone; connect again")
        token.extra["user_id"] = chosen["id"]
        try:
            account = _persist_connection(
                Platform.instagram,
                token,
                token.scopes,
                workspace_id=principal.workspace_id,
            )
        except Problem as exc:
            return fail(exc.code)
        except ProviderError:
            return fail("exchange_failed")
        finally:
            # The held ref was a temporary parking spot for the pick; the
            # persisted connection stores its own token.
            if token_ref:
                registry.token_store.delete(token_ref)
        return JSONResponse(
            jsonable_encoder(
                {"status": "connected", "account": _account_view(database, settings, account)}
            )
        )

    @router.get("/platforms/{platform}/oauth/callback")
    def oauth_callback(
        platform: str,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        principal: Principal = Depends(get_principal),
    ) -> Response:
        web_base = settings.resolved_web_base_url

        def fail(short_code: str) -> RedirectResponse:
            return RedirectResponse(
                url=f"{web_base}/accounts?error={short_code}",
                status_code=303,
            )

        try:
            parsed = parse_platform(platform)
        except ValueError:
            return fail("unknown_platform")
        if error:
            return fail("denied" if error == "access_denied" else "provider_error")
        if not code or not state:
            return fail("missing_code")
        try:
            registry.token_store.ensure_writable()
        except StandbyError:
            # Standby check before the code exchange and before consuming the
            # state: a standby machine must not burn the single-use
            # authorization code on a token it could never store.
            return fail("standby")
        try:
            record = oauth_states.consume(parsed, state=state)
        except OAuthStateError as exc:
            return fail(str(exc))
        if (
            parsed is Platform.instagram
            and _state_meta(record).get("adapter") == "instagram_unified"
        ):
            return _unified_callback(code, record, principal, fail)
        provider = registry.provider(parsed)
        verifier, _payload = read_state_storage(record)
        requested_scopes = _state_requested_scopes(record)
        try:
            if requested_scopes and isinstance(provider, InstagramProvider):
                # Incremental consent: the state named the scopes the consent
                # screen showed; Meta may omit `permissions` in its response.
                token = provider.exchange_code(
                    code=code,
                    redirect_uri=record.redirect_uri,
                    code_verifier=verifier,
                    requested_scopes=requested_scopes,
                )
            else:
                token = provider.exchange_code(
                    code=code,
                    redirect_uri=record.redirect_uri,
                    code_verifier=verifier,
                )
            if record.account_id is not None:
                # A bound flow (reconnect, enable_publishing) must attach to
                # the exact account it was issued for, identity included.
                refusal = _persist_bound_connection(
                    parsed,
                    record.account_id,
                    token,
                    workspace_id=principal.workspace_id,
                )
                if refusal is not None:
                    return fail(refusal)
            else:
                _persist_connection(
                    parsed, token, token.scopes, workspace_id=principal.workspace_id
                )
        except Problem as exc:
            # The workspace reached its connected-accounts quota meanwhile.
            return fail(exc.code)
        except ProviderError as exc:
            logger.warning("oauth callback failed for %s: %s", platform, exc.detail)
            return fail("exchange_failed")
        except Exception:  # noqa: BLE001
            logger.exception("oauth callback failed for %s", platform)
            return fail("exchange_failed")
        outcome = "publishing_enabled" if record.purpose == "enable_publishing" else "connected"
        return RedirectResponse(
            url=f"{web_base}/accounts?{outcome}={parsed.value}",
            status_code=303,
        )

    @router.post("/platforms/instagram/import-token")
    def import_instagram_token(
        payload: ImportTokenRequest, principal: Principal = Depends(get_principal)
    ) -> dict[str, object]:
        if settings.platform_provider == "mock":
            raise HTTPException(
                status_code=400,
                detail="token import is unavailable in mock provider mode",
            )
        _require_active_instance()
        provider = registry.provider(Platform.instagram)
        token = TokenSet(access_token=payload.access_token)
        try:
            provider.fetch_identity(token)
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=exc.detail) from exc
        if token.expires_at is None and isinstance(provider, InstagramProvider):
            with contextlib.suppress(ProviderError):
                token = provider.exchange_long_lived(token)
        scopes = list(dict.fromkeys(str(scope) for scope in payload.granted_scopes))
        account = _persist_connection(
            Platform.instagram,
            token,
            scopes,
            scopes_source="user_declared",
            workspace_id=principal.workspace_id,
        )
        return {"account_id": account.id, "platform": "instagram"}

    # ----------------------------------------------------------- account ops

    def _account_or_404(account_id: int) -> ConnectedAccount:
        with database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            if account is None:
                raise HTTPException(status_code=404, detail="account not found")
            session.expunge(account)
            return account

    @router.post("/platforms/accounts/{account_id}/disconnect")
    def disconnect(account_id: int, payload: ConfirmedRequest) -> dict[str, object]:
        account = _account_or_404(account_id)
        _require_active_instance()
        if account.token_ref:
            token = registry.token_store.load(account.token_ref)
            if token is not None:
                try:
                    registry.provider(Platform(account.platform)).revoke(token)
                except ProviderError as exc:
                    logger.warning(
                        "token revoke failed for account %s: %s",
                        account_id,
                        exc.detail,
                    )
            registry.token_store.delete(account.token_ref)
        with database.session() as session:
            record = session.get(ConnectedAccount, account_id)
            if record is not None:
                record.status = "disconnected"
                record.token_ref = None
                record.last_error = None
                # WebSub subscriptions are cancelled and an unsubscribe job is
                # enqueued in the same transaction (spec 04 section 2).
                subscriptions.on_account_disconnected(session, queue, record)
        return {"account_id": account_id, "status": "disconnected"}

    @router.post("/platforms/accounts/{account_id}/reconnect")
    def reconnect(
        account_id: int, principal: Principal = Depends(get_principal)
    ) -> dict[str, object]:
        account = _account_or_404(account_id)
        if account.status == "disconnected":
            _require_slot(principal)
        return _issue_connect(
            Platform(account.platform),
            purpose="reconnect",
            account_id=account_id,
            workspace_id=principal.workspace_id,
        )

    @router.post("/platforms/accounts/{account_id}/sync")
    def sync_account(account_id: int) -> Response:
        _account_or_404(account_id)
        try:
            run = sync_service.start_background(account_id, trigger="manual")
        except (SyncRunInProgress, SyncAccountDisconnected) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(
            content=json.dumps({"run_id": run.id}),
            status_code=202,
            media_type="application/json",
        )

    @router.get("/platforms/sync-runs/{run_id}")
    def sync_run(run_id: int) -> dict[str, object]:
        with database.session() as session:
            run = session.get(SyncRun, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="sync run not found")
            return _run_view(run)

    @router.get("/platforms/accounts/{account_id}/sync-runs")
    def account_sync_runs(
        account_id: int, limit: int = Query(default=20, ge=1, le=100)
    ) -> list[dict[str, object]]:
        _account_or_404(account_id)
        with database.session() as session:
            runs = session.scalars(
                select(SyncRun)
                .where(SyncRun.account_id == account_id)
                .order_by(SyncRun.started_at.desc())
                .limit(limit)
            ).all()
            return [_run_view(run) for run in runs]

    # ---------------------------------------------------------- read services

    @router.get("/overview")
    def overview() -> dict[str, object]:
        if settings.platform_provider == "mock":
            registry.ensure_mock_accounts()
        with database.session() as session:
            records = session.scalars(select(ConnectedAccount)).all()
            views = [_account_view(database, settings, account) for account in records]
        return overview_service.summary(views)

    @router.get("/content")
    def content_list(
        platform: str | None = None,
        account_id: int | None = Query(default=None, ge=1),
        type: str | None = None,  # noqa: A002
        q: str | None = Query(default=None, max_length=200),
        sort: Literal["published_at", "view_count", "like_count", "comment_count"] = "published_at",
        order: Literal["asc", "desc"] = "desc",
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        return content_service.list_items(
            platform=platform,
            account_id=account_id,
            content_type=type,
            query=q,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )

    @router.get("/content/{content_id}")
    def content_detail(content_id: int) -> dict[str, object]:
        view = content_service.get_item(content_id)
        if view is None:
            raise HTTPException(status_code=404, detail="content not found")
        return view

    @router.post("/content/groups")
    def content_group_create(payload: GroupCreateRequest) -> dict[str, object]:
        return content_service.create_group(payload.title, payload.content_ids)

    @router.post("/content/{content_id}/group")
    def content_group_assign(content_id: int, payload: GroupAssignRequest) -> dict[str, object]:
        try:
            return content_service.set_group(content_id, payload.group_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/analytics/accounts/{account_id}")
    def account_analytics(
        account_id: int,
        range: Literal["7", "28", "90"] = "28",  # noqa: A002
    ) -> dict[str, object]:
        return account_analytics_view(database, _account_or_404(account_id), int(range))

    @router.get("/inbox")
    def inbox_list(
        platform: str | None = None,
        account_id: int | None = Query(default=None, ge=1),
        status: str | None = None,
        unanswered: bool = False,
        content_id: int | None = Query(default=None, ge=1),
        q: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        return inbox_service.list_comments(
            platform=platform,
            account_id=account_id,
            status=status,
            unanswered=unanswered,
            content_id=content_id,
            query=q,
            limit=limit,
            offset=offset,
        )

    @router.get("/inbox/{comment_id}")
    def inbox_detail(comment_id: int) -> dict[str, object]:
        view = inbox_service.get_comment(comment_id)
        if view is None:
            raise HTTPException(status_code=404, detail="comment not found")
        return view

    @router.patch("/inbox/{comment_id}")
    def inbox_set_status(comment_id: int, payload: InboxStatusRequest) -> dict[str, object]:
        try:
            return inbox_service.set_status(comment_id, payload.status)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.put("/inbox/{comment_id}/draft")
    def inbox_set_draft(comment_id: int, payload: DraftTextRequest) -> dict[str, object]:
        try:
            return inbox_service.set_draft(comment_id, payload.text)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/inbox/{comment_id}/draft")
    def inbox_delete_draft(comment_id: int) -> dict[str, object]:
        return inbox_service.delete_draft(comment_id)

    @router.post("/inbox/{comment_id}/reply")
    def inbox_reply(
        comment_id: int, payload: ReplyRequest, principal: Principal = Depends(get_principal)
    ) -> dict[str, object]:
        try:
            return inbox_service.reply(
                comment_id, payload.text, workspace_id=principal.workspace_id
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InboxReplyForbidden as exc:
            raise HTTPException(status_code=403, detail=exc.detail) from exc
        except StandbyError as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=exc.detail) from exc

    @router.get("/drafts")
    def drafts_list(status: str | None = None) -> list[dict[str, object]]:
        return draft_service.list_drafts(status=status)

    @router.post("/drafts")
    def drafts_create(payload: DraftCreateRequest) -> dict[str, object]:
        try:
            return draft_service.create_draft(
                title=payload.title,
                body=payload.body,
                scheduled_for=payload.scheduled_for,
                notes=payload.notes,
                targets=[target.model_dump() for target in payload.targets],
            )
        except DuplicateDraftTarget as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.patch("/drafts/{draft_id}")
    def drafts_update(draft_id: int, payload: DraftUpdateRequest) -> dict[str, object]:
        try:
            return draft_service.update_draft(
                draft_id,
                title=payload.title,
                body=payload.body,
                scheduled_for=payload.scheduled_for,
                clear_schedule=payload.clear_schedule,
                notes=payload.notes,
                status=payload.status,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            # The draft state machine refused the status (e.g. a published
            # draft back to draft, or scheduled without a time).
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.delete("/drafts/{draft_id}")
    def drafts_archive(draft_id: int) -> dict[str, object]:
        try:
            return draft_service.archive_draft(draft_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/drafts/{draft_id}/targets/{target_id}/mark-published")
    def drafts_mark_published(
        draft_id: int, target_id: int, payload: MarkPublishedRequest
    ) -> dict[str, object]:
        try:
            return draft_service.mark_published(draft_id, target_id, payload.published_url)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router
