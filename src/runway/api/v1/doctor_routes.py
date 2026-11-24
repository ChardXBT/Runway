"""``GET /api/v1/doctor/platforms``: the connect-readiness report for the UI.

This is the API twin of ``runway doctor platforms`` (spec 08 expansion): the
same ``PlatformsDoctor`` report, returned as JSON so the settings page can
show "what is missing" next to each Connect button. Read-only: it never
writes and it never calls the network - the ``--check-edge`` probe stays a
CLI option. Owner only, like every account-mutating route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from runway.api.v1.router import build_v1_router
from runway.auth.principal import WORKSPACE_ADMIN, Principal, get_principal, require
from runway.config import Settings
from runway.db.base import Database
from runway.platforms.doctor import PlatformsDoctor


def build_doctor_router(
    database: Database,
    settings: Settings,
    *,
    doctor: PlatformsDoctor | None = None,
) -> APIRouter:
    router = build_v1_router(tags=["doctor"], prefix="/doctor")
    active = doctor or PlatformsDoctor(database, settings)

    @router.get("/platforms")
    def doctor_platforms(
        principal: Principal = Depends(get_principal),
    ) -> dict[str, object]:
        """Per-platform connect readiness (owner only, read-only, no network)."""
        require(principal, WORKSPACE_ADMIN)
        return active.run().as_dict()

    return router


__all__ = ["build_doctor_router"]
