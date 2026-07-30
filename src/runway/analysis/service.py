from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import numpy as np
from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from runway.analysis.features import text_embedding
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
from runway.media.service import prepare_model_image

CORRECTION_OUTPUT_ALIASES = {
    "characters": "visible_characters",
    "visual_format": "visual_medium",
    "emotion": "facial_emotional_cues",
    "text_in_image": "text_overlay",
}

SIMILARITY_INSERT_BATCH_SIZE = 2_000


def _media_assets_by_post(
    session: Session,
    post_ids: Sequence[int],
) -> dict[int, list[MediaAsset]]:
    """Load ordered post media in one query instead of one query per post."""

    if not post_ids:
        return {}
    grouped: defaultdict[int, list[MediaAsset]] = defaultdict(list)
    rows = session.execute(
        select(PostMedia.post_id, MediaAsset)
        .join(MediaAsset, MediaAsset.id == PostMedia.media_asset_id)
        .where(PostMedia.post_id.in_(post_ids))
        .order_by(PostMedia.post_id, PostMedia.position, MediaAsset.id)
    ).all()
    for post_id, media in rows:
        grouped[post_id].append(media)
    return dict(grouped)


def _normalized_media_vector(media: MediaAsset | None) -> np.ndarray | None:
    if media is None or not media.embedding_vector:
        return None
    vector = np.frombuffer(media.embedding_vector, dtype=np.float32)
    if not vector.size:
        return None
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


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
        confidence_payload.get("characters", 0.0) if isinstance(confidence_payload, dict) else 0.0
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
            post_ids = [post.id for post in posts]
            existing_ids = (
                set(
                    session.scalars(
                        select(PostAnnotation.post_id).where(
                            PostAnnotation.post_id.in_(post_ids),
                            PostAnnotation.annotation_version == self.annotation_version,
                        )
                    ).all()
                )
                if post_ids
                else set()
            )
            pending_posts = [post for post in posts if not resume or post.id not in existing_ids]
            media_by_post = _media_assets_by_post(
                session,
                [post.id for post in pending_posts],
            )
            pending = [
                (post, media_by_post[post.id])
                for post in pending_posts
                if media_by_post.get(post.id)
            ]

        pending_before_limit = len(pending)
        if max_posts is not None:
            pending = pending[:max_posts]
        payloads: list[dict[str, object]] = []
        for post, media_assets in pending:
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
                    "published_at": (post.published_at.isoformat() if post.published_at else None),
                    "_image_paths": [str(path) for path in prepared_paths],
                }
            )

        completed = 0
        failed = 0
        skipped = len(posts) - pending_before_limit
        deferred = pending_before_limit - len(payloads)
        batches = 0
        refreshed_post_ids: list[int] = []
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
                existing_by_id = {
                    row.post_id: row
                    for row in session.scalars(
                        select(PostAnnotation).where(
                            PostAnnotation.post_id.in_(post_ids),
                            PostAnnotation.annotation_version == self.annotation_version,
                        )
                    ).all()
                }
                for post_id in post_ids:
                    existing = existing_by_id.get(post_id)
                    if existing is None:
                        session.add(self._annotation_record(post_id, annotations[post_id]))
                    else:
                        self._refresh_annotation_record(existing, annotations[post_id])
                    audit(
                        session,
                        "historical_post_annotated",
                        "post",
                        post_id,
                        {
                            "annotation_version": self.annotation_version,
                            "refreshed": existing is not None,
                        },
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
            refreshed_post_ids.extend(post_ids)
            batches += 1
        expected_edges = len(posts) * (len(posts) - 1) // 2
        current_edges = self._similarity_edge_count()
        if refreshed_post_ids:
            edges = self.refresh_similarity_edges(refreshed_post_ids)
        elif current_edges != expected_edges:
            edges = self.rebuild_similarity_edges()
        else:
            edges = current_edges
        return {
            "completed": completed,
            "skipped": skipped,
            "failed": failed,
            "batches": batches,
            "deferred": deferred,
            "edges": edges,
        }

    def rebuild_similarity_edges(self) -> int:
        return self._refresh_similarity_edges(None)

    def refresh_similarity_edges(self, changed_post_ids: Sequence[int]) -> int:
        """Update changed neighborhoods and repair an incomplete graph if needed."""

        edges = self._refresh_similarity_edges(changed_post_ids)
        expected = self._expected_similarity_edge_count()
        return edges if edges == expected else self.rebuild_similarity_edges()

    def _refresh_similarity_edges(self, changed_post_ids: Sequence[int] | None) -> int:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            channel_post_ids = session.scalars(
                select(Post.id).where(Post.channel_id == channel.id)
            ).all()
            posts = session.scalars(
                select(Post)
                .where(
                    Post.channel_id == channel.id,
                    Post.is_training_eligible.is_(True),
                )
                .order_by(Post.id)
            ).all()
            eligible_ids = {post.id for post in posts}
            annotation_map = (
                {
                    annotation.post_id: annotation
                    for annotation in session.scalars(
                        select(PostAnnotation).where(
                            PostAnnotation.post_id.in_(eligible_ids),
                            PostAnnotation.annotation_version == self.annotation_version,
                        )
                    ).all()
                }
                if eligible_ids
                else {}
            )
            media_rows = _media_assets_by_post(session, list(eligible_ids))
            media_map = {post_id: rows[0] for post_id, rows in media_rows.items() if rows}

            full_rebuild = changed_post_ids is None
            changed_ids = (
                set(channel_post_ids)
                if changed_post_ids is None
                else set(changed_post_ids) & set(channel_post_ids)
            )
            if changed_ids:
                session.execute(
                    delete(SimilarityEdge).where(
                        (SimilarityEdge.source_post_id.in_(changed_ids))
                        | (SimilarityEdge.target_post_id.in_(changed_ids))
                    )
                )

            caption_vectors = {post.id: text_embedding(post.caption or "") for post in posts}
            image_vectors = {
                post_id: vector
                for post_id, media in media_map.items()
                if (vector := _normalized_media_vector(media)) is not None
            }
            concept_values = {
                post_id: self._concept_values(annotation)
                for post_id, annotation in annotation_map.items()
            }
            calculated_at = utcnow()
            batch: list[dict[str, object]] = []
            for index, source in enumerate(posts):
                for target in posts[index + 1 :]:
                    if not full_rebuild and not ({source.id, target.id} & changed_ids):
                        continue
                    source_media = image_vectors.get(source.id)
                    target_media = image_vectors.get(target.id)
                    visual = 0.0
                    if (
                        source_media is not None
                        and target_media is not None
                        and source_media.shape == target_media.shape
                    ):
                        visual = float(np.dot(source_media, target_media))
                    caption = float(np.dot(caption_vectors[source.id], caption_vectors[target.id]))
                    concept = self._jaccard(
                        concept_values.get(source.id, set()),
                        concept_values.get(target.id, set()),
                    )
                    batch.append(
                        {
                            "source_post_id": source.id,
                            "target_post_id": target.id,
                            "visual_similarity": visual,
                            "caption_similarity": caption,
                            "concept_similarity": concept,
                            "calculated_at": calculated_at,
                        }
                    )
                    if len(batch) >= SIMILARITY_INSERT_BATCH_SIZE:
                        session.execute(insert(SimilarityEdge), batch)
                        batch.clear()
            if batch:
                session.execute(insert(SimilarityEdge), batch)
        return self._similarity_edge_count()

    def _similarity_edge_count(self) -> int:
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(SimilarityEdge)
                    .join(Post, Post.id == SimilarityEdge.source_post_id)
                    .where(Post.channel_id == channel_id)
                )
                or 0
            )

    def _expected_similarity_edge_count(self) -> int:
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            posts = int(
                session.scalar(
                    select(func.count(Post.id)).where(
                        Post.channel_id == channel_id,
                        Post.is_training_eligible.is_(True),
                    )
                )
                or 0
            )
        return posts * (posts - 1) // 2

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
        changed: dict[str, object] = {}
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
        if changed:
            self.refresh_similarity_edges([post_id])
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
    def _refresh_annotation_record(
        annotation: PostAnnotation,
        output: HistoricalAnnotation,
    ) -> None:
        """Refresh model-owned fields while preserving human review overlays."""

        fresh = AnalysisService._annotation_record(annotation.post_id, output)
        for field in (
            "franchise",
            "show_name",
            "characters_json",
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
            "model_confidence_json",
            "original_output_json",
        ):
            setattr(annotation, field, getattr(fresh, field))

    @staticmethod
    def _concept_similarity(first: PostAnnotation | None, second: PostAnnotation | None) -> float:
        if first is None or second is None:
            return 0.0
        return AnalysisService._jaccard(
            AnalysisService._concept_values(first),
            AnalysisService._concept_values(second),
        )

    @staticmethod
    def _concept_values(annotation: PostAnnotation) -> set[object]:
        effective = effective_annotation_fields(annotation)
        values = {
            effective["franchise"],
            effective["composition"],
            *cast(list[object], effective["characters"]),
            *cast(list[object], effective["actions"]),
            *cast(list[object], effective["objects"]),
        }
        values.discard(None)
        return values

    @staticmethod
    def _jaccard(first_values: set[object], second_values: set[object]) -> float:
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
        media_by_post = _media_assets_by_post(session, post_ids)
    for post_id in post_ids:
        media_vectors = [
            np.frombuffer(media.embedding_vector, dtype=np.float32)
            for media in media_by_post.get(post_id, [])
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
