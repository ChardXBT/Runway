from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import numpy as np
from sqlalchemy import delete, select

from runway.analysis.features import text_embedding, text_similarity
from runway.analysis.runtime import AgentRuntime, AgentTerminalError, runtime_for
from runway.analysis.schemas import HistoricalAnnotation
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    AnnotationCorrection,
    MediaAsset,
    ModelRun,
    Post,
    PostAnnotation,
    PostMedia,
    SimilarityEdge,
    utcnow,
)
from runway.db.repositories import audit, get_channel
from runway.media.service import cosine_similarity, prepare_model_image

CORRECTION_OUTPUT_ALIASES = {
    "characters": "visible_characters",
    "visual_format": "visual_medium",
    "emotion": "facial_emotional_cues",
    "text_in_image": "text_overlay",
}


def effective_annotation_fields(annotation: PostAnnotation) -> dict[str, object]:
    """Return normalized annotation fields with human review overlays applied."""
    try:
        original = json.loads(annotation.original_output_json or "{}")
    except json.JSONDecodeError:
        original = {}
    if not isinstance(original, dict):
        original = {}
    characters = json.loads(annotation.characters_json)
    generic_entities = original.get("entities", [])
    if not isinstance(generic_entities, list):
        generic_entities = []
    confidence_payload = original.get("confidence", {})
    character_confidence = (
        confidence_payload.get("characters", 0.0)
        if isinstance(confidence_payload, dict)
        else 0.0
    )
    if not isinstance(character_confidence, (int, float)):
        character_confidence = 0.0
    if not generic_entities:
        generic_entities = [
            {
                "name": str(value),
                "entity_type": "character",
                "confidence": float(character_confidence),
            }
            for value in characters
        ]
    fields: dict[str, object] = {
        "franchise": annotation.franchise,
        "show_name": annotation.show_name,
        "characters": characters,
        "visible_character_count": annotation.visible_character_count,
        "scene_description": annotation.scene_description,
        "visual_format": annotation.visual_format,
        "composition": annotation.composition,
        "emotion": annotation.emotion,
        "reaction_potential": annotation.reaction_potential,
        "caption_intent": annotation.caption_intent,
        "caption_structure": annotation.caption_structure,
        "humor_style": annotation.humor_style,
        "tone": annotation.tone,
        "text_in_image": annotation.text_in_image,
        "entities": generic_entities,
        "people": original.get("people", []),
        "organizations": original.get("organizations", []),
        "products": original.get("products", []),
        "teams": original.get("teams", []),
        "locations": original.get("locations", []),
        "animals": original.get("animals", []),
        "objects": original.get("objects", []),
        "actions": original.get("actions", []),
        "relationships": original.get("relationships", []),
        "setting": original.get("setting", "unknown"),
        "ocr_text": original.get("ocr_text", []),
        "editorial_angle": original.get("editorial_angle", "unknown"),
        "audience_invitation_type": original.get("audience_invitation_type", "none"),
        "image_caption_relationship": original.get(
            "image_caption_relationship",
            "unknown",
        ),
        "field_confidence": original.get("confidence", {}),
    }
    corrections = json.loads(annotation.reviewed_fields_json or "{}")
    if not isinstance(corrections, dict):
        raise ValueError(f"annotation {annotation.id} has invalid reviewed fields")
    fields.update(corrections)
    return fields


class AnalysisService:
    annotation_version = "historical-annotation-v3"
    compatible_annotation_versions = ("historical-annotation-v3", "historical-annotation-v2")
    prompt_version = "annotate-history-batch-v3"

    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime: AgentRuntime | None = None,
    ):
        self.database = database
        self.settings = settings
        self.runtime = runtime or runtime_for(settings)

    async def analyze_history(
        self,
        *,
        resume: bool = True,
        max_posts: int | None = None,
    ) -> dict[str, int]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            posts = session.scalars(
                select(Post)
                .where(
                    Post.channel_id == channel.id,
                    Post.is_training_eligible.is_(True),
                )
                .order_by(Post.id)
            ).all()
            existing_ids = set(
                session.scalars(
                    select(PostAnnotation.post_id).where(
                        PostAnnotation.annotation_version == self.annotation_version
                    )
                ).all()
            )
            payloads: list[dict[str, object]] = []
            for post in posts:
                if resume and post.id in existing_ids:
                    continue
                media_assets = session.scalars(
                    select(MediaAsset)
                    .join(PostMedia, PostMedia.media_asset_id == MediaAsset.id)
                    .where(PostMedia.post_id == post.id)
                    .order_by(PostMedia.position)
                ).all()
                if not media_assets:
                    continue
                prepared_paths = [
                    prepare_model_image(
                        self.settings.resolved_data_dir / asset.local_path,
                        self.settings,
                    )
                    for asset in media_assets
                ]
                payloads.append(
                    {
                        "post_id": post.id,
                        "caption": post.caption or "",
                        "post_type": post.post_type,
                        "published_at": (
                            post.published_at.isoformat() if post.published_at else None
                        ),
                        "_image_paths": [str(path) for path in prepared_paths],
                    }
                )

        pending_before_limit = len(payloads)
        if max_posts is not None:
            payloads = payloads[:max_posts]
        completed = 0
        failed = 0
        skipped = len(posts) - pending_before_limit
        deferred = pending_before_limit - len(payloads)
        batches = 0
        batch_size = self.settings.analysis_batch_size
        for offset in range(0, len(payloads), batch_size):
            payload_batch = payloads[offset : offset + batch_size]
            post_ids = [cast(int, payload["post_id"]) for payload in payload_batch]
            batch_image_paths: list[str] = []
            bounded_posts: list[dict[str, object]] = []
            for payload in payload_batch:
                indices: list[int] = []
                for path in cast(list[str], payload["_image_paths"]):
                    indices.append(len(batch_image_paths))
                    batch_image_paths.append(path)
                bounded_posts.append(
                    {key: value for key, value in payload.items() if not key.startswith("_")}
                    | {
                        "image_indices": indices,
                        "image_count": len(indices),
                    }
                )
            request: dict[str, object] = {
                "posts": bounded_posts,
                "_image_paths": batch_image_paths,
            }
            started = utcnow()
            try:
                output = await self.runtime.annotate_historical_posts(request)
                received_ids = [item.post_id for item in output.annotations]
                if len(received_ids) != len(set(received_ids)) or set(received_ids) != set(
                    post_ids
                ):
                    raise AgentTerminalError(
                        "Codex batch output did not map one-to-one to the requested post IDs"
                    )
            except Exception as exc:
                failed += len(payload_batch)
                self._record_model_run(
                    task="annotate_historical_posts",
                    prompt_version=self.prompt_version,
                    input_ids=post_ids,
                    request=request,
                    output=None,
                    started=started,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if isinstance(exc, AgentTerminalError):
                    raise
                continue
            annotations = {item.post_id: item.annotation for item in output.annotations}
            with self.database.session() as session:
                for post_id in post_ids:
                    existing = session.scalar(
                        select(PostAnnotation).where(
                            PostAnnotation.post_id == post_id,
                            PostAnnotation.annotation_version == self.annotation_version,
                        )
                    )
                    if existing is None:
                        session.add(self._annotation_record(post_id, annotations[post_id]))
                    audit(
                        session,
                        "historical_post_annotated",
                        "post",
                        post_id,
                        {"annotation_version": self.annotation_version},
                    )
            self._record_model_run(
                task="annotate_historical_posts",
                prompt_version=self.prompt_version,
                input_ids=post_ids,
                request=request,
                output=output.model_dump(),
                started=started,
                error=None,
            )
            completed += len(post_ids)
            batches += 1
        edges = self.rebuild_similarity_edges()
        return {
            "completed": completed,
            "skipped": skipped,
            "failed": failed,
            "batches": batches,
            "deferred": deferred,
            "edges": edges,
        }

    def rebuild_similarity_edges(self) -> int:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            posts = session.scalars(
                select(Post)
                .where(
                    Post.channel_id == channel.id,
                    Post.is_training_eligible.is_(True),
                )
                .order_by(Post.id)
            ).all()
            annotation_map = {
                annotation.post_id: annotation
                for annotation in session.scalars(
                    select(PostAnnotation).where(
                        PostAnnotation.annotation_version == self.annotation_version
                    )
                ).all()
            }
            media_map: dict[int, MediaAsset] = {}
            for post in posts:
                media = session.scalar(
                    select(MediaAsset)
                    .join(PostMedia, PostMedia.media_asset_id == MediaAsset.id)
                    .where(PostMedia.post_id == post.id)
                    .order_by(PostMedia.position)
                    .limit(1)
                )
                if media is not None:
                    media_map[post.id] = media
            post_ids = [post.id for post in posts]
            if post_ids:
                session.execute(
                    delete(SimilarityEdge).where(
                        (SimilarityEdge.source_post_id.in_(post_ids))
                        | (SimilarityEdge.target_post_id.in_(post_ids))
                    )
                )
            count = 0
            for index, source in enumerate(posts):
                for target in posts[index + 1 :]:
                    source_media = media_map.get(source.id)
                    target_media = media_map.get(target.id)
                    visual = 0.0
                    if (
                        source_media
                        and target_media
                        and source_media.embedding_vector
                        and target_media.embedding_vector
                    ):
                        visual = cosine_similarity(
                            source_media.embedding_vector, target_media.embedding_vector
                        )
                    caption = text_similarity(source.caption or "", target.caption or "")
                    concept = self._concept_similarity(
                        annotation_map.get(source.id), annotation_map.get(target.id)
                    )
                    session.add(
                        SimilarityEdge(
                            source_post_id=source.id,
                            target_post_id=target.id,
                            visual_similarity=visual,
                            caption_similarity=caption,
                            concept_similarity=concept,
                        )
                    )
                    count += 1
            return count

    def correct_annotation(
        self,
        post_id: int,
        fields: dict[str, object],
        *,
        review_note: str | None = None,
    ) -> dict[str, object]:
        return self.review_annotation(post_id, fields, review_note=review_note)

    def review_annotation(
        self,
        post_id: int,
        expected_fields: dict[str, object],
        *,
        review_note: str | None = None,
    ) -> dict[str, object]:
        """Record reviewed labels and apply only fields that differ as corrections."""
        allowed = {
            "franchise",
            "show_name",
            "characters",
            "visible_character_count",
            "scene_description",
            "visual_format",
            "composition",
            "emotion",
            "reaction_potential",
            "caption_intent",
            "caption_structure",
            "humor_style",
            "tone",
            "text_in_image",
        }
        unexpected = set(expected_fields) - allowed
        if unexpected:
            raise ValueError(f"unsupported correction fields: {', '.join(sorted(unexpected))}")
        if not expected_fields:
            raise ValueError("at least one reviewed field is required")
        self._validate_correction_fields(expected_fields)
        with self.database.session() as session:
            annotation = session.scalar(
                select(PostAnnotation)
                .where(
                    PostAnnotation.post_id == post_id,
                    PostAnnotation.annotation_version == self.annotation_version,
                )
                .order_by(PostAnnotation.created_at.desc())
                .limit(1)
            )
            if annotation is None:
                raise LookupError(f"post {post_id} has no annotation")
            current = json.loads(annotation.reviewed_fields_json or "{}")
            effective_before = effective_annotation_fields(annotation)
            changed = {
                key: value
                for key, value in expected_fields.items()
                if effective_before.get(key) != value
            }
            if changed:
                current.update(changed)
                annotation.reviewed_fields_json = json.dumps(current, sort_keys=True)
                session.add(
                    AnnotationCorrection(
                        post_annotation_id=annotation.id,
                        fields_json=json.dumps(changed, sort_keys=True),
                    )
                )
                correction_details: dict[str, object] = {"fields": sorted(changed)}
                if review_note:
                    correction_details["review_note"] = review_note
                audit(
                    session,
                    "annotation_corrected",
                    "post",
                    post_id,
                    correction_details,
                )
            annotation.review_status = "reviewed"
            review_details: dict[str, object] = {
                "annotation_id": annotation.id,
                "annotation_version": annotation.annotation_version,
                "expected_fields": expected_fields,
                "corrected_fields": sorted(changed),
            }
            if review_note:
                review_details["review_note"] = review_note
            audit(session, "annotation_reviewed", "post", post_id, review_details)
        return self.effective_annotation(post_id)

    def effective_annotation(self, post_id: int) -> dict[str, object]:
        with self.database.session() as session:
            annotation = session.scalar(
                select(PostAnnotation)
                .where(
                    PostAnnotation.post_id == post_id,
                    PostAnnotation.annotation_version == self.annotation_version,
                )
                .order_by(PostAnnotation.created_at.desc())
                .limit(1)
            )
            if annotation is None:
                raise LookupError(f"post {post_id} has no annotation")
            original = json.loads(annotation.original_output_json)
            corrections = json.loads(annotation.reviewed_fields_json or "{}")
            effective = dict(original)
            for key, value in corrections.items():
                effective[CORRECTION_OUTPUT_ALIASES.get(key, key)] = value
            return {
                "post_id": post_id,
                "annotation_id": annotation.id,
                "version": annotation.annotation_version,
                "review_status": annotation.review_status,
                "original": original,
                "corrections": corrections,
                "effective": effective,
            }

    def similar_posts(self, post_id: int, limit: int = 8) -> list[dict[str, object]]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            source = session.get(Post, post_id)
            if source is None or source.channel_id != channel.id:
                raise LookupError(f"post {post_id} was not found in @{channel.handle}")
            edges = session.scalars(
                select(SimilarityEdge).where(
                    (SimilarityEdge.source_post_id == post_id)
                    | (SimilarityEdge.target_post_id == post_id)
                )
            ).all()
            scored = []
            for edge in edges:
                target_id = (
                    edge.target_post_id if edge.source_post_id == post_id else edge.source_post_id
                )
                score = (
                    0.55 * edge.visual_similarity
                    + 0.25 * edge.caption_similarity
                    + 0.2 * edge.concept_similarity
                )
                scored.append((score, target_id, edge))
            results: list[dict[str, object]] = []
            for score, target_id, edge in sorted(scored, reverse=True)[:limit]:
                post = session.get(Post, target_id)
                if post and post.channel_id == channel.id:
                    results.append(
                        {
                            "post_id": post.id,
                            "caption": post.caption,
                            "published_at": (
                                post.published_at.isoformat() if post.published_at else None
                            ),
                            "score": round(score, 6),
                            "visual_similarity": round(edge.visual_similarity, 6),
                            "caption_similarity": round(edge.caption_similarity, 6),
                            "concept_similarity": round(edge.concept_similarity, 6),
                        }
                    )
            return results

    @staticmethod
    def _annotation_record(post_id: int, output: HistoricalAnnotation) -> PostAnnotation:
        return PostAnnotation(
            post_id=post_id,
            annotation_version=AnalysisService.annotation_version,
            franchise=output.franchise,
            show_name=output.show_name,
            characters_json=json.dumps(output.visible_characters),
            visible_character_count=output.visible_character_count,
            scene_description=output.scene_description,
            visual_format=output.visual_medium,
            composition=output.composition,
            emotion=output.facial_emotional_cues,
            reaction_potential=output.reaction_potential,
            caption_intent=output.caption_intent,
            caption_structure=output.caption_structure,
            humor_style=output.humor_style,
            tone=output.tone,
            text_in_image=output.text_overlay,
            model_confidence_json=json.dumps(output.confidence.model_dump(), sort_keys=True),
            original_output_json=output.model_dump_json(),
            review_status="unreviewed",
            reviewed_fields_json="{}",
        )

    @staticmethod
    def _concept_similarity(first: PostAnnotation | None, second: PostAnnotation | None) -> float:
        if first is None or second is None:
            return 0.0
        first_effective = effective_annotation_fields(first)
        second_effective = effective_annotation_fields(second)
        first_values = {
            first_effective["franchise"],
            first_effective["composition"],
            *cast(list[object], first_effective["characters"]),
            *cast(list[object], first_effective["actions"]),
            *cast(list[object], first_effective["objects"]),
        }
        second_values = {
            second_effective["franchise"],
            second_effective["composition"],
            *cast(list[object], second_effective["characters"]),
            *cast(list[object], second_effective["actions"]),
            *cast(list[object], second_effective["objects"]),
        }
        first_values.discard(None)
        second_values.discard(None)
        union = first_values | second_values
        return len(first_values & second_values) / len(union) if union else 0.0

    @staticmethod
    def _validate_correction_fields(fields: dict[str, object]) -> None:
        nullable_text = {"franchise", "show_name"}
        text_fields = {
            "scene_description",
            "visual_format",
            "composition",
            "emotion",
            "reaction_potential",
            "caption_intent",
            "caption_structure",
            "humor_style",
            "tone",
        }
        for key in nullable_text:
            if key in fields and fields[key] is not None and not isinstance(fields[key], str):
                raise ValueError(f"{key} must be text or null")
        for key in text_fields:
            if key in fields and not isinstance(fields[key], str):
                raise ValueError(f"{key} must be text")
        if "characters" in fields and (
            not isinstance(fields["characters"], list)
            or not all(isinstance(value, str) for value in cast(list[object], fields["characters"]))
        ):
            raise ValueError("characters must be a list of strings")
        if "visible_character_count" in fields and (
            isinstance(fields["visible_character_count"], bool)
            or not isinstance(fields["visible_character_count"], int)
            or fields["visible_character_count"] < 0
        ):
            raise ValueError("visible_character_count must be a non-negative integer")
        if "text_in_image" in fields and not isinstance(fields["text_in_image"], bool):
            raise ValueError("text_in_image must be true or false")

    def _record_model_run(
        self,
        *,
        task: str,
        prompt_version: str,
        input_ids: Sequence[int],
        request: dict[str, object],
        output: dict[str, object] | None,
        started: datetime,
        error: str | None,
    ) -> None:
        with self.database.session() as session:
            session.add(
                ModelRun(
                    task_type=task,
                    provider=self.runtime.provider,
                    model=self.runtime.model_name,
                    prompt_version=prompt_version,
                    input_record_ids_json=json.dumps(list(input_ids)),
                    request_summary_json=json.dumps(request, sort_keys=True, default=str),
                    structured_output_json=json.dumps(output or {}, sort_keys=True, default=str),
                    token_usage_json=json.dumps(
                        getattr(self.runtime, "last_token_usage", {}), sort_keys=True
                    ),
                    started_at=started,
                    completed_at=datetime.now(UTC),
                    status="failed" if error else "completed",
                    error_summary=error,
                )
            )


def image_matrix_for_posts(
    database: Database, post_ids: Sequence[int]
) -> tuple[np.ndarray, list[int]]:
    vectors: list[np.ndarray] = []
    matched: list[int] = []
    with database.session() as session:
        for post_id in post_ids:
            media_rows = session.scalars(
                select(MediaAsset)
                .join(PostMedia, PostMedia.media_asset_id == MediaAsset.id)
                .where(PostMedia.post_id == post_id)
                .order_by(PostMedia.position)
            ).all()
            media_vectors = [
                np.frombuffer(media.embedding_vector, dtype=np.float32)
                for media in media_rows
                if media.embedding_vector
            ]
            dimensions = {vector.size for vector in media_vectors}
            if media_vectors and len(dimensions) == 1:
                pooled = np.mean(np.vstack(media_vectors), axis=0)
                norm = float(np.linalg.norm(pooled))
                vectors.append(pooled / norm if norm else pooled)
                matched.append(post_id)
    if not vectors:
        return np.empty((0, 0), dtype=np.float32), []
    return np.vstack(vectors), matched


def caption_matrix(captions: Sequence[str]) -> np.ndarray:
    return np.vstack([text_embedding(caption) for caption in captions])
