from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import numpy as np
from sqlalchemy import func, select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    IntelligenceActivation,
    MediaAsset,
    Post,
    PostMedia,
    RepresentationRecord,
    RepresentationSet,
    RepresentationSetItem,
    utcnow,
)
from runway.db.repositories import get_channel
from runway.intelligence.embeddings import (
    ImageEmbeddingProvider,
    MultimodalEmbeddingProvider,
    RepresentationProviderRegistry,
    RepresentationResult,
    RepresentationStore,
    TextEmbeddingProvider,
    configuration_hash,
    content_hash,
)

RepresentationModality = Literal["text", "image", "multimodal"]

SCOPE_BY_MODALITY: dict[RepresentationModality, str] = {
    "text": "historical_text",
    "image": "historical_image",
    "multimodal": "historical_multimodal",
}
PURPOSE_BY_MODALITY: dict[RepresentationModality, str] = {
    "text": "historical_caption_semantics",
    "image": "historical_visual_semantics",
    "multimodal": "historical_pair_semantics",
}
FIELD_BY_MODALITY: dict[RepresentationModality, str] = {
    "text": "caption",
    "image": "image",
    "multimodal": "image_caption",
}
REQUIRED_CHALLENGER_ACTIVATION_GATES = frozenset(
    {
        "schema_contract",
        "coverage",
        "channel_isolation",
        "quality_non_regression",
        "latency_budget",
        "provider_provenance",
        "offline_only",
    }
)


@dataclass(frozen=True)
class PlannedRepresentation:
    entity_type: str
    entity_id: int
    field: str
    source_content_hash: str
    source_locator: dict[str, object]

    def identity(self) -> dict[str, object]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "field": self.field,
            "source_content_hash": self.source_content_hash,
        }


class RepresentationSetService:
    """Plan, checkpoint, validate, activate, supersede, and roll back exact sets."""

    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        registry: RepresentationProviderRegistry | None = None,
    ):
        self.database = database
        self.settings = settings or database.settings
        self.registry = registry or RepresentationProviderRegistry()
        self.store = RepresentationStore(database)

    def plan_history(
        self,
        modality: RepresentationModality,
        *,
        provider_name: str = "runway-local",
    ) -> dict[str, object]:
        provider = self._provider(modality, provider_name)
        planned = self._eligible_history(modality)
        scope = SCOPE_BY_MODALITY[modality]
        purpose = PURPOSE_BY_MODALITY[modality]
        config_hash = provider.configuration_fingerprint
        plan_payload = {
            "scope": scope,
            "purpose": purpose,
            "modality": modality,
            "provider": provider.name,
            "model": provider.model,
            "model_version": provider.version,
            "configuration_hash": config_hash,
            "items": [item.identity() for item in planned],
        }
        plan_hash = configuration_hash(plan_payload)
        configuration = {
            "planner": "historical-representation-plan-v1",
            "provider": provider.name,
            "model": provider.model,
            "model_version": provider.version,
            "configuration_hash": config_hash,
        }
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            existing = session.scalar(
                select(RepresentationSet)
                .where(
                    RepresentationSet.channel_id == channel.id,
                    RepresentationSet.scope == scope,
                    RepresentationSet.purpose == purpose,
                    RepresentationSet.plan_hash == plan_hash,
                )
                .limit(1)
            )
            if existing is not None:
                set_id = existing.id
                created = False
            else:
                representation_set = RepresentationSet(
                    channel_id=channel.id,
                    scope=scope,
                    purpose=purpose,
                    modality=modality,
                    provider=provider.name,
                    model=provider.model,
                    model_version=provider.version,
                    configuration_json=json.dumps(configuration, sort_keys=True),
                    configuration_hash=config_hash,
                    plan_hash=plan_hash,
                    status="planned",
                    expected_count=len(planned),
                    completed_count=0,
                    failed_count=0,
                    stale_count=0,
                    active=False,
                )
                session.add(representation_set)
                session.flush()
                session.add_all(
                    [
                        RepresentationSetItem(
                            representation_set_id=representation_set.id,
                            channel_id=channel.id,
                            entity_type=item.entity_type,
                            entity_id=item.entity_id,
                            field=item.field,
                            source_content_hash=item.source_content_hash,
                            source_locator_json=json.dumps(
                                item.source_locator,
                                sort_keys=True,
                            ),
                            status="pending",
                            attempts=0,
                        )
                        for item in planned
                    ]
                )
                set_id = representation_set.id
                created = True
        return self.status(set_id) | {"created": created}

    def backfill(
        self,
        representation_set_id: int,
        *,
        batch_size: int = 100,
    ) -> dict[str, object]:
        if batch_size < 1 or batch_size > 10_000:
            raise ValueError("batch_size must be between 1 and 10000")
        with self.database.session() as session:
            representation_set = session.get(RepresentationSet, representation_set_id)
            if representation_set is None:
                raise LookupError(f"representation set {representation_set_id} was not found")
            if representation_set.active:
                raise ValueError("an active representation set is immutable")
            if representation_set.status == "superseded":
                raise ValueError("a superseded representation set cannot be backfilled")
            representation_set.status = "backfilling"
            item_ids = list(
                session.scalars(
                    select(RepresentationSetItem.id)
                    .where(
                        RepresentationSetItem.representation_set_id == representation_set_id,
                        RepresentationSetItem.status.in_(("pending", "failed")),
                    )
                    .order_by(RepresentationSetItem.id)
                    .limit(batch_size)
                ).all()
            )

        completed = 0
        failed = 0
        for item_id in item_ids:
            try:
                self._backfill_item(item_id)
                completed += 1
            except Exception as exc:
                failed += 1
                with self.database.session() as session:
                    item = session.get(RepresentationSetItem, item_id)
                    if item is not None:
                        item.status = "failed"
                        item.error_summary = f"{type(exc).__name__}: {exc}"[:2000]
        self._refresh_counts(representation_set_id)
        result = self.status(representation_set_id)
        return result | {
            "batch_attempted": len(item_ids),
            "batch_completed": completed,
            "batch_failed": failed,
        }

    def validate(self, representation_set_id: int) -> dict[str, object]:
        errors: list[str] = []
        with self.database.session() as session:
            representation_set = session.get(RepresentationSet, representation_set_id)
            if representation_set is None:
                raise LookupError(f"representation set {representation_set_id} was not found")
            items = session.scalars(
                select(RepresentationSetItem)
                .where(RepresentationSetItem.representation_set_id == representation_set_id)
                .order_by(RepresentationSetItem.id)
            ).all()
            if len(items) != representation_set.expected_count:
                errors.append("planned item count does not match the representation-set contract")
            identities = {(item.entity_type, item.entity_id, item.field) for item in items}
            if len(identities) != len(items):
                errors.append("representation-set plan contains duplicate identities")
            vector_contracts: set[tuple[int, int, str, bool]] = set()
            for item in items:
                if item.status != "complete" or item.representation_record_id is None:
                    errors.append(f"item {item.id} is {item.status}")
                    continue
                record = session.get(
                    RepresentationRecord,
                    item.representation_record_id,
                )
                if record is None:
                    errors.append(f"item {item.id} references a missing record")
                    continue
                if (
                    record.channel_id != representation_set.channel_id
                    or record.entity_type != item.entity_type
                    or record.entity_id != item.entity_id
                    or record.field != item.field
                    or record.purpose != representation_set.purpose
                    or record.provider != representation_set.provider
                    or record.model != representation_set.model
                    or record.model_version != representation_set.model_version
                    or record.configuration_hash != representation_set.configuration_hash
                    or record.source_content_hash != item.source_content_hash
                ):
                    errors.append(f"item {item.id} has a mismatched representation identity")
                    continue
                try:
                    vectors = RepresentationStore.vectors(record)
                    if not np.all(np.isfinite(vectors)):
                        raise ValueError("representation contains nonfinite values")
                    if record.vector_count != vectors.shape[0]:
                        raise ValueError("representation vector count is inconsistent")
                    if record.normalized:
                        norms = np.linalg.norm(vectors, axis=1)
                        if np.any(np.abs(norms - 1.0) > 0.01):
                            raise ValueError("normalized representation has an invalid norm")
                    vector_contracts.add(
                        (
                            record.dimensions,
                            record.vector_count,
                            record.dtype,
                            record.normalized,
                        )
                    )
                except ValueError as exc:
                    errors.append(str(exc))
            if len(vector_contracts) > 1:
                errors.append("representation set contains inconsistent vector contracts")
            valid = not errors
            representation_set.completed_count = sum(item.status == "complete" for item in items)
            representation_set.failed_count = sum(item.status == "failed" for item in items)
            representation_set.stale_count = sum(item.status == "stale" for item in items)
            if valid:
                representation_set.status = "ready"
                representation_set.validated_at = utcnow()
                representation_set.error_summary = None
            else:
                representation_set.status = (
                    "backfilling" if any(item.status == "pending" for item in items) else "failed"
                )
                representation_set.error_summary = "; ".join(errors[:20])
        return self.status(representation_set_id) | {
            "valid": valid,
            "errors": errors,
        }

    def activate(
        self,
        representation_set_id: int,
        *,
        reason: str,
        gate_results: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if not reason.strip():
            raise ValueError("activation reason is required")
        with self.database.session() as session:
            target = session.get(RepresentationSet, representation_set_id)
            if target is None:
                raise LookupError(f"representation set {representation_set_id} was not found")
            complete = int(
                session.scalar(
                    select(func.count(RepresentationSetItem.id)).where(
                        RepresentationSetItem.representation_set_id == target.id,
                        RepresentationSetItem.status == "complete",
                        RepresentationSetItem.representation_record_id.is_not(None),
                    )
                )
                or 0
            )
            if (
                target.status != "ready"
                or complete != target.expected_count
                or target.failed_count
                or target.stale_count
            ):
                raise ValueError("only a complete, validated representation set can be activated")
            self._require_activation_gates(
                provider=target.provider,
                gate_results=gate_results,
            )
            active_rows = session.scalars(
                select(RepresentationSet)
                .where(
                    RepresentationSet.channel_id == target.channel_id,
                    RepresentationSet.scope == target.scope,
                    RepresentationSet.purpose == target.purpose,
                    RepresentationSet.active.is_(True),
                    RepresentationSet.id != target.id,
                )
                .order_by(RepresentationSet.activated_at.desc(), RepresentationSet.id.desc())
            ).all()
            if len(active_rows) > 1:
                raise RuntimeError("multiple active representation sets already exist")
            previous = active_rows[0] if active_rows else None
            if previous is not None:
                previous.active = False
                previous.status = "superseded"
                previous.superseded_at = utcnow()
                target.supersedes_set_id = previous.id
            target.active = True
            target.status = "active"
            target.activated_at = utcnow()
            target.superseded_at = None
            session.add(
                IntelligenceActivation(
                    channel_id=target.channel_id,
                    resource_type="representation_set",
                    target=f"{target.scope}:{target.purpose}",
                    action="activate",
                    resource_id=str(target.id),
                    previous_resource_id=(str(previous.id) if previous is not None else None),
                    reason=reason.strip(),
                    gate_results_json=json.dumps(
                        gate_results or {},
                        sort_keys=True,
                    ),
                )
            )
        return self.status(representation_set_id)

    @staticmethod
    def _require_activation_gates(
        *,
        provider: str,
        gate_results: dict[str, object] | None,
    ) -> None:
        if provider == "runway-local":
            return
        results = gate_results or {}
        failed = sorted(
            gate for gate in REQUIRED_CHALLENGER_ACTIVATION_GATES if results.get(gate) is not True
        )
        if failed:
            raise ValueError(
                "challenger representation activation requires passing evaluation "
                f"gates: {', '.join(failed)}"
            )

    def rollback(
        self,
        representation_set_id: int,
        *,
        to_set_id: int | None = None,
        reason: str,
    ) -> dict[str, object]:
        if not reason.strip():
            raise ValueError("rollback reason is required")
        with self.database.session() as session:
            current = session.get(RepresentationSet, representation_set_id)
            if current is None or not current.active or current.status != "active":
                raise ValueError(f"representation set {representation_set_id} is not active")
            target_id = to_set_id or current.supersedes_set_id
            if target_id is None:
                raise ValueError("there is no prior representation set to restore")
            target = session.get(RepresentationSet, target_id)
            if (
                target is None
                or target.channel_id != current.channel_id
                or target.scope != current.scope
                or target.purpose != current.purpose
            ):
                raise ValueError("rollback target is outside the active set scope")
            complete = int(
                session.scalar(
                    select(func.count(RepresentationSetItem.id)).where(
                        RepresentationSetItem.representation_set_id == target.id,
                        RepresentationSetItem.status == "complete",
                        RepresentationSetItem.representation_record_id.is_not(None),
                    )
                )
                or 0
            )
            if complete != target.expected_count or target.failed_count or target.stale_count:
                raise ValueError("rollback target is not complete and valid")
            current.active = False
            current.status = "rolled_back"
            current.superseded_at = utcnow()
            target.active = True
            target.status = "active"
            target.activated_at = utcnow()
            target.superseded_at = None
            session.add(
                IntelligenceActivation(
                    channel_id=current.channel_id,
                    resource_type="representation_set",
                    target=f"{current.scope}:{current.purpose}",
                    action="rollback",
                    resource_id=str(target.id),
                    previous_resource_id=str(current.id),
                    reason=reason.strip(),
                    gate_results_json=json.dumps(
                        {"complete": complete, "expected": target.expected_count},
                        sort_keys=True,
                    ),
                )
            )
        return self.status(target_id)

    def status(self, representation_set_id: int) -> dict[str, object]:
        coverage = self.store.coverage(representation_set_id)
        with self.database.session() as session:
            row = session.get(RepresentationSet, representation_set_id)
            if row is None:
                raise LookupError(f"representation set {representation_set_id} was not found")
            return coverage | {
                "scope": row.scope,
                "purpose": row.purpose,
                "modality": row.modality,
                "provider": row.provider,
                "model": row.model,
                "model_version": row.model_version,
                "configuration_hash": row.configuration_hash,
                "plan_hash": row.plan_hash,
                "validated_at": (row.validated_at.isoformat() if row.validated_at else None),
                "activated_at": (row.activated_at.isoformat() if row.activated_at else None),
                "supersedes_set_id": row.supersedes_set_id,
                "error_summary": row.error_summary,
            }

    def list_sets(self) -> list[dict[str, object]]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            ids = list(
                session.scalars(
                    select(RepresentationSet.id)
                    .where(RepresentationSet.channel_id == channel.id)
                    .order_by(RepresentationSet.id.desc())
                ).all()
            )
        return [self.status(set_id) for set_id in ids]

    def _backfill_item(self, item_id: int) -> None:
        with self.database.session() as session:
            item = session.get(RepresentationSetItem, item_id)
            if item is None:
                raise LookupError(f"representation item {item_id} was not found")
            representation_set = session.get(
                RepresentationSet,
                item.representation_set_id,
            )
            if representation_set is None:
                raise LookupError("representation set was deleted")
            locator: object = json.loads(item.source_locator_json)
            if not isinstance(locator, dict):
                raise ValueError("representation item locator is malformed")
            item.attempts += 1
            item.status = "running"
            channel_id = item.channel_id
            entity_type = item.entity_type
            entity_id = item.entity_id
            field = item.field
            expected_hash = item.source_content_hash
            modality = cast(RepresentationModality, representation_set.modality)
            purpose = representation_set.purpose
            provider_name = representation_set.provider
            expected_model = representation_set.model
            expected_model_version = representation_set.model_version
            expected_configuration_hash = representation_set.configuration_hash

        observed_hash = self._source_hash(modality, locator)
        if observed_hash != expected_hash:
            raise ValueError("planned source content changed before backfill")
        provider = self._provider(modality, provider_name)
        record = self.store.get_or_create(
            channel_id=channel_id,
            entity_type=entity_type,
            entity_id=entity_id,
            field=field,
            modality=modality,
            purpose=purpose,
            provider=provider.name,
            model=expected_model,
            model_version=expected_model_version,
            source_content_hash=expected_hash,
            configuration_hash=expected_configuration_hash,
            producer=lambda: self._embed(modality, provider, locator, purpose),
            metadata={
                "representation_set_id": representation_set.id,
                "source_locator": locator,
            },
        )
        with self.database.session() as session:
            item = session.get(RepresentationSetItem, item_id)
            if item is None:
                raise LookupError(f"representation item {item_id} disappeared")
            item.representation_record_id = record.id
            item.status = "complete"
            item.error_summary = None
            item.completed_at = utcnow()

    def _refresh_counts(self, representation_set_id: int) -> None:
        with self.database.session() as session:
            representation_set = session.get(RepresentationSet, representation_set_id)
            if representation_set is None:
                raise LookupError(f"representation set {representation_set_id} was not found")
            rows = session.execute(
                select(
                    RepresentationSetItem.status,
                    func.count(RepresentationSetItem.id),
                )
                .where(RepresentationSetItem.representation_set_id == representation_set_id)
                .group_by(RepresentationSetItem.status)
            ).all()
            counts = {str(status): int(value) for status, value in rows}
            representation_set.completed_count = counts.get("complete", 0)
            representation_set.failed_count = counts.get("failed", 0)
            representation_set.stale_count = counts.get("stale", 0)
            if (
                representation_set.completed_count == representation_set.expected_count
                and not representation_set.failed_count
                and not representation_set.stale_count
            ):
                representation_set.status = "backfilled"
            elif counts.get("pending", 0) or counts.get("running", 0):
                representation_set.status = "backfilling"
            else:
                representation_set.status = "failed"

    def _eligible_history(
        self,
        modality: RepresentationModality,
    ) -> list[PlannedRepresentation]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            if modality == "text":
                posts = session.scalars(
                    select(Post)
                    .where(
                        Post.channel_id == channel.id,
                        Post.is_training_eligible.is_(True),
                    )
                    .order_by(Post.id)
                ).all()
                return [
                    PlannedRepresentation(
                        entity_type="post",
                        entity_id=post.id,
                        field=FIELD_BY_MODALITY[modality],
                        source_content_hash=content_hash(post.caption or ""),
                        source_locator={"text": post.caption or ""},
                    )
                    for post in posts
                ]

            rows = session.execute(
                select(Post, MediaAsset, PostMedia.position)
                .join(PostMedia, PostMedia.post_id == Post.id)
                .join(MediaAsset, MediaAsset.id == PostMedia.media_asset_id)
                .where(
                    Post.channel_id == channel.id,
                    Post.is_training_eligible.is_(True),
                )
                .order_by(Post.id, PostMedia.position, MediaAsset.id)
            ).all()
            if modality == "image":
                by_media: dict[int, PlannedRepresentation] = {}
                for _post, media, _position in rows:
                    by_media.setdefault(
                        media.id,
                        PlannedRepresentation(
                            entity_type="media_asset",
                            entity_id=media.id,
                            field=FIELD_BY_MODALITY[modality],
                            source_content_hash=media.sha256,
                            source_locator={
                                "local_path": media.local_path,
                                "sha256": media.sha256,
                            },
                        ),
                    )
                return list(by_media.values())

            first_media: dict[int, tuple[Post, MediaAsset]] = {}
            for post, media, _position in rows:
                first_media.setdefault(post.id, (post, media))
            return [
                PlannedRepresentation(
                    entity_type="post",
                    entity_id=post.id,
                    field=FIELD_BY_MODALITY[modality],
                    source_content_hash=configuration_hash(
                        {"media_sha256": media.sha256, "text": post.caption or ""}
                    ),
                    source_locator={
                        "local_path": media.local_path,
                        "sha256": media.sha256,
                        "text": post.caption or "",
                    },
                )
                for post, media in first_media.values()
            ]

    def _provider(
        self,
        modality: RepresentationModality,
        provider_name: str,
    ) -> TextEmbeddingProvider | ImageEmbeddingProvider | MultimodalEmbeddingProvider:
        if modality == "text":
            return self.registry.text(provider_name)
        if modality == "image":
            return self.registry.image(provider_name)
        return self.registry.multimodal(provider_name)

    def _embed(
        self,
        modality: RepresentationModality,
        provider: TextEmbeddingProvider | ImageEmbeddingProvider | MultimodalEmbeddingProvider,
        locator: dict[str, object],
        purpose: str,
    ) -> RepresentationResult:
        if modality == "text":
            return cast(TextEmbeddingProvider, provider).embed_text(
                str(locator.get("text") or ""),
                purpose=purpose,
            )
        path = self._resolved_path(locator)
        if modality == "image":
            return cast(ImageEmbeddingProvider, provider).embed_image(
                path,
                purpose=purpose,
            )
        return cast(MultimodalEmbeddingProvider, provider).embed_image_text(
            path,
            str(locator.get("text") or ""),
            purpose=purpose,
        )

    def _source_hash(
        self,
        modality: RepresentationModality,
        locator: dict[str, object],
    ) -> str:
        if modality == "text":
            return content_hash(str(locator.get("text") or ""))
        path = self._resolved_path(locator)
        observed_media_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if observed_media_hash != str(locator.get("sha256") or ""):
            return observed_media_hash
        if modality == "image":
            return observed_media_hash
        return configuration_hash(
            {
                "media_sha256": observed_media_hash,
                "text": str(locator.get("text") or ""),
            }
        )

    def _resolved_path(self, locator: dict[str, object]) -> Path:
        raw = locator.get("local_path")
        if not isinstance(raw, str) or not raw:
            raise ValueError("representation source has no local path")
        path = Path(raw)
        resolved = path if path.is_absolute() else self.settings.resolved_data_dir / path
        resolved = resolved.resolve()
        data_root = self.settings.resolved_data_dir.resolve()
        if data_root != resolved and data_root not in resolved.parents:
            raise ValueError("representation source path escapes the configured data root")
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return resolved
