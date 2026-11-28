"""`/api/v1/media`: resumable uploads and probed assets for posts (spec 02 section 4).

Uploaded bytes never leave through a static mount or the API process: in
`local` the poster preview is a guarded file route, and in `hosted` the same
route answers with a redirect to a short-lived signed URL on the object store.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from runway.api.v1.idempotency import idempotent
from runway.api.v1.router import build_v1_router
from runway.auth.principal import Principal, get_principal
from runway.auth.problems import Problem, ProblemCode
from runway.posts.media_service import (
    MediaComplete,
    MediaCreate,
    MediaOut,
    MediaService,
    UploadStatusOut,
)

# One part per request; parts are at most 64 MiB (settings.media_api_chunk_bytes).
MAX_PART_BYTES = 64 * 1024 * 1024 + 1


def build_media_router(service: MediaService) -> APIRouter:
    router = build_v1_router(tags=["media"], prefix="/media")

    @router.post("", status_code=status.HTTP_201_CREATED)
    @idempotent
    def create_media(
        payload: MediaCreate, principal: Principal = Depends(get_principal)
    ) -> MediaOut:
        """Start an upload (`media:write`): returns the asset and its upload plan."""
        return service.create(principal, payload)

    @router.put("/{asset_id}/parts/{part_number}")
    async def put_part(
        asset_id: int,
        part_number: int,
        request: Request,
        principal: Principal = Depends(get_principal),
    ) -> UploadStatusOut:
        """Send one part's bytes (`api` mode). Re-sending a part replaces it."""
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_PART_BYTES:
            raise Problem.of(ProblemCode.VALIDATION_FAILED, "a part may be at most 64 MiB")
        body = await request.body()
        if len(body) > MAX_PART_BYTES:
            raise Problem.of(ProblemCode.VALIDATION_FAILED, "a part may be at most 64 MiB")
        return await run_in_threadpool(service.receive_part, principal, asset_id, part_number, body)

    @router.get("/{asset_id}/upload")
    def upload_status(
        asset_id: int, principal: Principal = Depends(get_principal)
    ) -> UploadStatusOut:
        """What has arrived, so an interrupted client resumes without re-sending parts."""
        return service.upload_status(principal, asset_id)

    @router.post("/{asset_id}/complete")
    @idempotent
    def complete_media(
        asset_id: int, payload: MediaComplete, principal: Principal = Depends(get_principal)
    ) -> MediaOut:
        """Verify size and SHA-256, then check the file (`processing` until the probe ends)."""
        return service.complete(principal, asset_id, payload)

    @router.get("/{asset_id}")
    def get_media(asset_id: int, principal: Principal = Depends(get_principal)) -> MediaOut:
        """The asset, its probe and which post types it is valid for."""
        return service.get(principal, asset_id)

    @router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_media(asset_id: int, principal: Principal = Depends(get_principal)) -> Response:
        """Delete an asset that no post uses."""
        service.delete(principal, asset_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/{asset_id}/poster",
        response_class=FileResponse,
        response_model=None,
        responses={200: {"content": {"image/jpeg": {}}}},
    )
    def get_poster(
        asset_id: int, principal: Principal = Depends(get_principal)
    ) -> FileResponse | RedirectResponse:
        """The preview frame Runway extracted (never the uploaded file).

        Local serves the poster file; hosted answers 307 to a short-lived
        signed URL, so no preview bytes are served from the API origin.
        """
        if service.settings.profile != "local":
            return RedirectResponse(service.poster_url(principal, asset_id))
        path = service.poster_path(principal, asset_id)
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
        )

    return router
