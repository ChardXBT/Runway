"""Outbound webhook endpoints (spec 06 section 8). Everything here needs `workspace:admin`.

Managing outbound destinations and their signing secrets is a person's job:
every route also requires a human principal (invariant 16).

- `GET /api/v1/webhooks`, `POST /api/v1/webhooks` (the signing secret is shown once),
- `GET`, `PATCH`, `DELETE /api/v1/webhooks/{endpoint_id}`,
- `GET /api/v1/webhooks/{endpoint_id}/deliveries`.

`POST /api/v1/webhooks` is not idempotent for the same reason as key creation:
a replay would have to store the secret in clear.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from runway.agent.webhooks import (
    CreatedWebhook,
    DeliveryView,
    WebhookCreate,
    WebhookPatch,
    WebhookService,
    WebhookView,
)
from runway.api.v1.idempotency import idempotent
from runway.api.v1.pagination import CursorPage, CursorParams, cursor_params
from runway.api.v1.router import build_v1_router
from runway.auth.principal import (
    WORKSPACE_ADMIN,
    Principal,
    get_principal,
    require,
    require_person,
)


def build_webhooks_router(service: WebhookService) -> APIRouter:
    router = build_v1_router(tags=["webhooks"], prefix="/webhooks")

    @router.get("")
    def list_webhooks(
        params: CursorParams = Depends(cursor_params),
        principal: Principal = Depends(get_principal),
    ) -> CursorPage[WebhookView]:
        """The workspace's webhook endpoints, newest first."""
        require(principal, WORKSPACE_ADMIN)
        require_person(principal, "list webhook endpoints")
        return service.list_endpoints(principal, params)

    @router.post("", status_code=status.HTTP_201_CREATED)
    def create_webhook(
        payload: WebhookCreate, principal: Principal = Depends(get_principal)
    ) -> CreatedWebhook:
        """Register an endpoint; the response carries its signing secret once."""
        require(principal, WORKSPACE_ADMIN)
        require_person(principal, "create webhook endpoints")
        return service.create(principal, payload)

    @router.get("/{endpoint_id}")
    def get_webhook(endpoint_id: int, principal: Principal = Depends(get_principal)) -> WebhookView:
        require(principal, WORKSPACE_ADMIN)
        require_person(principal, "inspect webhook endpoints")
        return service.get(principal, endpoint_id)

    @router.patch("/{endpoint_id}")
    @idempotent
    def update_webhook(
        endpoint_id: int, payload: WebhookPatch, principal: Principal = Depends(get_principal)
    ) -> WebhookView:
        """Change the event filter or description, or disable / re-enable the endpoint."""
        require(principal, WORKSPACE_ADMIN)
        require_person(principal, "update webhook endpoints")
        return service.update(principal, endpoint_id, payload)

    @router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_webhook(endpoint_id: int, principal: Principal = Depends(get_principal)) -> Response:
        require(principal, WORKSPACE_ADMIN)
        require_person(principal, "delete webhook endpoints")
        service.delete(principal, endpoint_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/{endpoint_id}/deliveries")
    def list_deliveries(
        endpoint_id: int,
        params: CursorParams = Depends(cursor_params),
        principal: Principal = Depends(get_principal),
    ) -> CursorPage[DeliveryView]:
        """Delivery attempts for one endpoint, newest first (status codes, never bodies)."""
        require(principal, WORKSPACE_ADMIN)
        require_person(principal, "inspect webhook deliveries")
        return service.deliveries(principal, endpoint_id, params)

    return router
