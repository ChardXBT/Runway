from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from leeway import __version__
from leeway.api.catalog_routes import build_catalog_router
from leeway.api.discovery_routes import build_discovery_router
from leeway.api.intelligence_routes import build_intelligence_router
from leeway.api.proposal_routes import build_proposal_router
from leeway.config import Settings, get_settings
from leeway.db import initialize_database
from leeway.logging import configure_logging
from leeway.services.dashboard import DashboardService


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    configure_logging()
    database = initialize_database(active_settings)

    application = FastAPI(
        title="Leeway API",
        version=__version__,
        description="Local-only Qlob Community-post intelligence and planning API",
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
            "publishing_enabled": False,
        }

    @application.get("/api/settings", tags=["settings"])
    def settings_view() -> dict[str, object]:
        return active_settings.public_dict()

    @application.get("/api/dashboard", tags=["dashboard"])
    def dashboard() -> dict[str, object]:
        return DashboardService(database).summary()

    return application


app = create_app()
