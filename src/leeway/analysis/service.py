from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import numpy as np
from sqlalchemy import delete, select

from leeway.analysis.features import text_embedding, text_similarity
from leeway.analysis.runtime import AgentRuntime, runtime_for
from leeway.analysis.schemas import HistoricalAnnotation
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import (
    AnnotationCorrection,
    MediaAsset,
    ModelRun,
    Post,
    PostAnnotation,
    PostMedia,
    SimilarityEdge,
    utcnow,
)
from leeway.db.repositories import audit
from leeway.media.service import cosine_similarity


class AnalysisService:
    annotation_version = "historical-annotation-v1"
    prompt_version = "annotate-history-v1"

    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime: AgentRuntime | None = None,
    ):
        self.database = database
        self.settings = settings
        self.runtime = runtime or runtime_for(settings)

    async def analyze_history(self, *, resume: bool = True) -> dict[str, int]:
        with self.database.session() as session:
            posts = session.scalars(
                select(Post).where(Post.is_training_eligible.is_(True)).order_by(Post.id)
            ).all()
            existing_ids = set(
                session.scalars(
                    select(PostAnnotation.post_id).where(
                        PostAnnotation.annotation_version == self.annotation_version
                    )
                ).all()
            )
            payloads: list[dict[str, object]] = [
                {
                    "post_id": post.id,
                    "caption": post.caption or "",
                    "post_type": post.post_type,
                    "published_at": post.published_at.isoformat() if post.published_at else None,
                }
                for post in posts
                if not resume or post.id not in existing_ids
            ]

        completed = 0
        failed = 0
        skipped = len(posts) - len(payloads)
        for payload in payloads:
            post_id = cast(int, payload["post_id"])
            started = utcnow()
            try:
                output = await self.runtime.annotate_historical_post(payload)
            except Exception as exc:
                failed += 1
                self._record_model_run(
                    task="annotate_historical_post",
                    prompt_version=self.prompt_version,
                    input_ids=[post_id],
                    request=payload,
                    output=None,
                    started=started,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            with self.database.session() as session:
                existing = session.scalar(
                    select(PostAnnotation).where(
                        PostAnnotation.post_id == post_id,
                        PostAnnotation.annotation_version == self.annotation_version,
                    )
                )
                if existing is None:
                    session.add(self._annotation_record(post_id, output))
                audit(
                    session,
                    "historical_post_annotated",
                    "post",
                    post_id,
                    {"annotation_version": self.annotation_version},
                )
            self._record_model_run(
                task="annotate_historical_post",
                prompt_version=self.prompt_version,
                input_ids=[post_id],
                request=payload,
                output=output.model_dump(),
                started=started,
                error=None,
            )
            completed += 1
        edges = self.rebuild_similarity_edges()
        return {"completed": completed, "skipped": skipped, "failed": failed, "edges": edges}

    def rebuild_similarity_edges(self) -> int:
        with self.database.session() as session:
            posts = session.scalars(
                select(Post).where(Post.is_training_eligible.is_(True)).order_by(Post.id)
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
            session.execute(delete(SimilarityEdge))
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

    def correct_annotation(self, post_id: int, fields: dict[str, object]) -> dict[str, object]:
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
        unexpected = set(fields) - allowed
        if unexpected:
            raise ValueError(f"unsupported correction fields: {', '.join(sorted(unexpected))}")
        with self.database.session() as session:
            annotation = session.scalar(
                select(PostAnnotation)
                .where(PostAnnotation.post_id == post_id)
                .order_by(PostAnnotation.created_at.desc())
                .limit(1)
            )
            if annotation is None:
                raise LookupError(f"post {post_id} has no annotation")
            current = json.loads(annotation.reviewed_fields_json or "{}")
            current.update(fields)
            annotation.reviewed_fields_json = json.dumps(current, sort_keys=True)
            annotation.review_status = "reviewed"
            session.add(
                AnnotationCorrection(
                    post_annotation_id=annotation.id,
                    fields_json=json.dumps(fields, sort_keys=True),
                )
            )
            audit(session, "annotation_corrected", "post", post_id, {"fields": sorted(fields)})
        return self.effective_annotation(post_id)

    def effective_annotation(self, post_id: int) -> dict[str, object]:
        with self.database.session() as session:
            annotation = session.scalar(
                select(PostAnnotation)
                .where(PostAnnotation.post_id == post_id)
                .order_by(PostAnnotation.created_at.desc())
                .limit(1)
            )
            if annotation is None:
                raise LookupError(f"post {post_id} has no annotation")
            original = json.loads(annotation.original_output_json)
            corrections = json.loads(annotation.reviewed_fields_json or "{}")
            effective = dict(original)
            aliases = {
                "characters": "visible_characters",
                "visual_format": "visual_medium",
                "emotion": "facial_emotional_cues",
                "text_in_image": "text_overlay",
            }
            for key, value in corrections.items():
                effective[aliases.get(key, key)] = value
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
                if post:
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
            model_confidence_json=json.dumps(output.confidence, sort_keys=True),
            original_output_json=output.model_dump_json(),
            review_status="unreviewed",
            reviewed_fields_json="{}",
        )

    @staticmethod
    def _concept_similarity(first: PostAnnotation | None, second: PostAnnotation | None) -> float:
        if first is None or second is None:
            return 0.0
        first_values = {first.franchise, first.composition, *json.loads(first.characters_json)}
        second_values = {second.franchise, second.composition, *json.loads(second.characters_json)}
        first_values.discard(None)
        second_values.discard(None)
        union = first_values | second_values
        return len(first_values & second_values) / len(union) if union else 0.0

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
            media = session.scalar(
                select(MediaAsset)
                .join(PostMedia, PostMedia.media_asset_id == MediaAsset.id)
                .where(PostMedia.post_id == post_id)
                .order_by(PostMedia.position)
                .limit(1)
            )
            if media and media.embedding_vector:
                vectors.append(np.frombuffer(media.embedding_vector, dtype=np.float32))
                matched.append(post_id)
    if not vectors:
        return np.empty((0, 0), dtype=np.float32), []
    return np.vstack(vectors), matched


def caption_matrix(captions: Sequence[str]) -> np.ndarray:
    return np.vstack([text_embedding(caption) for caption in captions])
