from __future__ import annotations

import math
from collections import Counter
from typing import Any, cast

import numpy as np
from sqlalchemy import select

from runway.analysis.service import AnalysisService, effective_annotation_fields
from runway.captions.taxonomy import analyze_caption
from runway.db.base import Database
from runway.db.models import Post, PostAnnotation, RepresentationRecord
from runway.intelligence.embeddings import ActiveRepresentationResolver, cosine


class ChannelContentModeService:
    """Learn interpretable per-channel modes from the active multimodal space."""

    version = "channel-content-modes-v1"

    def __init__(
        self,
        database: Database,
        resolver: ActiveRepresentationResolver | None = None,
    ):
        self.database = database
        self.representations = resolver or ActiveRepresentationResolver(database)

    def learn(
        self,
        *,
        channel_id: int,
        post_ids: list[int],
        maximum_modes: int = 7,
    ) -> dict[str, object]:
        active_set = self.representations.store.active_set(
            channel_id=channel_id,
            scope="historical_multimodal",
            purpose="historical_pair_semantics",
        )
        if active_set is None:
            return {
                "version": self.version,
                "status": "blocked_no_active_multimodal_set",
                "modes": [],
                "neural": False,
            }
        wanted = set(post_ids)
        records = [
            record
            for record in self.representations.store.records_for_set(active_set.id)
            if record.entity_type == "post" and record.entity_id in wanted
        ]
        records.sort(key=lambda row: row.entity_id)
        if len(records) < 3:
            return {
                "version": self.version,
                "status": "blocked_insufficient_representations",
                "modes": [],
                "represented_posts": len(records),
                "neural": active_set.provider != "runway-local",
            }
        vectors = np.vstack([self.representations.store.vectors(record)[0] for record in records])
        mode_count = min(
            maximum_modes,
            max(2, min(len(records), round(math.sqrt(len(records) / 12)) + 1)),
        )
        medoids, assignments = self._k_medoids(vectors, mode_count)
        with self.database.session() as session:
            posts = {
                post.id: post
                for post in session.scalars(
                    select(Post).where(Post.id.in_([record.entity_id for record in records]))
                ).all()
            }
            annotations: dict[int, PostAnnotation] = {}
            priority = {
                version: index
                for index, version in enumerate(
                    reversed(AnalysisService.compatible_annotation_versions)
                )
            }
            for annotation in session.scalars(
                select(PostAnnotation).where(
                    PostAnnotation.post_id.in_(list(posts)),
                    PostAnnotation.annotation_version.in_(
                        AnalysisService.compatible_annotation_versions
                    ),
                )
            ):
                current = annotations.get(annotation.post_id)
                if current is None or priority.get(
                    annotation.annotation_version,
                    -1,
                ) > priority.get(current.annotation_version, -1):
                    annotations[annotation.post_id] = annotation

        recent_post_ids = {
            post.id
            for post in sorted(
                posts.values(),
                key=lambda row: (row.published_at is not None, row.published_at, row.id),
                reverse=True,
            )[:20]
        }

        modes: list[dict[str, object]] = []
        for cluster_index, medoid_index in enumerate(medoids):
            member_indexes = [
                index for index, assignment in enumerate(assignments) if assignment == cluster_index
            ]
            member_post_ids = [records[index].entity_id for index in member_indexes]
            member_posts = [posts[post_id] for post_id in member_post_ids if post_id in posts]
            effective = [
                effective_annotation_fields(annotations[post_id])
                for post_id in member_post_ids
                if post_id in annotations
            ]
            structures = Counter(
                analyze_caption(post.caption or "").structure for post in member_posts
            )
            entities: Counter[str] = Counter()
            scenes: Counter[str] = Counter()
            angles: Counter[str] = Counter()
            for fields in effective:
                for entity in cast(list[Any], fields.get("entities", [])):
                    if isinstance(entity, dict):
                        name = str(entity.get("canonical_name") or entity.get("name") or "")
                    else:
                        name = str(entity)
                    if name.strip():
                        entities[name.strip()] += 1
                scene = str(fields.get("scene_description") or "").strip()
                if scene:
                    scenes[scene] += 1
                angle = str(
                    fields.get("editorial_angle") or fields.get("caption_intent") or ""
                ).strip()
                if angle:
                    angles[angle] += 1
            medoid_record = records[medoid_index]
            medoid_post = posts.get(medoid_record.entity_id)
            label_parts = [
                structures.most_common(1)[0][0] if structures else "mixed structure",
                entities.most_common(1)[0][0] if entities else "mixed subject",
            ]
            recent_count = sum(post.id in recent_post_ids for post in member_posts)
            modes.append(
                {
                    "mode_id": f"mode-{cluster_index + 1}",
                    "learned_label": " · ".join(label_parts),
                    "member_count": len(member_post_ids),
                    "representative_post_ids": self._representatives(
                        vectors,
                        member_indexes,
                        medoid_index,
                        records,
                    ),
                    "visual_prototype": {
                        "medoid_post_id": medoid_record.entity_id,
                        "representation_record_id": medoid_record.id,
                        "representation_set_id": active_set.id,
                    },
                    "caption_prototype": {
                        "medoid_caption": medoid_post.caption if medoid_post is not None else "",
                        "common_structures": structures.most_common(5),
                    },
                    "common_entities": entities.most_common(10),
                    "common_scenes": scenes.most_common(5),
                    "preferred_structures": structures.most_common(5),
                    "preferred_editorial_angles": angles.most_common(5),
                    "positive_feedback": {"count": 0, "status": "awaiting_mode_linked_labels"},
                    "negative_feedback": {"count": 0, "status": "awaiting_mode_linked_labels"},
                    "recent_exposure": recent_count,
                    "fatigue_state": (
                        "high" if recent_count >= 7 else "moderate" if recent_count >= 4 else "low"
                    ),
                }
            )
        return {
            "version": self.version,
            "status": "learned",
            "method": "active-multimodal-k-medoids",
            "neural": active_set.provider != "runway-local",
            "representation_set": {
                "id": active_set.id,
                "provider": active_set.provider,
                "model": active_set.model,
                "model_version": active_set.model_version,
                "configuration_hash": active_set.configuration_hash,
            },
            "mode_count": len(modes),
            "represented_posts": len(records),
            "modes": modes,
        }

    def score_candidate(
        self,
        *,
        channel_id: int,
        candidate_description: str,
        content_modes: dict[str, object],
    ) -> list[dict[str, object]]:
        modes = content_modes.get("modes", [])
        if not isinstance(modes, list):
            return []
        scored: list[dict[str, object]] = []
        for mode in modes:
            if not isinstance(mode, dict):
                continue
            prototype = mode.get("caption_prototype", {})
            prototype_text = (
                str(prototype.get("medoid_caption") or "") if isinstance(prototype, dict) else ""
            )
            descriptor = " ".join(
                [
                    str(mode.get("learned_label") or ""),
                    prototype_text,
                    " ".join(
                        str(row[0])
                        for row in mode.get("common_entities", [])
                        if isinstance(row, (list, tuple)) and row
                    ),
                ]
            ).strip()
            similarity = self.representations.text_similarity(
                channel_id,
                candidate_description,
                descriptor,
                score_purpose="content_mode_gating",
            )
            scored.append(
                {
                    "mode_id": mode.get("mode_id"),
                    "score": round(max(0.0, similarity.score), 6),
                    "fatigue_state": mode.get("fatigue_state"),
                    "representation": similarity.representation.as_dict(),
                }
            )
        scored.sort(key=lambda row: (-self._score_value(row["score"]), str(row["mode_id"])))
        return scored

    @staticmethod
    def _score_value(value: object) -> float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return 0.0

    @staticmethod
    def _k_medoids(vectors: np.ndarray, k: int) -> tuple[list[int], list[int]]:
        similarities = vectors @ vectors.T
        distances = 1.0 - similarities
        medoids = [0]
        while len(medoids) < k:
            candidate = max(
                (index for index in range(len(vectors)) if index not in medoids),
                key=lambda index: (min(distances[index, medoid] for medoid in medoids), -index),
            )
            medoids.append(candidate)
        assignments = [0] * len(vectors)
        for _iteration in range(20):
            assignments = [
                min(
                    range(len(medoids)),
                    key=lambda cluster: (distances[index, medoids[cluster]], cluster),
                )
                for index in range(len(vectors))
            ]
            updated: list[int] = []
            for cluster in range(k):
                members = [
                    index for index, assignment in enumerate(assignments) if assignment == cluster
                ]
                if not members:
                    updated.append(medoids[cluster])
                    continue
                updated.append(
                    min(
                        members,
                        key=lambda candidate: (
                            float(sum(distances[candidate, other] for other in members)),
                            candidate,
                        ),
                    )
                )
            if updated == medoids:
                break
            medoids = updated
        assignments = [
            min(
                range(len(medoids)),
                key=lambda cluster: (distances[index, medoids[cluster]], cluster),
            )
            for index in range(len(vectors))
        ]
        return medoids, assignments

    @staticmethod
    def _representatives(
        vectors: np.ndarray,
        member_indexes: list[int],
        medoid_index: int,
        records: list[RepresentationRecord],
    ) -> list[int]:
        ordered = sorted(
            member_indexes,
            key=lambda index: (
                -cosine(vectors[index], vectors[medoid_index]),
                records[index].entity_id,
            ),
        )
        return [records[index].entity_id for index in ordered[:5]]
