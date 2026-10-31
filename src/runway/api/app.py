from __future__ import annotations

import logging
import subprocess
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from runway import __version__
from runway.api.catalog_routes import build_catalog_router
from runway.api.discovery_routes import build_discovery_router
from runway.api.intelligence_routes import build_intelligence_router
from runway.api.platform_routes import build_platform_router
from runway.api.proposal_routes import build_proposal_router
from runway.api.security import LocalRequestGuard, loopback_origins
from runway.config import Settings, get_settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.logging import configure_logging
from runway.platforms.sync import PlatformSyncScheduler, SyncService

logger = logging.getLogger(__name__)


def _checkout_revision() -> str:
    """Short commit of the checkout serving this API, or "unknown"."""
    repo = Path(__file__).resolve().parents[3]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else "unknown"


def _startup_summary(settings: Settings) -> str:
    # Mode switches only. Client ids, secrets and API keys are never logged.
    return (
        "runway api start "
        f"version={__version__} git={_checkout_revision()} "
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
        logger.info(_startup_summary(settings), extra={"event_type": "api_start"})
        sync_service = SyncService(database, settings)
        try:
            sync_service.mark_stale_runs_failed()
        except Exception:  # noqa: BLE001
            logger.exception("stale sync-run cleanup failed during startup")
        scheduler: PlatformSyncScheduler | None = None
        if settings.platform_sync_interval_minutes > 0:
            scheduler = PlatformSyncScheduler(sync_service, settings.platform_sync_interval_minutes)
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.stop()
            logger.info("runway api stop", extra={"event_type": "api_stop"})

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    database = initialize_database(active_settings)
    # The migration runner loads alembic.ini, which installs its own root
    # handler and WARNING level; configure the application's logging last so
    # the JSON configuration is the one that survives startup.
    configure_logging()

    application = FastAPI(
        title="Runway API",
        version=__version__,
        description=(
            "Local-only YouTube Community-post intelligence and planning API "
            f"for the configured {active_settings.channel_name} channel"
        ),
        lifespan=_lifespan(database, active_settings),
    )
    application.state.settings = active_settings
    application.state.database = database
    web_origins = loopback_origins(active_settings.web_port)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=web_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )
    # Added last so it runs first, before CORS handling and every route.
    application.add_middleware(LocalRequestGuard, allowed_origins=web_origins)
    application.mount(
        "/media",
        StaticFiles(directory=active_settings.resolved_data_dir / "media"),
        name="media",
    )
    application.include_router(build_catalog_router(database, active_settings))
    application.include_router(build_discovery_router(database, active_settings))
    application.include_router(build_intelligence_router(database, active_settings))
    application.include_router(build_proposal_router(database, active_settings))
    application.include_router(build_platform_router(database, active_settings))

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
        }

    @application.get("/api/settings", tags=["settings"])
    def settings_view() -> dict[str, object]:
        return active_settings.public_dict()

    return application


app = create_app()
