from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
from sqlalchemy import desc, func, select

from leeway.analysis.features import aggregate_caption_features, qlob_style_score, text_embedding
from leeway.analysis.runtime import AgentRuntime, runtime_for
from leeway.analysis.service import image_matrix_for_posts
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import (
    AnnotationCorrection,
    MediaAsset,
    ModelRun,
    Post,
    PostAnnotation,
    PostMedia,
    Proposal,
    StyleProfile,
    utcnow,
)
from leeway.db.repositories import audit, get_channel
from leeway.media.service import cosine_similarity, ensure_fixture_images, inspect_image


class StyleProfileService:
    schema_version = "style-profile-v1"

    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime: AgentRuntime | None = None,
    ):
        self.database = database
        self.settings = settings
        self.runtime = runtime or runtime_for(settings)

    async def build(self) -> dict[str, object]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            posts = session.scalars(
                select(Post).where(Post.is_training_eligible.is_(True)).order_by(Post.id)
            ).all()
            if len(posts) < 3:
                raise ValueError("at least three training-eligible posts are required")
            holdout_count = max(1, round(len(posts) * 0.2))
            holdout_ids = [post.id for post in posts[::5]][:holdout_count]
            if len(holdout_ids) < holdout_count:
                holdout_ids.extend(post.id for post in posts if post.id not in holdout_ids)
                holdout_ids = holdout_ids[:holdout_count]
            training = [post for post in posts if post.id not in set(holdout_ids)]
            annotations = {
                annotation.post_id: annotation
                for annotation in session.scalars(
                    select(PostAnnotation).where(
                        PostAnnotation.post_id.in_([post.id for post in training])
                    )
                ).all()
            }
            if len(annotations) < len(training):
                raise ValueError("analyze history before building a style profile")
            caption_stats = aggregate_caption_features(post.caption or "" for post in training)
            median_words = float(caption_stats.get("median_words", 0))
            representatives = sorted(
                training,
                key=lambda post: (
                    abs(len((post.caption or "").split()) - median_words),
                    post.id,
                ),
            )[:5]
            representative_media: dict[int, str | None] = {}
            for representative in representatives:
                media = session.scalar(
                    select(MediaAsset)
                    .join(PostMedia, PostMedia.media_asset_id == MediaAsset.id)
                    .where(PostMedia.post_id == representative.id)
                    .order_by(PostMedia.position)
                    .limit(1)
                )
                representative_media[representative.id] = (
                    f"/media/{Path(media.local_path).relative_to('media').as_posix()}"
                    if media
                    else None
                )
            franchise_counts = Counter(
                annotations[post.id].franchise or "unknown" for post in training
            )
            character_counts: Counter[str] = Counter()
            for post in training:
                character_counts.update(json.loads(annotations[post.id].characters_json))
            visual_formats = Counter(annotations[post.id].visual_format for post in training)
            structures = Counter(annotations[post.id].caption_structure for post in training)
            compositions = Counter(annotations[post.id].composition for post in training)
            rejected = session.scalars(
                select(Proposal)
                .where(Proposal.status == "rejected")
                .order_by(desc(Proposal.id))
                .limit(5)
            ).all()
            next_version = (
                session.scalar(
                    select(func.max(StyleProfile.version)).where(
                        StyleProfile.channel_id == channel.id
                    )
                )
                or 0
            ) + 1
            cutoff = max(post.updated_at for post in posts)

        summary_payload: dict[str, object] = {
            "median_caption_words": caption_stats.get("median_words"),
            "question_frequency": caption_stats.get("question_frequency"),
            "dominant_structures": structures.most_common(5),
            "visual_formats": visual_formats.most_common(5),
            "representative_post_ids": [post.id for post in representatives],
        }
        started = utcnow()
        summary = await self.runtime.build_style_summary(summary_payload)
        profile: dict[str, Any] = {
            "schema_version": self.schema_version,
            "version": next_version,
            "channel": self.settings.channel_name,
            "training_post_ids": [post.id for post in training],
            "holdout_post_ids": holdout_ids,
            "holdout_fraction": round(len(holdout_ids) / len(posts), 6),
            "caption_statistics": caption_stats,
            "dominant_caption_structures": structures.most_common(10),
            "visual_formats": visual_formats.most_common(10),
            "visual_compositions": compositions.most_common(10),
            "franchise_distribution": franchise_counts.most_common(),
            "character_distribution": character_counts.most_common(),
            "representative_positive_examples": [
                {
                    "post_id": post.id,
                    "caption": post.caption,
                    "media_url": representative_media[post.id],
                }
                for post in representatives
            ],
            "outliers": self._outliers(training),
            "rotation_patterns": summary.rotation_observations,
            "recent_overuse_rules": {
                "avoid_consecutive_franchise": True,
                "avoid_consecutive_composition": True,
                "duplicate_window_days": self.settings.duplicate_window_days,
            },
            "negative_examples": [
                {"proposal_id": proposal.id, "caption": proposal.final_caption}
                for proposal in rejected
            ],
            "summary": summary.summary,
            "summary_cited_post_ids": summary.cited_post_ids,
        }
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            for current in session.scalars(
                select(StyleProfile).where(StyleProfile.channel_id == channel.id)
            ):
                current.is_active = False
            record = StyleProfile(
                channel_id=channel.id,
                version=next_version,
                catalogue_cutoff=cutoff,
                profile_json=json.dumps(profile, sort_keys=True),
                representative_post_ids_json=json.dumps([post.id for post in representatives]),
                excluded_post_ids_json=json.dumps(holdout_ids),
                is_active=True,
            )
            session.add(record)
            session.add(
                ModelRun(
                    task_type="build_style_summary",
                    provider=self.runtime.provider,
                    model=self.runtime.model_name,
                    prompt_version="style-summary-v1",
                    input_record_ids_json=json.dumps([post.id for post in training]),
                    request_summary_json=json.dumps(summary_payload, sort_keys=True),
                    structured_output_json=summary.model_dump_json(),
                    token_usage_json=json.dumps(
                        getattr(self.runtime, "last_token_usage", {}), sort_keys=True
                    ),
                    started_at=started,
                    completed_at=datetime.now(UTC),
                    status="completed",
                )
            )
            session.flush()
            audit(
                session,
                "style_profile_built",
                "style_profile",
                record.id,
                {"version": next_version, "holdout_ids": holdout_ids},
            )
        self._write_profile_reports(next_version, profile)
        return profile

    def active(self) -> dict[str, Any]:
        with self.database.session() as session:
            record = session.scalar(
                select(StyleProfile)
                .where(StyleProfile.is_active.is_(True))
                .order_by(desc(StyleProfile.version))
                .limit(1)
            )
            if record is None:
                raise LookupError("no active style profile")
            result = cast(dict[str, Any], json.loads(record.profile_json))
            result["id"] = record.id
            result["created_at"] = record.created_at.isoformat()
            result["catalogue_cutoff"] = record.catalogue_cutoff.isoformat()
            return result

    def list_profiles(self) -> list[dict[str, object]]:
        with self.database.session() as session:
            records = session.scalars(
                select(StyleProfile).order_by(desc(StyleProfile.version))
            ).all()
            return [
                {
                    "id": record.id,
                    "version": record.version,
                    "is_active": record.is_active,
                    "catalogue_cutoff": record.catalogue_cutoff.isoformat(),
                    "created_at": record.created_at.isoformat(),
                }
                for record in records
            ]

    def detail(self, profile_id: int) -> dict[str, Any]:
        with self.database.session() as session:
            record = session.get(StyleProfile, profile_id)
            if record is None:
                raise LookupError(f"style profile {profile_id} not found")
            result = cast(dict[str, Any], json.loads(record.profile_json))
            result["id"] = record.id
            result["is_active"] = record.is_active
            result["created_at"] = record.created_at.isoformat()
            return result

    def latest_evaluation(self) -> dict[str, Any]:
        path = self.settings.resolved_data_dir / "reports" / "profile-evaluation.json"
        if not path.is_file():
            raise LookupError("no profile evaluation report exists")
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))

    def evaluate(self) -> dict[str, Any]:
        profile = self.active()
        training_ids = [int(value) for value in cast(list[Any], profile["training_post_ids"])]
        holdout_ids = [int(value) for value in cast(list[Any], profile["holdout_post_ids"])]
        with self.database.session() as session:
            posts = {
                post.id: post
                for post in session.scalars(
                    select(Post).where(Post.id.in_(training_ids + holdout_ids))
                )
            }
            annotations = {
                annotation.post_id: annotation
                for annotation in session.scalars(
                    select(PostAnnotation).where(
                        PostAnnotation.post_id.in_(training_ids + holdout_ids)
                    )
                )
            }

        matching = self._image_caption_matching(training_ids, holdout_ids, posts)
        stats = cast(dict[str, Any], profile["caption_statistics"])
        generic = [
            "Like and subscribe for more!",
            "What do you think? Comment below!",
            "This is an image from a popular show.",
        ]
        ranking_hits = 0
        for post_id in holdout_ids:
            own = qlob_style_score(posts[post_id].caption or "", stats)
            distractor = max(qlob_style_score(caption, stats) for caption in generic)
            ranking_hits += own > distractor
        ranking_accuracy = ranking_hits / len(holdout_ids) if holdout_ids else 0.0

        reviewed_total = 0
        reviewed_correct = 0
        with self.database.session() as session:
            corrected = session.scalars(select(AnnotationCorrection)).all()
            for correction in corrected:
                annotation = session.get(PostAnnotation, correction.post_annotation_id)
                if annotation is None:
                    continue
                original = json.loads(annotation.original_output_json)
                aliases = {
                    "characters": "visible_characters",
                    "visual_format": "visual_medium",
                    "emotion": "facial_emotional_cues",
                    "text_in_image": "text_overlay",
                }
                for field, expected in json.loads(correction.fields_json).items():
                    reviewed_total += 1
                    reviewed_correct += original.get(aliases.get(field, field)) == expected

        retrieval_hits = 0
        train_matrix, train_matched = image_matrix_for_posts(self.database, training_ids)
        holdout_matrix, holdout_matched = image_matrix_for_posts(self.database, holdout_ids)
        for vector, post_id in zip(holdout_matrix, holdout_matched, strict=True):
            similarities = train_matrix @ vector
            top_indices = np.argsort(similarities)[::-1][:3]
            expected = annotations.get(post_id)
            if expected and any(
                annotations.get(train_matched[index])
                and annotations[train_matched[index]].franchise == expected.franchise
                for index in top_indices
            ):
                retrieval_hits += 1
        retrieval_relevance = retrieval_hits / len(holdout_matched) if holdout_matched else 0.0

        duplicate_metrics = self._duplicate_metrics()
        report: dict[str, Any] = {
            "profile_version": profile["version"],
            "training_samples": len(training_ids),
            "holdout_samples": len(holdout_ids),
            "holdout_fraction": profile["holdout_fraction"],
            "image_caption_matching": matching,
            "qlob_caption_ranking_accuracy": round(ranking_accuracy, 6),
            "reviewed_annotation_field_accuracy": (
                round(reviewed_correct / reviewed_total, 6) if reviewed_total else None
            ),
            "reviewed_annotation_fields": reviewed_total,
            "duplicate_detection": duplicate_metrics,
            "retrieval_top3_franchise_relevance": round(retrieval_relevance, 6),
            "sample_errors": matching.get("errors", []),
            "limitations": [
                "Synthetic fixture metrics are directional and do not establish "
                "real-channel quality.",
                "Reviewed annotation accuracy is null until a human correction exists.",
                "The system builds a retrieval profile; it does not fine-tune model weights.",
            ],
        }
        self._write_evaluation(report)
        return report

    def _image_caption_matching(
        self,
        training_ids: list[int],
        holdout_ids: list[int],
        posts: dict[int, Post],
    ) -> dict[str, Any]:
        train_images, train_matched = image_matrix_for_posts(self.database, training_ids)
        holdout_images, holdout_matched = image_matrix_for_posts(self.database, holdout_ids)
        if len(train_matched) < 2 or not holdout_matched:
            return {"accuracy": 0.0, "evaluated": 0, "errors": ["insufficient paired samples"]}
        train_captions = np.vstack(
            [text_embedding(posts[post_id].caption or "") for post_id in train_matched]
        )
        projection = np.linalg.pinv(train_images) @ train_captions
        candidates = np.vstack(
            [text_embedding(posts[post_id].caption or "") for post_id in holdout_matched]
        )
        hits = 0
        errors: list[dict[str, object]] = []
        for index, (image_vector, post_id) in enumerate(
            zip(holdout_images, holdout_matched, strict=True)
        ):
            predicted = image_vector @ projection
            norm = float(np.linalg.norm(predicted)) or 1.0
            scores = candidates @ (predicted / norm)
            selected = int(np.argmax(scores))
            if selected == index:
                hits += 1
            else:
                errors.append(
                    {
                        "post_id": post_id,
                        "selected_post_id": holdout_matched[selected],
                        "score": round(float(scores[selected]), 6),
                    }
                )
        return {
            "accuracy": round(hits / len(holdout_matched), 6),
            "evaluated": len(holdout_matched),
            "errors": errors,
        }

    def _duplicate_metrics(self) -> dict[str, object]:
        assets = ensure_fixture_images(self.settings)
        original = inspect_image(assets["history-01"])
        positives = [
            inspect_image(assets["history-01-resized"]),
            inspect_image(assets["history-01-cropped"]),
            inspect_image(assets["history-01-color"]),
            inspect_image(assets["history-01-bordered"]),
        ]
        negative = inspect_image(assets["unrelated"])

        def transformed_match(candidate: Any) -> bool:
            perceptual = int(original.perceptual_hash, 16) ^ int(candidate.perceptual_hash, 16)
            phash_similarity = 1 - perceptual.bit_count() / 64
            semantic = cosine_similarity(original.embedding, candidate.embedding)
            return phash_similarity >= 0.88 or semantic >= 0.99

        true_positives = sum(transformed_match(candidate) for candidate in positives)
        false_positive = transformed_match(negative)
        return {
            "exact_identity_passed": original.sha256 == original.sha256,
            "transformed_true_positive_rate": round(true_positives / len(positives), 6),
            "unrelated_false_positive_rate": float(false_positive),
            "positive_variants": len(positives),
            "documented_phash_threshold": 0.88,
            "documented_semantic_threshold": 0.99,
        }

    @staticmethod
    def _outliers(posts: list[Post]) -> list[dict[str, object]]:
        ordered = sorted(posts, key=lambda post: len((post.caption or "").split()))
        selected = ordered[:1] + ordered[-1:]
        return [
            {
                "post_id": post.id,
                "caption": post.caption,
                "word_count": len((post.caption or "").split()),
            }
            for post in selected
        ]

    def _write_profile_reports(self, version: int, profile: dict[str, Any]) -> None:
        reports = self.settings.resolved_data_dir / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"style-profile-v{version}.json").write_text(
            json.dumps(profile, indent=2, sort_keys=True), encoding="utf-8"
        )
        stats = profile["caption_statistics"]
        lines = [
            f"# Qlob style profile v{version}",
            "",
            profile["summary"],
            "",
            f"- Training records: {len(profile['training_post_ids'])}",
            f"- Holdout records: {len(profile['holdout_post_ids'])}",
            f"- Median words: {stats['median_words']}",
            f"- Question frequency: {stats.get('question_frequency', 0):.1%}",
            f"- Exclamation frequency: {stats.get('exclamation_frequency', 0):.1%}",
            f"- Representative post IDs: {profile['summary_cited_post_ids']}",
            "",
            "This is a reproducible retrieval/style profile, not model-weight fine-tuning.",
        ]
        (reports / f"style-profile-v{version}.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def _write_evaluation(self, report: dict[str, Any]) -> None:
        reports = self.settings.resolved_data_dir / "reports"
        (reports / "profile-evaluation.json").write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
        lines = [
            f"# Style profile v{report['profile_version']} evaluation",
            "",
            f"- Train / holdout: {report['training_samples']} / {report['holdout_samples']}",
            f"- Image-caption matching: {report['image_caption_matching']['accuracy']:.1%}",
            f"- Qlob-like caption ranking: {report['qlob_caption_ranking_accuracy']:.1%}",
            "- Retrieval top-3 franchise relevance: "
            f"{report['retrieval_top3_franchise_relevance']:.1%}",
            "- Transformed duplicate recall: "
            f"{report['duplicate_detection']['transformed_true_positive_rate']:.1%}",
            "",
            "## Limitations",
            "",
            *[f"- {item}" for item in report["limitations"]],
        ]
        (reports / "profile-evaluation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
