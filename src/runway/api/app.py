from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from runway import __version__
from runway.api.catalog_routes import build_catalog_router
from runway.api.discovery_routes import build_discovery_router
from runway.api.intelligence_routes import build_intelligence_router
from runway.api.proposal_routes import build_proposal_router
from runway.config import Settings, get_settings
from runway.db import initialize_database
from runway.logging import configure_logging
from runway.services.dashboard import DashboardService


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    configure_logging()
    database = initialize_database(active_settings)

    application = FastAPI(
        title="Runway API",
        version=__version__,
        description=(
            "Local-only YouTube Community-post intelligence and planning API "
            f"for the configured {active_settings.channel_name} channel"
        ),
    )
    application.state.settings = active_settings
    application.state.database = database
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://127.0.0.1:{active_settings.web_port}",
            f"http://localhost:{active_settings.web_port}",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )
    application.mount(
        "/media",
        StaticFiles(directory=active_settings.resolved_data_dir / "media"),
        name="media",
    )
    application.include_router(build_catalog_router(database, active_settings))
    application.include_router(build_discovery_router(database, active_settings))
    application.include_router(build_intelligence_router(database, active_settings))
    application.include_router(build_proposal_router(database, active_settings))

    @application.get("/health", tags=["system"])
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "product": active_settings.product_name,
            "version": __version__,
            "publishing_enabled": active_settings.publishing_enabled,
            "publishing_trigger": "human_accept",
            "one_bot_post_per_day": True,
            "scheduling_horizon_days": None,
        }

    @application.get("/api/settings", tags=["settings"])
    def settings_view() -> dict[str, object]:
        return active_settings.public_dict()

    @application.get("/api/dashboard", tags=["dashboard"])
    def dashboard() -> dict[str, object]:
        return DashboardService(database).summary()

    return application


app = create_app()
