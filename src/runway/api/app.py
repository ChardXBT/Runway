from __future__ import annotations

import logging
import subprocess
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from runway import __version__
from runway.api.catalog_routes import build_catalog_router
from runway.api.discovery_routes import build_discovery_router
from runway.api.intelligence_routes import build_intelligence_router
from runway.api.platform_routes import build_platform_router
from runway.api.proposal_routes import build_proposal_router
from runway.api.security import LocalRequestGuard, loopback_origins
from runway.auth.principal import get_principal
from runway.auth.problems import Problem, problem_handler
from runway.config import Settings, get_settings
from runway.db.base import Database
from runway.db.bootstrap import DatabaseNotCurrent, require_database_at_head
from runway.logging import configure_logging
from runway.platforms.sync import SyncService

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


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    # No migrations and no database connection here: importing the module or
    # building the app must never alter whatever database .env points at.
    database = Database(active_settings)
    configure_logging()
    hosted = active_settings.profile == "hosted"

    application = FastAPI(
        title="Runway API",
        version=__version__,
        description=(
            "Local-only YouTube Community-post intelligence and planning API "
            f"for the configured {active_settings.channel_name} channel"
        ),
        lifespan=_lifespan(database, active_settings),
        # The generated docs carry no principal; hosted mode does not serve them.
        docs_url=None if hosted else "/docs",
        redoc_url=None if hosted else "/redoc",
        openapi_url=None if hosted else "/openapi.json",
    )
    application.state.settings = active_settings
    application.add_exception_handler(Problem, problem_handler)
    application.state.database = database
    web_origins = loopback_origins(active_settings.web_port)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=web_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
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

    @application.get("/api/settings", tags=["settings"], dependencies=[Depends(get_principal)])
    def settings_view() -> dict[str, object]:
        return active_settings.public_dict()

    return application


app = create_app()
