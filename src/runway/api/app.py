from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from runway import __version__
from runway.api.catalog_routes import build_catalog_router
from runway.api.discovery_routes import build_discovery_router
from runway.api.identity_hook_routes import build_identity_hook_router
from runway.api.intelligence_routes import build_intelligence_router
from runway.api.platform_routes import build_platform_router
from runway.api.proposal_routes import build_proposal_router
from runway.api.security import LocalRequestGuard, loopback_origins
from runway.api.v1.errors import install_problem_handlers
from runway.api.v1.events_routes import build_events_router
from runway.api.v1.media_routes import build_media_router
from runway.api.v1.posts_routes import build_cross_post_router, build_posts_router
from runway.api.workspace_routes import build_workspace_router
from runway.auth.principal import get_principal
from runway.config import Settings, get_settings
from runway.db.base import Database
from runway.db.bootstrap import DatabaseNotCurrent, migration_head, require_database_at_head
from runway.hosted.app_setup import configure_hosted, hosted_settings_view
from runway.logging import configure_logging
from runway.operations.revision import checkout_revision
from runway.platforms.sync import SyncService

logger = logging.getLogger(__name__)


def _startup_summary(settings: Settings) -> str:
    # Mode switches only. Client ids, secrets and API keys are never logged.
    return (
        "runway api start "
        f"version={__version__} git={checkout_revision()} "
        f"platform_provider={settings.platform_provider} "
        f"publishing_enabled={settings.publishing_enabled} "
        f"publishing_mode={settings.publishing_mode} "
        f"youtube_automation_authorized={settings.youtube_automation_authorized} "
        f"inbox_replies_enabled={settings.inbox_replies_enabled} "
        f"agent_runtime={settings.agent_runtime} "
        f"sync_interval_minutes={settings.platform_sync_interval_minutes} "
        f"data_dir={settings.resolved_data_dir}"
    )


def _lifespan(
    database: Database, settings: Settings
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # The API never migrates. A database behind head means `runway db
        # upgrade` was not run; refusing here makes uvicorn exit non-zero.
        try:
            require_database_at_head(settings)
        except DatabaseNotCurrent as exc:
            logger.error(str(exc), extra={"event_type": "api_refused"})
            raise
        logger.info(_startup_summary(settings), extra={"event_type": "api_start"})
        try:
            SyncService(database, settings).mark_stale_runs_failed()
        except Exception:  # noqa: BLE001
            logger.exception("stale sync-run cleanup failed during startup")
        # Scheduled platform sync is produced and run by `runway worker`; the
        # API process runs no timer thread (audit C8).
        try:
            yield
        finally:
            logger.info("runway api stop", extra={"event_type": "api_stop"})

    return lifespan


def _add_documentation_routes(application: FastAPI) -> None:
    """The OpenAPI document and its viewers, behind the principal like every route."""
    principal = [Depends(get_principal)]

    @application.get("/openapi.json", include_in_schema=False, dependencies=principal)
    def openapi_document() -> JSONResponse:
        return JSONResponse(application.openapi())

    @application.get("/docs", include_in_schema=False, dependencies=principal)
    def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{application.title} docs")

    @application.get("/redoc", include_in_schema=False, dependencies=principal)
    def redoc() -> HTMLResponse:
        return get_redoc_html(openapi_url="/openapi.json", title=f"{application.title} docs")


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    # No migrations and no database connection here: importing the module or
    # building the app must never alter whatever database .env points at.
    database = Database(active_settings)
    configure_logging()
    hosted = active_settings.profile == "hosted"
    # Identity of the running code, so the launcher can tell a stale API
    # (older checkout or older schema) from a current one.
    revision = checkout_revision()
    schema_head = migration_head(active_settings)

    application = FastAPI(
        title="Runway API",
        version=__version__,
        description=(
            "Local-only YouTube Community-post intelligence and planning API "
            f"for the configured {active_settings.channel_name} channel"
        ),
        lifespan=_lifespan(database, active_settings),
        # FastAPI's built-in docs routes cannot take dependencies; they are
        # re-added below behind get_principal (local only).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.settings = active_settings
    install_problem_handlers(application)
    application.state.database = database
    if hosted:
        # Web-origin CORS and origin guard, rate limits, security headers and
        # the identity provider (spec 05 sections 2 and 6).
        configure_hosted(application, active_settings)
    else:
        web_origins = loopback_origins(active_settings.web_port)
        application.add_middleware(
            CORSMiddleware,
            allow_origins=web_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            # v1 clients send If-Match and Idempotency-Key and read ETag (spec 06 section 6).
            allow_headers=["Content-Type", "If-Match", "Idempotency-Key", "Last-Event-ID"],
            expose_headers=["ETag", "Idempotent-Replayed"],
        )
        # Added last so it runs first, before CORS handling and every route.
        application.add_middleware(LocalRequestGuard, allowed_origins=web_origins)
        # Local-only surfaces (spec 01 section 7): media on local disk, capture,
        # catalogue, discovery, intelligence, the proposal/Lineup flow and the
        # browser publisher with its connector. Hosted does not mount them (404).
        application.mount(
            "/media",
            StaticFiles(directory=active_settings.resolved_data_dir / "media"),
            name="media",
        )
        application.include_router(build_catalog_router(database, active_settings))
        application.include_router(build_discovery_router(database, active_settings))
        application.include_router(build_intelligence_router(database, active_settings))
        application.include_router(build_proposal_router(database, active_settings))
        # Accounts, content, inbox, drafts and overview read and write the
        # global platform tables with no workspace filter and no scope checks,
        # so a member of one hosted workspace would reach another's accounts.
        # Hosted serves none of it (404) until the workspace-tenancy work
        # scopes every query by principal.workspace_id and calls require().
        application.include_router(build_platform_router(database, active_settings))
        # Spec 03 section 8. Nothing here publishes: a release only queues a job
        # for the separate `runway worker` process. Local only until the tenancy
        # work proves workspace isolation on Postgres for these routes too.
        from runway.posts.bridge import ProposalBridge
        from runway.posts.runtime import build_media_service, build_post_service

        post_service = build_post_service(active_settings, database)
        media_service = build_media_service(
            active_settings,
            database,
            registry=post_service.registry,
            stores=post_service.media_stores,
        )
        application.state.post_service = post_service
        application.state.media_service = media_service
        application.include_router(build_posts_router(post_service))
        application.include_router(build_media_router(media_service))
        application.include_router(
            build_cross_post_router(ProposalBridge(post_service, media_service))
        )
    application.include_router(build_workspace_router(database, active_settings))
    # Versioned surface (spec 06): every router comes from build_v1_router.
    application.include_router(build_events_router(database))
    if hosted and active_settings.identity_provider.strip() == "clerk":
        if not active_settings.clerk_webhook_secrets:
            # The API receives the membership webhooks; without the secret it
            # could verify none. Migrations and the worker do not need it.
            raise ValueError(
                "RUNWAY_PROFILE=hosted requires a Clerk webhook signing secret "
                "(RUNWAY_CLERK_WEBHOOK_SECRET) for the API"
            )
        application.include_router(build_identity_hook_router(database, active_settings))

    @application.get("/health", tags=["system"])
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "product": active_settings.product_name,
            "version": __version__,
            "publishing_enabled": active_settings.publishing_enabled,
            "publishing_trigger": "explicit_lineup_action",
            "publishing_mode": active_settings.publishing_mode,
            "one_bot_post_per_day": True,
            "scheduling_horizon_days": None,
            "platform_provider": active_settings.platform_provider,
            "revision": revision,
            "migration_head": schema_head,
            # Scheduled sync runs in `runway worker`; this API runs no sync
            # thread. Older APIs lack the field (they synced in-process).
            "sync_scheduler": "worker",
        }

    if not hosted:
        _add_documentation_routes(application)

    @application.get("/api/settings", tags=["settings"], dependencies=[Depends(get_principal)])
    def settings_view() -> dict[str, object]:
        if hosted:
            # Tenants see product switches, not the operator's paths or accounts.
            return hosted_settings_view(active_settings)
        return active_settings.public_dict()

    return application


app = create_app()
