"""Builds the `PublishContext` an adapter receives from a target snapshot.

The engine hooks (`mark_request_sent`, `save_resumable`, progress) are wired
by the worker (`runway.posts.engine`); a release-time preflight gets a context
whose hooks refuse, because nothing may be sent outside the worker.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, BinaryIO

from runway.db.models import ConnectedAccount, MediaAssetV2
from runway.platforms.base import TokenSet
from runway.platforms.publisher import AccountRef, MediaRef, PublishContext
from runway.posts.snapshot import SnapshotSources, TargetSnapshot, granted_scopes, utc


def account_ref(account: ConnectedAccount | None) -> AccountRef | None:
    if account is None:
        return None
    from runway.platforms.capabilities import Platform

    return AccountRef(
        id=account.id,
        platform=Platform(account.platform),
        external_account_id=account.external_account_id,
        handle=account.handle,
        status=account.status,
        granted_scopes=granted_scopes(account),
        token_ref=account.token_ref,
        account_type=account.account_type,
    )


def probe_of(asset: MediaAssetV2) -> dict[str, Any]:
    try:
        value = json.loads(asset.probe_json or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def media_refs(assets: list[MediaAssetV2]) -> tuple[MediaRef, ...]:
    return tuple(
        MediaRef(
            asset_id=asset.id,
            position=position,
            kind=asset.kind,
            sha256=asset.sha256,
            size_bytes=int(asset.size_bytes),
            content_type=asset.content_type,
            storage=asset.storage,
            storage_key=asset.storage_key,
            original_filename=asset.original_filename,
            probe=probe_of(asset),
        )
        for position, asset in enumerate(assets)
    )


def _refuse(name: str) -> Callable[..., Any]:
    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"{name} is only available to the worker while sending")

    return refused


def build_context(
    sources: SnapshotSources,
    snap: TargetSnapshot,
    *,
    publisher: str,
    payload_hash: str | None = None,
    resumable: Mapping[str, Any] | None = None,
    request_sent_at: Any = None,
    attempt_id: int | None = None,
    job_id: int | None = None,
) -> PublishContext:
    """A context for one target; engine hooks refuse until the worker replaces them."""
    target = snap.target
    account = account_ref(snap.account)
    workspace_id = target.workspace_id

    def open_media(media: MediaRef) -> BinaryIO:
        store = sources.media_store_for(media.storage)
        if store is None:
            raise RuntimeError("no media store is configured for this media")
        return store.open(media.storage_key, workspace_id=workspace_id)

    def signed_media_url(media: MediaRef, ttl_seconds: int) -> str | None:
        store = sources.media_store_for(media.storage)
        if store is None:
            return None
        return store.signed_get_url(media.storage_key, ttl_seconds, workspace_id=workspace_id)

    def access_token() -> TokenSet:
        if sources.token_provider is None or snap.account is None:
            raise RuntimeError("no token is available for this account")
        return sources.token_provider(snap.account)

    return PublishContext(
        workspace_id=workspace_id,
        post_id=snap.post.id,
        target_id=target.id,
        platform=snap.platform,
        post_type=snap.post_type.value if snap.post_type else str(target.post_type or ""),
        publisher=publisher,
        account=account,
        title=snap.post.title,
        caption=snap.caption,
        settings=dict(snap.effective_settings),
        media=media_refs(snap.media),
        planned_at=utc(target.planned_at),
        payload_hash=payload_hash or snap.payload_hash,
        ai_generated=bool(snap.post.ai_generated),
        external_id=target.external_id,
        permalink=target.published_url,
        attempt_id=attempt_id,
        job_id=job_id,
        resumable=dict(resumable or {}),
        request_sent_at=utc(request_sent_at),
        timezone=sources.timezone,
        clock=sources.clock,
        _mark_request_sent=_refuse("mark_request_sent"),
        _save_resumable=_refuse("save_resumable"),
        _open_media=open_media,
        _signed_media_url=signed_media_url,
        _access_token=access_token,
    )
