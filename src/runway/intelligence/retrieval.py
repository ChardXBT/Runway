from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast, overload

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, select

from runway.analysis.features import caption_features
from runway.analysis.service import AnalysisService, effective_annotation_fields
from runway.captions.feedback import CaptionFeedbackService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CandidateImage,
    CaptionFeedback,
    FeedbackSignal,
    GeneratedAssetLineage,
    IntelligenceRetrievalRun,
    MediaAsset,
    Post,
    PostAnnotation,
    PostMedia,
    Proposal,
    RepresentationSet,
    RetrievalEvidenceRecord,
    SearchRun,
    StyleProfile,
)
from runway.db.repositories import get_channel
from runway.intelligence.content_modes import ChannelContentModeService
from runway.intelligence.embeddings import (
    ActiveRepresentationResolver,
    ImageEmbeddingProvider,
    MultimodalEmbeddingProvider,
    RepresentationProviderRegistry,
    TextEmbeddingProvider,
    configuration_hash,
    content_hash,
    cosine,
)
from runway.intelligence.fusion import (
    EvidenceCandidate,
    mmr_select,
    reciprocal_rank_fusion,
    role_coverage,
)
from runway.intelligence.policies import ChannelPolicyService, PolicySnapshot


def _required_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{field} must be an integer")
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an integer") from exc


class ReferenceRetrievalQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference_media_ids: list[int] = Field(default_factory=list, max_length=8)
    modification_text: str = Field(default="", max_length=1000)
    desired_entities: list[str] = Field(default_factory=list, max_length=20)
    excluded_entities: list[str] = Field(default_factory=list, max_length=20)
    desired_topic: str | None = Field(default=None, max_length=200)
    desired_action: str | None = Field(default=None, max_length=200)
    desired_scene: str | None = Field(default=None, max_length=200)
    desired_emotion: str | None = Field(default=None, max_length=200)
    desired_composition: str | None = Field(default=None, max_length=200)
    desired_format: str | None = Field(default=None, max_length=200)
    text_overlay_preference: str | None = Field(default=None, max_length=80)
    source_policy: str | None = Field(default=None, max_length=80)
    rights_policy: str | None = Field(default=None, max_length=80)
    aspect_ratio: float | None = Field(default=None, gt=0, le=10)
    post_format: str | None = Field(default=None, max_length=80)


class RetrievalConfiguration(BaseModel):
    version: str = "hybrid-retrieval-2"
    candidate_limit_per_pool: int = Field(default=50, ge=5, le=500)
    selected_limit: int = Field(default=18, ge=4, le=50)
    rank_constant: float = Field(default=40.0, gt=0)
    mmr_lambda: float = Field(default=0.72, ge=0, le=1)
    recency_half_life_days: float = Field(default=45.0, gt=0)
    pool_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "visual": 1.4,
            "multimodal": 1.15,
            "semantic": 1.2,
            "lexical": 0.8,
            "entity": 1.2,
            "topic": 1.0,
            "action": 0.8,
            "emotion": 0.9,
            "scene": 0.8,
            "composition": 0.7,
            "caption_structure": 0.65,
            "representative": 0.8,
            "recent": 0.7,
        }
    )
    role_quotas: dict[str, int] = Field(
        default_factory=lambda: {
            "visual_analogue": 5,
            "semantic_analogue": 5,
            "caption_structure_example": 4,
            "representative_channel_example": 3,
            "recent_exclusion": 4,
        }
    )

    @property
    def hash(self) -> str:
        return configuration_hash(self.model_dump())


class RetrievalService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        registry: RepresentationProviderRegistry | None = None,
        configuration: RetrievalConfiguration | None = None,
    ):
        self.database = database
        self.settings = settings
        if registry is None:
            from runway.intelligence.neural_providers import configured_provider_registry

            registry = configured_provider_registry(settings)
        self.registry = registry
        self.resolver = ActiveRepresentationResolver(database, registry)
        self.store = self.resolver.store
        self.configuration = configuration or RetrievalConfiguration()
        self.policy_service = ChannelPolicyService(database, settings)

    def context_for_candidate(
        self,
        media_asset_id: int,
        *,
        candidate_id: int | None = None,
    ) -> dict[str, object]:
        started = time.perf_counter()
        context = self._load_candidate_context(media_asset_id, candidate_id)
        channel_id = _required_int(context["channel_id"], "channel_id")
        policy = self.policy_service.ensure_defaults(channel_id)
        run_id = self._start_run(
            channel_id=channel_id,
            query_type="candidate_context",
            candidate_id=candidate_id,
            reference_media_ids=[media_asset_id],
            modification_text="",
            constraints={},
            profile_record=cast(StyleProfile, context["profile_record"]),
            policy=policy,
        )
        try:
            pools, post_rows = self._post_pools(context)
            fused = reciprocal_rank_fusion(
                pools,
                weights=self.configuration.pool_weights,
                rank_constant=self.configuration.rank_constant,
            )
            selected = mmr_select(
                fused,
                limit=self.configuration.selected_limit,
                similarity=self._similarity_function(channel_id),
                lambda_relevance=self.configuration.mmr_lambda,
                role_quotas=self.configuration.role_quotas,
            )
            self._persist_evidence(run_id, channel_id, pools, fused)
            result = self._compile_context(
                context=context,
                selected=selected,
                post_rows=post_rows,
                policy=policy,
                retrieval_run_id=run_id,
            )
            self._complete_run(run_id, started)
            return result
        except Exception as exc:
            self._fail_run(run_id, started, exc)
            raise

    def retrieve_reference(
        self,
        query: ReferenceRetrievalQuery,
        *,
        limit: int = 20,
    ) -> dict[str, object]:
        if not query.reference_media_ids and not query.modification_text.strip():
            raise ValueError("reference retrieval requires media or modification text")
        started = time.perf_counter()
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            channel_media_ids = self._channel_media_ids(session, channel.id)
            missing_references = sorted(set(query.reference_media_ids) - channel_media_ids)
            if missing_references:
                raise LookupError(
                    "reference media are missing or outside the configured channel: "
                    + ", ".join(str(value) for value in missing_references)
                )
            profile_record = self._active_profile(session, channel.id)
            candidates = session.scalars(
                select(CandidateImage)
                .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                .where(
                    SearchRun.channel_id == channel.id,
                    CandidateImage.hard_rejection_reason.is_(None),
                )
                .order_by(CandidateImage.id)
            ).all()
            media_by_id = {
                media.id: media
                for media in session.scalars(
                    select(MediaAsset).where(
                        MediaAsset.id.in_(
                            {candidate.media_asset_id for candidate in candidates}
                            | set(query.reference_media_ids)
                        )
                    )
                )
            }
            session.expunge(profile_record)
            for candidate in candidates:
                session.expunge(candidate)
            for stored_media in media_by_id.values():
                session.expunge(stored_media)
        policy = self.policy_service.ensure_defaults(channel.id)
        run_id = self._start_run(
            channel_id=channel.id,
            query_type="reference_image_plus_text",
            candidate_id=None,
            reference_media_ids=query.reference_media_ids,
            modification_text=query.modification_text,
            constraints=query.model_dump(exclude={"reference_media_ids", "modification_text"}),
            profile_record=profile_record,
            policy=policy,
        )
        try:
            reference_vectors = [
                self._media_vector(channel.id, media_by_id[media_id])
                for media_id in query.reference_media_ids
                if media_id in media_by_id
            ]
            reference_vector = self._mean_vector(reference_vectors) if reference_vectors else None
            text_query = self._query_text(query.model_dump())
            text_vector = (
                self._query_text_vector(
                    channel.id,
                    entity_type="retrieval_run",
                    entity_id=run_id,
                    field="instruction",
                    text=text_query,
                    purpose="reference_instruction",
                )
                if text_query
                else None
            )
            pools: dict[str, list[EvidenceCandidate]] = defaultdict(list)
            eligible_candidates: dict[int, CandidateImage] = {}
            for candidate in candidates:
                candidate_media = media_by_id.get(candidate.media_asset_id)
                if candidate_media is None:
                    continue
                rights = self.policy_service.rights_decision(
                    channel_id=channel.id,
                    rights_status=candidate.rights_status,
                )
                analysis = self._json_dict(candidate.detected_topic_json)
                excluded = self._excluded_entity_match(query.excluded_entities, analysis)
                eligibility_reason = (
                    "rights_blocked"
                    if rights.outcome == "blocked"
                    else ("excluded_entity" if excluded else None)
                )
                if eligibility_reason:
                    pools["policy"].append(
                        EvidenceCandidate(
                            entity_type="candidate_image",
                            entity_id=candidate.id,
                            retrieval_channel="policy",
                            raw_score=0.0,
                            policy_score=0.0,
                            exclusion_reason=eligibility_reason,
                            evidence_role="explicit_rule",
                            metadata={"rights": rights.model_dump(), "analysis": analysis},
                        )
                    )
                    continue
                eligible_candidates[candidate.id] = candidate
                image_vector = self._media_vector(channel.id, candidate_media)
                metadata = {
                    "candidate_id": candidate.id,
                    "media_asset_id": candidate_media.id,
                    "caption": "",
                    "analysis": analysis,
                    "rights": rights.model_dump(),
                }
                if reference_vector is not None:
                    pools["reference_visual"].append(
                        EvidenceCandidate(
                            entity_type="candidate_image",
                            entity_id=candidate.id,
                            retrieval_channel="reference_visual",
                            raw_score=max(0.0, cosine(reference_vector, image_vector)),
                            policy_score=1.0 if rights.outcome == "allowed" else 0.5,
                            duplicate_cluster=candidate_media.perceptual_hash,
                            evidence_role="visual_analogue",
                            metadata=metadata,
                        )
                    )
                if text_vector is not None:
                    candidate_text = self._query_text(analysis)
                    if candidate_text:
                        candidate_vector = self._query_text_vector(
                            channel.id,
                            entity_type="candidate_image",
                            entity_id=candidate.id,
                            field="analysis",
                            text=candidate_text,
                            purpose="candidate_semantics",
                        )
                        pools["instruction_text"].append(
                            EvidenceCandidate(
                                entity_type="candidate_image",
                                entity_id=candidate.id,
                                retrieval_channel="instruction_text",
                                raw_score=max(
                                    0.0,
                                    cosine(text_vector, candidate_vector),
                                ),
                                policy_score=(1.0 if rights.outcome == "allowed" else 0.5),
                                duplicate_cluster=candidate_media.perceptual_hash,
                                evidence_role="semantic_analogue",
                                metadata=metadata,
                            )
                        )
                constraint_score = self._constraint_score(
                    query,
                    analysis,
                    candidate_media,
                )
                pools["constraints"].append(
                    EvidenceCandidate(
                        entity_type="candidate_image",
                        entity_id=candidate.id,
                        retrieval_channel="constraints",
                        raw_score=constraint_score,
                        policy_score=1.0 if rights.outcome == "allowed" else 0.5,
                        duplicate_cluster=candidate_media.perceptual_hash,
                        evidence_role="semantic_analogue",
                        metadata=metadata,
                    )
                )
            composed_weights = {
                "reference_visual": 1.3,
                "instruction_text": 1.1,
                "constraints": 1.0,
                "policy": 2.0,
            }
            fused = reciprocal_rank_fusion(
                pools,
                weights=composed_weights,
                rank_constant=self.configuration.rank_constant,
            )
            selected = mmr_select(
                fused,
                limit=min(limit, self.configuration.selected_limit),
                similarity=self._similarity_function(channel.id),
                lambda_relevance=self.configuration.mmr_lambda,
            )
            self._persist_evidence(run_id, channel.id, pools, fused)
            self._complete_run(run_id, started)
            return {
                "retrieval_run_id": run_id,
                "query": query.model_dump(),
                "configuration_hash": self.configuration.hash,
                "policy_version": policy.version,
                "results": [
                    {
                        "candidate_id": row.entity_id,
                        "rank": row.selected_rank,
                        "fusion_score": round(row.fusion_score, 8),
                        "policy_score": row.policy_score,
                        "diversity_penalty": round(row.diversity_penalty, 8),
                        "matched_channels": row.metadata.get(
                            "matched_retrieval_channels",
                            [],
                        ),
                        "analysis": row.metadata.get("analysis", {}),
                        "rights": row.metadata.get("rights", {}),
                    }
                    for row in selected
                    if row.entity_id in eligible_candidates
                ],
            }
        except Exception as exc:
            self._fail_run(run_id, started, exc)
            raise

    def inspect_run(self, retrieval_run_id: int) -> dict[str, object]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            run = session.get(IntelligenceRetrievalRun, retrieval_run_id)
            if run is None or run.channel_id != channel.id:
                raise LookupError(f"retrieval run {retrieval_run_id} not found")
            evidence = session.scalars(
                select(RetrievalEvidenceRecord)
                .where(RetrievalEvidenceRecord.retrieval_run_id == run.id)
                .order_by(
                    RetrievalEvidenceRecord.selected.desc(),
                    RetrievalEvidenceRecord.selected_rank,
                    RetrievalEvidenceRecord.retrieval_channel,
                    RetrievalEvidenceRecord.entity_id,
                )
            ).all()
            return {
                "id": run.id,
                "channel_id": run.channel_id,
                "query_identity": run.query_identity,
                "query_type": run.query_type,
                "candidate_image_id": run.candidate_image_id,
                "reference_media_ids": json.loads(run.reference_media_ids_json),
                "modification_text": run.modification_text,
                "constraints": json.loads(run.structured_constraints_json),
                "profile_version": run.profile_version,
                "policy_version": run.policy_version,
                "configuration": json.loads(run.retrieval_configuration_json),
                "configuration_hash": run.configuration_hash,
                "embedding_versions": json.loads(run.embedding_versions_json),
                "representation_sets": json.loads(run.representation_sets_json),
                "cache_diagnostics": json.loads(run.cache_diagnostics_json),
                "status": run.status,
                "latency_ms": run.latency_ms,
                "error_summary": run.error_summary,
                "evidence": [
                    {
                        "entity_type": row.entity_type,
                        "entity_id": row.entity_id,
                        "retrieval_channel": row.retrieval_channel,
                        "raw_score": row.raw_score,
                        "normalized_score": row.normalized_score,
                        "fusion_score": row.fusion_score,
                        "recency_score": row.recency_score,
                        "policy_score": row.policy_score,
                        "diversity_penalty": row.diversity_penalty,
                        "duplicate_cluster": row.duplicate_cluster,
                        "exclusion_reason": row.exclusion_reason,
                        "selected": row.selected,
                        "selected_rank": row.selected_rank,
                        "evidence_role": row.evidence_role,
                        "metadata": json.loads(row.metadata_json),
                    }
                    for row in evidence
                ],
            }

    def _load_candidate_context(
        self,
        media_asset_id: int,
        candidate_id: int | None,
    ) -> dict[str, object]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            media = session.get(MediaAsset, media_asset_id)
            if media is None:
                raise LookupError(f"candidate media {media_asset_id} was not found")
            if media_asset_id not in self._channel_media_ids(session, channel.id):
                raise LookupError(f"candidate media {media_asset_id} is outside @{channel.handle}")
            candidate: CandidateImage | None = None
            if candidate_id is not None:
                candidate = session.get(CandidateImage, candidate_id)
                if candidate is None or candidate.media_asset_id != media_asset_id:
                    raise LookupError(f"candidate {candidate_id} does not match media")
                search_run = session.get(SearchRun, candidate.search_run_id)
                if search_run is None or search_run.channel_id != channel.id:
                    raise LookupError(f"candidate {candidate_id} is outside @{channel.handle}")
            profile_record = self._active_profile(session, channel.id)
            profile = self._json_dict(profile_record.profile_json)
            training_ids = [
                _required_int(value, "training_post_id")
                for value in cast(list[object], profile.get("training_post_ids", []))
            ]
            posts = session.scalars(
                select(Post)
                .where(
                    Post.channel_id == channel.id,
                    Post.id.in_(training_ids),
                    Post.is_training_eligible.is_(True),
                )
                .order_by(Post.id)
            ).all()
            media_rows = session.execute(
                select(PostMedia, MediaAsset)
                .join(MediaAsset, MediaAsset.id == PostMedia.media_asset_id)
                .where(PostMedia.post_id.in_([post.id for post in posts]))
                .order_by(PostMedia.post_id, PostMedia.position)
            ).all()
            annotations = self._annotation_map(session, [post.id for post in posts])
            context = {
                "channel_id": channel.id,
                "channel_handle": channel.handle,
                "candidate": candidate,
                "candidate_media": media,
                "candidate_analysis": (
                    self._json_dict(candidate.detected_topic_json) if candidate else {}
                ),
                "profile_record": profile_record,
                "profile": profile,
                "posts": posts,
                "post_media": list(media_rows),
                "annotations": annotations,
            }
            session.expunge(media)
            session.expunge(profile_record)
            if candidate is not None:
                session.expunge(candidate)
            for post in posts:
                session.expunge(post)
            for _link, asset in media_rows:
                if asset in session:
                    session.expunge(asset)
            for annotation in annotations.values():
                session.expunge(annotation)
            return context

    @staticmethod
    def _channel_media_ids(session: Any, channel_id: int) -> set[int]:
        historical = set(
            session.scalars(
                select(PostMedia.media_asset_id)
                .join(Post, Post.id == PostMedia.post_id)
                .where(Post.channel_id == channel_id)
            )
        )
        candidates = set(
            session.scalars(
                select(CandidateImage.media_asset_id)
                .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                .where(SearchRun.channel_id == channel_id)
            )
        )
        generated = set(
            session.scalars(
                select(GeneratedAssetLineage.media_asset_id).where(
                    GeneratedAssetLineage.channel_id == channel_id
                )
            )
        )
        return historical | candidates | generated

    def _post_pools(
        self,
        context: dict[str, object],
    ) -> tuple[dict[str, list[EvidenceCandidate]], dict[int, dict[str, object]]]:
        channel_id = _required_int(context["channel_id"], "channel_id")
        candidate_media = cast(MediaAsset, context["candidate_media"])
        query_analysis = cast(dict[str, Any], context["candidate_analysis"])
        profile = cast(dict[str, Any], context["profile"])
        posts = cast(list[Post], context["posts"])
        annotations = cast(dict[int, PostAnnotation], context["annotations"])
        media_rows = cast(list[tuple[PostMedia, MediaAsset]], context["post_media"])
        media_by_post: defaultdict[int, list[MediaAsset]] = defaultdict(list)
        for link, media in media_rows:
            media_by_post[link.post_id].append(media)

        query_visual = self._media_vector(channel_id, candidate_media)
        query_text = self._query_text(query_analysis)
        candidate = cast(CandidateImage | None, context["candidate"])
        query_entity_type = "candidate_image" if candidate is not None else "media_asset"
        query_entity_id = candidate.id if candidate is not None else candidate_media.id
        query_semantic = (
            self._query_text_vector(
                channel_id,
                entity_type=query_entity_type,
                entity_id=query_entity_id,
                field="analysis",
                text=query_text,
            )
            if query_text
            else None
        )
        query_multimodal = self._query_multimodal_vector(
            channel_id,
            media=candidate_media,
            text=query_text,
            entity_type=query_entity_type,
            entity_id=query_entity_id,
        )
        if candidate is not None:
            self._query_text_vector(
                channel_id,
                entity_type="candidate_image",
                entity_id=candidate.id,
                field="editorial_angle",
                text=query_text,
                purpose="editorial_angle_semantics",
            )

        query_tokens = self._tokens(query_text)
        query_entities = self._entity_keys(query_analysis)
        query_topic = self._normalized(str(query_analysis.get("franchise") or ""))
        query_action = {
            self._normalized(str(value))
            for value in cast(list[object], query_analysis.get("actions", []))
            if str(value).strip()
        }
        query_emotion = self._normalized(str(query_analysis.get("emotion") or ""))
        query_scene = self._normalized(str(query_analysis.get("scene_archetype") or ""))
        query_composition = self._normalized(str(query_analysis.get("composition") or ""))
        representative_ids = {
            _required_int(value, "representative_post_id")
            for value in cast(
                list[object],
                profile.get("representative_post_ids", []),
            )
        }
        now = datetime.now(UTC)
        pools: dict[str, list[EvidenceCandidate]] = defaultdict(list)
        post_rows: dict[int, dict[str, object]] = {}
        for post in posts:
            annotation = annotations.get(post.id)
            effective = effective_annotation_fields(annotation) if annotation is not None else {}
            post_text = self._post_text(post, effective)
            text_vector = self._historical_text_vector(
                channel_id,
                post_id=post.id,
                caption=post.caption or "",
            )
            semantic = (
                max(0.0, cosine(query_semantic, text_vector)) if query_semantic is not None else 0.0
            )
            post_tokens = self._tokens(post_text)
            lexical = self._jaccard(query_tokens, post_tokens)
            post_entities = self._entity_keys(effective)
            entity_score = self._jaccard(query_entities, post_entities)
            post_topic = self._normalized(str(effective.get("franchise") or ""))
            topic_score = float(bool(query_topic and query_topic == post_topic))
            post_actions = {
                self._normalized(str(value))
                for value in cast(list[object], effective.get("actions", []))
                if str(value).strip()
            }
            action_score = self._jaccard(query_action, post_actions)
            emotion_score = self._text_field_score(
                query_emotion,
                str(effective.get("emotion") or ""),
            )
            scene_score = self._text_field_score(
                query_scene,
                str(effective.get("scene_description") or ""),
            )
            composition_score = self._text_field_score(
                query_composition,
                str(effective.get("composition") or ""),
            )
            query_structure = str(query_analysis.get("preferred_caption_structure") or "")
            historical_structure = self._caption_structure(post.caption or "")
            structure_score = float(
                bool(query_structure and query_structure == historical_structure)
            )
            historical_vectors = [
                self._media_vector(channel_id, media, historical=True)
                for media in media_by_post.get(post.id, [])
            ]
            visual_scores = [
                max(0.0, cosine(query_visual, vector)) for vector in historical_vectors
            ]
            visual = (
                0.7 * max(visual_scores) + 0.3 * sum(visual_scores) / len(visual_scores)
                if visual_scores
                else 0.0
            )
            primary_media = media_by_post[post.id][0] if media_by_post.get(post.id) else None
            multimodal = (
                max(
                    0.0,
                    cosine(
                        query_multimodal,
                        self._historical_multimodal_vector(
                            channel_id,
                            post=post,
                            media=primary_media,
                        ),
                    ),
                )
                if primary_media is not None
                else 0.0
            )
            recency = self._recency_score(post, now)
            media_ids = [media.id for media in media_by_post.get(post.id, [])]
            cluster = (
                media_by_post[post.id][0].perceptual_hash
                if media_by_post.get(post.id)
                else f"post:{post.id}"
            )
            base_metadata: dict[str, object] = {
                "post_id": post.id,
                "caption": post.caption,
                "published_at": post.published_at.isoformat() if post.published_at else None,
                "date_precision": post.date_precision,
                "media_asset_ids": media_ids,
                "media_urls": [
                    f"/media/{Path(media.local_path).relative_to('media').as_posix()}"
                    for media in media_by_post.get(post.id, [])
                ],
                "annotation": effective,
                "caption_structure": historical_structure,
            }
            post_rows[post.id] = base_metadata
            scores = {
                "visual": visual,
                "multimodal": multimodal,
                "semantic": semantic,
                "lexical": lexical,
                "entity": entity_score,
                "topic": topic_score,
                "action": action_score,
                "emotion": emotion_score,
                "scene": scene_score,
                "composition": composition_score,
                "caption_structure": structure_score,
            }
            role_map = {
                "visual": "visual_analogue",
                "multimodal": "semantic_analogue",
                "semantic": "semantic_analogue",
                "lexical": "caption_structure_example",
                "entity": "semantic_analogue",
                "topic": "semantic_analogue",
                "action": "semantic_analogue",
                "emotion": "visual_analogue",
                "scene": "visual_analogue",
                "composition": "visual_analogue",
                "caption_structure": "caption_structure_example",
            }
            for pool_name, score in scores.items():
                if score <= 0 and pool_name not in {"visual", "multimodal", "semantic"}:
                    continue
                pools[pool_name].append(
                    EvidenceCandidate(
                        entity_type="post",
                        entity_id=post.id,
                        retrieval_channel=pool_name,
                        raw_score=float(score),
                        recency_score=recency,
                        policy_score=0.5,
                        duplicate_cluster=cluster,
                        evidence_role=role_map[pool_name],
                        metadata=base_metadata,
                    )
                )
            if post.id in representative_ids:
                pools["representative"].append(
                    EvidenceCandidate(
                        entity_type="post",
                        entity_id=post.id,
                        retrieval_channel="representative",
                        raw_score=1.0,
                        recency_score=recency,
                        policy_score=0.7,
                        duplicate_cluster=cluster,
                        evidence_role="representative_channel_example",
                        metadata=base_metadata,
                    )
                )
            if self._is_recent(post, now):
                pools["recent"].append(
                    EvidenceCandidate(
                        entity_type="post",
                        entity_id=post.id,
                        retrieval_channel="recent",
                        raw_score=max(recency, 0.01),
                        recency_score=recency,
                        policy_score=1.0,
                        duplicate_cluster=cluster,
                        evidence_role="recent_exclusion",
                        metadata=base_metadata,
                    )
                )
        for rows in pools.values():
            rows.sort(key=lambda row: (-row.raw_score, row.entity_id))
            del rows[self.configuration.candidate_limit_per_pool :]
        return dict(pools), post_rows

    def _compile_context(
        self,
        *,
        context: dict[str, object],
        selected: list[EvidenceCandidate],
        post_rows: dict[int, dict[str, object]],
        policy: PolicySnapshot,
        retrieval_run_id: int,
    ) -> dict[str, object]:
        channel_id = _required_int(context["channel_id"], "channel_id")
        profile_record = cast(StyleProfile, context["profile_record"])
        profile = cast(dict[str, Any], context["profile"])
        candidate = cast(CandidateImage | None, context["candidate"])
        selected_posts = [
            row for row in selected if row.entity_type == "post" and row.entity_id in post_rows
        ]
        visual_rows = [
            row
            for row in selected_posts
            if row.evidence_role in {"visual_analogue", "recent_exclusion"}
        ][:8]
        if not visual_rows:
            visual_rows = selected_posts[:8]
        visual_examples = [
            {
                "post_id": row.entity_id,
                "caption": post_rows[row.entity_id].get("caption"),
                "published_at": post_rows[row.entity_id].get("published_at"),
                "visual_similarity": round(row.raw_score, 6),
                "media_asset_id": cast(
                    list[int],
                    post_rows[row.entity_id].get("media_asset_ids", []),
                )[0]
                if post_rows[row.entity_id].get("media_asset_ids")
                else None,
                "media_asset_ids": post_rows[row.entity_id].get("media_asset_ids", []),
                "media_url": cast(
                    list[str],
                    post_rows[row.entity_id].get("media_urls", []),
                )[0]
                if post_rows[row.entity_id].get("media_urls")
                else None,
                "media_urls": post_rows[row.entity_id].get("media_urls", []),
                "evidence_role": row.evidence_role,
                "fusion_score": round(row.fusion_score, 8),
            }
            for row in visual_rows
        ]
        caption_examples = [
            {
                "post_id": row.entity_id,
                "caption": post_rows[row.entity_id].get("caption"),
                "evidence": row.evidence_role,
                "fusion_score": round(row.fusion_score, 8),
                "caption_structure": post_rows[row.entity_id].get("caption_structure"),
            }
            for row in selected_posts[:8]
        ]
        now = datetime.now(UTC)
        recent_exclusions = [
            {
                "post_id": row.entity_id,
                "media_asset_ids": post_rows[row.entity_id].get("media_asset_ids", []),
                "published_at": post_rows[row.entity_id].get("published_at"),
                "warning": (
                    "date precision cannot prove the duplicate-window boundary"
                    if post_rows[row.entity_id].get("date_precision") in {"relative", "unknown"}
                    else None
                ),
            }
            for row in selected_posts
            if row.evidence_role == "recent_exclusion"
            or self._metadata_recent(post_rows[row.entity_id], now)
        ]
        negative_examples = self._negative_examples(channel_id)
        feedback_context = (
            CaptionFeedbackService(self.database, self.settings).context_for_candidate(candidate.id)
            if candidate is not None
            else {
                "editorial_policy": {
                    "preferred_structures": policy.preferred_structures,
                    "question_first": policy.question_first,
                },
                "positive_examples": [],
                "negative_examples": [],
                "learned_preferences": {},
            }
        )
        recent_posts = sorted(
            post_rows.values(),
            key=lambda row: (
                str(row.get("published_at") or ""),
                _required_int(row["post_id"], "post_id"),
            ),
            reverse=True,
        )
        with self.database.session() as session:
            scheduled = session.scalars(
                select(Proposal)
                .where(
                    Proposal.channel_id == channel_id,
                    Proposal.status.in_(
                        [
                            "approved",
                            "internally_scheduled",
                            "publishing",
                            "externally_scheduled",
                            "publish_unverified",
                        ]
                    ),
                )
                .order_by(desc(Proposal.scheduled_publish_at), desc(Proposal.id))
                .limit(30)
            ).all()
        rotation = {
            "last_3_post_ids": [
                _required_int(row["post_id"], "post_id") for row in recent_posts[:3]
            ],
            "last_10_post_ids": [
                _required_int(row["post_id"], "post_id") for row in recent_posts[:10]
            ],
            "last_30_post_ids": [
                _required_int(row["post_id"], "post_id") for row in recent_posts[:30]
            ],
            "scheduled_proposal_ids": [proposal.id for proposal in scheduled],
            "recent_rejection_ids": [
                _required_int(item["proposal_id"], "proposal_id")
                for item in negative_examples
                if item.get("proposal_id") is not None
            ],
        }
        explicit_rules = [
            {
                "rule_id": row["id"],
                "type": row["type"],
                "priority": row["priority"],
                "value": row["value"],
                "text": row["text"],
            }
            for row in policy.rules
        ]
        content_modes = cast(dict[str, object], profile.get("content_modes", {}))
        candidate_mode_scores = ChannelContentModeService(
            self.database,
            self.resolver,
        ).score_candidate(
            channel_id=channel_id,
            candidate_description=json.dumps(
                context["candidate_analysis"],
                sort_keys=True,
                default=str,
            ),
            content_modes=content_modes,
        )
        evidence_brief = {
            "explicit_rules": explicit_rules,
            "visual_facts": cast(dict[str, object], context["candidate_analysis"]),
            "uncertainty": cast(dict[str, object], context["candidate_analysis"]).get(
                "field_confidence",
                {},
            ),
            "positive_evidence": caption_examples,
            "negative_evidence": negative_examples,
            "rotation_constraints": rotation,
            "content_mode_scores": candidate_mode_scores,
            "output_requirements": {
                "language": policy.language,
                "locale": policy.locale,
                "preferred_structures": policy.preferred_structures,
            },
        }
        return {
            "retrieval_run_id": retrieval_run_id,
            "query_identity": self._query_identity(
                channel_id,
                candidate.id if candidate else None,
                [cast(MediaAsset, context["candidate_media"]).id],
                "",
                {},
            ),
            "visual_examples": visual_examples,
            "caption_style_examples": caption_examples,
            "recent_180_day_exclusions": recent_exclusions,
            "rotation_state": rotation,
            "negative_examples": negative_examples,
            "explicit_rules": explicit_rules,
            "evidence_brief": evidence_brief,
            "evidence_role_coverage": role_coverage(selected),
            "style_profile": {
                "summary": profile.get("summary", ""),
                "caption_statistics": profile.get("caption_statistics", {}),
                "dominant_caption_structures": profile.get(
                    "dominant_caption_structures",
                    [],
                ),
                "long_term_channel_dna": profile.get("long_term_channel_dna", {}),
                "recent_editorial_mode": profile.get("recent_editorial_mode", {}),
                "content_modes": content_modes,
                "candidate_mode_scores": candidate_mode_scores,
                "learned_creator_preference": profile.get(
                    "learned_creator_preference",
                    {},
                ),
                "recent_overuse_rules": profile.get("recent_overuse_rules", {}),
            },
            "feedback_context": feedback_context,
            "style_profile_version": profile_record.version,
            "policy": policy.model_dump(),
            "retrieval_configuration": self.configuration.model_dump(),
            "retrieval_configuration_hash": self.configuration.hash,
        }

    def _negative_examples(self, channel_id: int) -> list[dict[str, object]]:
        with self.database.session() as session:
            legacy = session.scalars(
                select(CaptionFeedback)
                .join(Proposal, Proposal.id == CaptionFeedback.proposal_id)
                .where(
                    Proposal.channel_id == channel_id,
                    CaptionFeedback.verdict == "rejected",
                )
                .order_by(desc(CaptionFeedback.created_at), desc(CaptionFeedback.id))
                .limit(8)
            ).all()
            normalized = session.scalars(
                select(FeedbackSignal)
                .where(
                    FeedbackSignal.channel_id == channel_id,
                    FeedbackSignal.target.in_(["caption", "pairing"]),
                    FeedbackSignal.verdict == "rejected",
                )
                .order_by(desc(FeedbackSignal.created_at), desc(FeedbackSignal.id))
                .limit(8)
            ).all()
        rows = [
            {
                "feedback_id": row.id,
                "proposal_id": row.proposal_id,
                "caption": row.generated_caption,
                "reason_codes": json.loads(row.reason_codes_json),
                "note": row.note,
                "evidence_role": "negative_example",
            }
            for row in legacy
        ]
        rows.extend(
            {
                "feedback_signal_id": row.id,
                "proposal_id": row.proposal_id,
                "caption": row.value_text,
                "reason_codes": json.loads(row.reason_codes_json),
                "note": row.note,
                "evidence_role": "negative_example",
            }
            for row in normalized
        )
        deduplicated: list[dict[str, object]] = []
        seen: set[str] = set()
        for row in rows:
            key = self._normalized(str(row.get("caption") or ""))
            if not key or key in seen:
                continue
            seen.add(key)
            deduplicated.append(row)
        return deduplicated[:8]

    def _start_run(
        self,
        *,
        channel_id: int,
        query_type: str,
        candidate_id: int | None,
        reference_media_ids: list[int],
        modification_text: str,
        constraints: dict[str, object],
        profile_record: StyleProfile,
        policy: PolicySnapshot,
    ) -> int:
        identity = self._query_identity(
            channel_id,
            candidate_id,
            reference_media_ids,
            modification_text,
            constraints,
        )
        with self.database.session() as session:
            run = IntelligenceRetrievalRun(
                channel_id=channel_id,
                query_identity=identity,
                query_type=query_type,
                candidate_image_id=candidate_id,
                reference_media_ids_json=json.dumps(reference_media_ids),
                modification_text=modification_text or None,
                structured_constraints_json=json.dumps(constraints, sort_keys=True),
                style_profile_id=profile_record.id,
                profile_version=profile_record.version,
                policy_version=policy.version,
                retrieval_configuration_json=self.configuration.model_dump_json(),
                configuration_hash=self.configuration.hash,
                embedding_versions_json=json.dumps(
                    self._embedding_versions(channel_id),
                    sort_keys=True,
                ),
                representation_sets_json=json.dumps(
                    self._active_set_snapshot(channel_id),
                    sort_keys=True,
                ),
                cache_diagnostics_json=json.dumps(self.store.diagnostics(), sort_keys=True),
                status="running",
            )
            session.add(run)
            session.flush()
            self.store.reset_diagnostics()
            return run.id

    def _complete_run(self, run_id: int, started: float) -> None:
        with self.database.session() as session:
            run = session.get(IntelligenceRetrievalRun, run_id)
            if run is None:
                raise LookupError(f"retrieval run {run_id} disappeared")
            starting_sets = self._json_dict(run.representation_sets_json)
            finishing_sets = self._active_set_snapshot(run.channel_id)
            if starting_sets != finishing_sets:
                raise RuntimeError(
                    "active representation sets changed during retrieval; "
                    "the run cannot be treated as canonical"
                )
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
            run.latency_ms = round((time.perf_counter() - started) * 1000, 3)
            run.cache_diagnostics_json = json.dumps(
                self.store.diagnostics(),
                sort_keys=True,
            )

    def _fail_run(self, run_id: int, started: float, exc: Exception) -> None:
        with self.database.session() as session:
            run = session.get(IntelligenceRetrievalRun, run_id)
            if run is None:
                return
            run.status = "failed"
            run.completed_at = datetime.now(UTC)
            run.latency_ms = round((time.perf_counter() - started) * 1000, 3)
            run.error_summary = f"{type(exc).__name__}: {exc}"
            run.cache_diagnostics_json = json.dumps(
                self.store.diagnostics(),
                sort_keys=True,
            )

    def _persist_evidence(
        self,
        run_id: int,
        channel_id: int,
        pools: dict[str, list[EvidenceCandidate]],
        fused: list[EvidenceCandidate],
    ) -> None:
        aggregate = {row.key: row for row in fused}
        records: list[RetrievalEvidenceRecord] = []
        for pool_name, rows in sorted(pools.items()):
            for row in rows:
                fused_row = aggregate.get(row.key)
                records.append(
                    RetrievalEvidenceRecord(
                        channel_id=channel_id,
                        retrieval_run_id=run_id,
                        entity_type=row.entity_type,
                        entity_id=row.entity_id,
                        retrieval_channel=pool_name,
                        raw_score=row.raw_score,
                        normalized_score=row.normalized_score,
                        fusion_score=fused_row.fusion_score if fused_row else 0.0,
                        recency_score=row.recency_score,
                        policy_score=row.policy_score,
                        diversity_penalty=0.0,
                        duplicate_cluster=row.duplicate_cluster,
                        exclusion_reason=row.exclusion_reason,
                        selected=False,
                        selected_rank=None,
                        evidence_role=row.evidence_role,
                        metadata_json=json.dumps(row.metadata, sort_keys=True, default=str),
                    )
                )
        records.extend(
            RetrievalEvidenceRecord(
                channel_id=channel_id,
                retrieval_run_id=run_id,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                retrieval_channel="fusion",
                raw_score=max(
                    (
                        candidate.raw_score
                        for candidates in pools.values()
                        for candidate in candidates
                        if candidate.key == row.key
                    ),
                    default=0.0,
                ),
                normalized_score=max(
                    (
                        candidate.normalized_score
                        for candidates in pools.values()
                        for candidate in candidates
                        if candidate.key == row.key
                    ),
                    default=0.0,
                ),
                fusion_score=row.fusion_score,
                recency_score=row.recency_score,
                policy_score=row.policy_score,
                diversity_penalty=row.diversity_penalty,
                duplicate_cluster=row.duplicate_cluster,
                exclusion_reason=row.exclusion_reason,
                selected=row.selected,
                selected_rank=row.selected_rank,
                evidence_role=row.evidence_role,
                metadata_json=json.dumps(row.metadata, sort_keys=True, default=str),
            )
            for row in fused
        )
        with self.database.session() as session:
            session.add_all(records)

    def _media_vector(
        self,
        channel_id: int,
        media: MediaAsset,
        *,
        historical: bool = False,
    ) -> np.ndarray:
        purpose = "historical_visual_semantics" if historical else "query_visual_semantics"
        provider, active_set = self._resolve_provider(
            channel_id,
            modality="image",
            scope="historical_image",
            purpose="historical_visual_semantics",
        )
        record = self.store.get_or_create(
            channel_id=channel_id,
            entity_type="media_asset",
            entity_id=media.id,
            field="image",
            modality="image",
            purpose=purpose,
            provider=provider.name,
            model=provider.model,
            model_version=provider.version,
            source_content_hash=media.sha256,
            configuration_hash=provider.configuration_fingerprint,
            producer=lambda: provider.embed_image(
                self._media_path(media),
                purpose=purpose,
            ),
            metadata={"mime_type": media.mime_type},
            active_scope=("historical_image" if historical and active_set is not None else None),
        )
        return cast(np.ndarray, self.store.vectors(record)[0])

    def _historical_text_vector(
        self,
        channel_id: int,
        *,
        post_id: int,
        caption: str,
    ) -> np.ndarray:
        purpose = "historical_caption_semantics"
        provider, active_set = self._resolve_provider(
            channel_id,
            modality="text",
            scope="historical_text",
            purpose=purpose,
        )
        record = self.store.get_or_create(
            channel_id=channel_id,
            entity_type="post",
            entity_id=post_id,
            field="caption",
            modality="text",
            purpose=purpose,
            provider=provider.name,
            model=provider.model,
            model_version=provider.version,
            source_content_hash=content_hash(caption),
            configuration_hash=provider.configuration_fingerprint,
            producer=lambda: provider.embed_text(caption, purpose=purpose),
            metadata={"caption": caption},
            active_scope="historical_text" if active_set is not None else None,
        )
        return cast(np.ndarray, self.store.vectors(record)[0])

    def _query_text_vector(
        self,
        channel_id: int,
        *,
        entity_type: str,
        entity_id: int,
        field: str,
        text: str,
        purpose: str = "query_text_semantics",
    ) -> np.ndarray:
        provider, _active_set = self._resolve_provider(
            channel_id,
            modality="text",
            scope="historical_text",
            purpose="historical_caption_semantics",
        )
        record = self.store.get_or_create(
            channel_id=channel_id,
            entity_type=entity_type,
            entity_id=entity_id,
            field=field,
            modality="text",
            purpose=purpose,
            provider=provider.name,
            model=provider.model,
            model_version=provider.version,
            source_content_hash=content_hash(text),
            configuration_hash=provider.configuration_fingerprint,
            producer=lambda: provider.embed_text(text, purpose=purpose),
            metadata={"text": text},
        )
        return cast(np.ndarray, self.store.vectors(record)[0])

    def _historical_multimodal_vector(
        self,
        channel_id: int,
        *,
        post: Post,
        media: MediaAsset,
    ) -> np.ndarray:
        purpose = "historical_pair_semantics"
        provider, active_set = self._resolve_provider(
            channel_id,
            modality="multimodal",
            scope="historical_multimodal",
            purpose=purpose,
        )
        caption = post.caption or ""
        source_hash = configuration_hash({"media_sha256": media.sha256, "text": caption})
        record = self.store.get_or_create(
            channel_id=channel_id,
            entity_type="post",
            entity_id=post.id,
            field="image_caption",
            modality="multimodal",
            purpose=purpose,
            provider=provider.name,
            model=provider.model,
            model_version=provider.version,
            source_content_hash=source_hash,
            configuration_hash=provider.configuration_fingerprint,
            producer=lambda: provider.embed_image_text(
                self._media_path(media),
                caption,
                purpose=purpose,
            ),
            metadata={"media_asset_id": media.id, "caption": caption},
            active_scope=("historical_multimodal" if active_set is not None else None),
        )
        return cast(np.ndarray, self.store.vectors(record)[0])

    def _query_multimodal_vector(
        self,
        channel_id: int,
        *,
        media: MediaAsset,
        text: str,
        entity_type: str,
        entity_id: int,
    ) -> np.ndarray:
        purpose = "query_pair_semantics"
        provider, _active_set = self._resolve_provider(
            channel_id,
            modality="multimodal",
            scope="historical_multimodal",
            purpose="historical_pair_semantics",
        )
        source_hash = configuration_hash({"media_sha256": media.sha256, "text": text})
        record = self.store.get_or_create(
            channel_id=channel_id,
            entity_type=entity_type,
            entity_id=entity_id,
            field="image_analysis",
            modality="multimodal",
            purpose=purpose,
            provider=provider.name,
            model=provider.model,
            model_version=provider.version,
            source_content_hash=source_hash,
            configuration_hash=provider.configuration_fingerprint,
            producer=lambda: provider.embed_image_text(
                self._media_path(media),
                text,
                purpose=purpose,
            ),
            metadata={"media_asset_id": media.id, "text": text},
        )
        return cast(np.ndarray, self.store.vectors(record)[0])

    @overload
    def _resolve_provider(
        self,
        channel_id: int,
        *,
        modality: Literal["text"],
        scope: str,
        purpose: str,
    ) -> tuple[TextEmbeddingProvider, RepresentationSet | None]: ...

    @overload
    def _resolve_provider(
        self,
        channel_id: int,
        *,
        modality: Literal["image"],
        scope: str,
        purpose: str,
    ) -> tuple[ImageEmbeddingProvider, RepresentationSet | None]: ...

    @overload
    def _resolve_provider(
        self,
        channel_id: int,
        *,
        modality: Literal["multimodal"],
        scope: str,
        purpose: str,
    ) -> tuple[MultimodalEmbeddingProvider, RepresentationSet | None]: ...

    def _resolve_provider(
        self,
        channel_id: int,
        *,
        modality: Literal["text", "image", "multimodal"],
        scope: str,
        purpose: str,
    ) -> tuple[
        TextEmbeddingProvider | ImageEmbeddingProvider | MultimodalEmbeddingProvider,
        RepresentationSet | None,
    ]:
        provider, resolution = self.resolver.resolve(
            channel_id,
            modality=modality,
            scope=scope,
            purpose=purpose,
        )
        active_set = (
            self.store.active_set(channel_id=channel_id, scope=scope, purpose=purpose)
            if resolution.representation_set_id is not None
            else None
        )
        return provider, active_set

    def _active_set_snapshot(self, channel_id: int) -> dict[str, object]:
        with self.database.session() as session:
            rows = session.scalars(
                select(RepresentationSet)
                .where(
                    RepresentationSet.channel_id == channel_id,
                    RepresentationSet.active.is_(True),
                    RepresentationSet.status == "active",
                )
                .order_by(
                    RepresentationSet.scope,
                    RepresentationSet.purpose,
                    RepresentationSet.id,
                )
            ).all()
        result: dict[str, object] = {}
        for row in rows:
            key = f"{row.scope}:{row.purpose}"
            if key in result:
                raise RuntimeError(
                    f"multiple active representation sets violate canonical resolution for {key}"
                )
            result[key] = {
                "id": row.id,
                "provider": row.provider,
                "model": row.model,
                "version": row.model_version,
                "configuration_hash": row.configuration_hash,
                "plan_hash": row.plan_hash,
            }
        return result

    def _embedding_versions(self, channel_id: int) -> dict[str, object]:
        result: dict[str, object] = {}
        for modality, scope, purpose in (
            ("text", "historical_text", "historical_caption_semantics"),
            ("image", "historical_image", "historical_visual_semantics"),
            ("multimodal", "historical_multimodal", "historical_pair_semantics"),
        ):
            provider, active_set = self._resolve_provider(
                channel_id,
                modality=cast(Literal["text", "image", "multimodal"], modality),
                scope=scope,
                purpose=purpose,
            )
            result[modality] = {
                "provider": provider.name,
                "model": provider.model,
                "version": provider.version,
                "configuration_hash": provider.configuration_fingerprint,
                "representation_set_id": (active_set.id if active_set is not None else None),
                "resolution": "active_set" if active_set is not None else "baseline",
            }
        return result

    def _media_path(self, media: MediaAsset) -> Path:
        raw = Path(media.local_path)
        resolved = (raw if raw.is_absolute() else self.settings.resolved_data_dir / raw).resolve()
        root = self.settings.resolved_data_dir.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"media asset {media.id} escapes the configured data root")
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return resolved

    def _active_profile(self, session: Any, channel_id: int) -> StyleProfile:
        record = session.scalar(
            select(StyleProfile)
            .where(
                StyleProfile.channel_id == channel_id,
                StyleProfile.is_active.is_(True),
            )
            .order_by(desc(StyleProfile.version))
            .limit(1)
        )
        if record is None:
            raise LookupError("build a channel profile before retrieval")
        return cast(StyleProfile, record)

    @staticmethod
    def _annotation_map(session: Any, post_ids: list[int]) -> dict[int, PostAnnotation]:
        priority = {
            version: index
            for index, version in enumerate(
                reversed(AnalysisService.compatible_annotation_versions)
            )
        }
        result: dict[int, PostAnnotation] = {}
        for annotation in session.scalars(
            select(PostAnnotation).where(
                PostAnnotation.post_id.in_(post_ids),
                PostAnnotation.annotation_version.in_(
                    AnalysisService.compatible_annotation_versions
                ),
            )
        ):
            current = result.get(annotation.post_id)
            if current is None or priority.get(
                annotation.annotation_version,
                -1,
            ) > priority.get(current.annotation_version, -1):
                result[annotation.post_id] = annotation
        return result

    @staticmethod
    def _query_identity(
        channel_id: int,
        candidate_id: int | None,
        reference_media_ids: list[int],
        modification_text: str,
        constraints: dict[str, object],
    ) -> str:
        return configuration_hash(
            {
                "channel_id": channel_id,
                "candidate_id": candidate_id,
                "reference_media_ids": reference_media_ids,
                "modification_text": modification_text,
                "constraints": constraints,
            }
        )

    @staticmethod
    def _json_dict(value: str) -> dict[str, Any]:
        try:
            result = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return result if isinstance(result, dict) else {}

    @classmethod
    def _query_text(cls, value: dict[str, object]) -> str:
        parts: list[str] = []
        for key in (
            "franchise",
            "desired_topic",
            "scene_archetype",
            "desired_scene",
            "composition",
            "desired_composition",
            "emotion",
            "desired_emotion",
            "setting",
            "modification_text",
            "desired_action",
            "desired_format",
        ):
            item = value.get(key)
            if item:
                parts.append(str(item))
        for key in (
            "characters",
            "entities",
            "desired_entities",
            "objects",
            "actions",
            "relationships",
            "ocr_text",
        ):
            items = value.get(key, [])
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    text = item.get("canonical_name") or item.get("name")
                else:
                    text = item
                if text:
                    parts.append(str(text))
        return " ".join(parts)

    @classmethod
    def _post_text(cls, post: Post, annotation: dict[str, object]) -> str:
        return " ".join(
            value
            for value in [
                post.caption or "",
                cls._query_text(annotation),
                str(annotation.get("caption_structure") or ""),
                str(annotation.get("editorial_angle") or ""),
            ]
            if value
        )

    @classmethod
    def _entity_keys(cls, value: dict[str, object]) -> set[str]:
        result: set[str] = set()
        for key in (
            "characters",
            "people",
            "organizations",
            "products",
            "teams",
            "locations",
            "animals",
        ):
            items = value.get(key, [])
            if isinstance(items, list):
                result.update(cls._normalized(str(item)) for item in items if str(item).strip())
        entities = value.get("entities", [])
        if isinstance(entities, list):
            for item in entities:
                if isinstance(item, dict):
                    name = item.get("canonical_name") or item.get("name")
                else:
                    name = item
                if name:
                    result.add(cls._normalized(str(name)))
        return {item for item in result if item}

    @staticmethod
    def _tokens(value: str) -> set[str]:
        features = caption_features(value)
        return {str(token) for token in cast(list[object], features["tokens"])}

    @staticmethod
    def _normalized(value: str) -> str:
        return " ".join(value.strip().casefold().split())

    @staticmethod
    def _jaccard(first: set[str], second: set[str]) -> float:
        union = first | second
        return len(first & second) / len(union) if union else 0.0

    @classmethod
    def _text_field_score(cls, first: str, second: str) -> float:
        if not first or not second:
            return 0.0
        first_tokens = cls._tokens(first)
        second_tokens = cls._tokens(second)
        return max(
            cls._jaccard(first_tokens, second_tokens),
            float(cls._normalized(first) == cls._normalized(second)),
        )

    @staticmethod
    def _caption_structure(caption: str) -> str:
        features = caption_features(caption)
        if bool(features["has_question"]):
            return "question"
        if bool(features["has_exclamation"]):
            return "exclamation"
        if int(features["word_count"]) <= 3:
            return "short_phrase"
        if int(features["word_count"]) <= 8:
            return "short_statement"
        return "long_statement"

    def _recency_score(self, post: Post, now: datetime) -> float:
        published = post.published_at
        if published is None:
            return 0.2 if post.date_precision in {"relative", "unknown"} else 0.0
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        age_days = max(0.0, (now - published).total_seconds() / 86400)
        return math.exp(-math.log(2) * age_days / self.configuration.recency_half_life_days)

    def _is_recent(self, post: Post, now: datetime) -> bool:
        published = post.published_at
        if published is None:
            return post.date_precision in {"relative", "unknown"}
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        return published >= now - timedelta(days=self.settings.duplicate_window_days)

    def _metadata_recent(self, metadata: dict[str, object], now: datetime) -> bool:
        value = metadata.get("published_at")
        if not value:
            return metadata.get("date_precision") in {"relative", "unknown"}
        try:
            published = datetime.fromisoformat(str(value))
        except ValueError:
            return False
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        return published >= now - timedelta(days=self.settings.duplicate_window_days)

    def _similarity_function(
        self,
        channel_id: int,
    ) -> Callable[[EvidenceCandidate, EvidenceCandidate], float]:
        vectors: dict[tuple[str, int, str], np.ndarray] = {}

        def vector(row: EvidenceCandidate) -> np.ndarray | None:
            caption = str(row.metadata.get("caption") or "")
            if not caption:
                return None
            key = (row.entity_type, row.entity_id, content_hash(caption))
            cached = vectors.get(key)
            if cached is not None:
                return cached
            if row.entity_type == "post":
                value = self._historical_text_vector(
                    channel_id,
                    post_id=row.entity_id,
                    caption=caption,
                )
            else:
                value = self._query_text_vector(
                    channel_id,
                    entity_type=row.entity_type,
                    entity_id=row.entity_id,
                    field="diversity_caption",
                    text=caption,
                    purpose="diversity",
                )
            vectors[key] = value
            return value

        def similarity(
            first: EvidenceCandidate,
            second: EvidenceCandidate,
        ) -> float:
            if (
                first.duplicate_cluster
                and second.duplicate_cluster
                and first.duplicate_cluster == second.duplicate_cluster
            ):
                return 1.0
            first_vector = vector(first)
            second_vector = vector(second)
            if first_vector is None or second_vector is None:
                return 0.0
            return max(0.0, cosine(first_vector, second_vector))

        return similarity

    @staticmethod
    def _mean_vector(vectors: list[np.ndarray]) -> np.ndarray:
        dimensions = {vector.size for vector in vectors}
        if not vectors or len(dimensions) != 1:
            raise ValueError("reference media representations are incompatible")
        value = np.mean(np.vstack(vectors), axis=0)
        norm = float(np.linalg.norm(value))
        return value / norm if norm else value  # type: ignore[no-any-return]

    @classmethod
    def _excluded_entity_match(
        cls,
        excluded: list[str],
        analysis: dict[str, object],
    ) -> bool:
        excluded_values = {cls._normalized(value) for value in excluded}
        return bool(excluded_values & cls._entity_keys(analysis))

    @classmethod
    def _constraint_score(
        cls,
        query: ReferenceRetrievalQuery,
        analysis: dict[str, object],
        media: MediaAsset,
    ) -> float:
        checks: list[float] = []
        if query.desired_entities:
            checks.append(
                cls._jaccard(
                    {cls._normalized(value) for value in query.desired_entities},
                    cls._entity_keys(analysis),
                )
            )
        for expected, key in (
            (query.desired_topic, "franchise"),
            (query.desired_action, "actions"),
            (query.desired_scene, "scene_archetype"),
            (query.desired_emotion, "emotion"),
            (query.desired_composition, "composition"),
            (query.desired_format, "visual_format"),
        ):
            if expected is None:
                continue
            actual = analysis.get(key, "")
            actual_text = (
                " ".join(str(value) for value in actual)
                if isinstance(
                    actual,
                    list,
                )
                else str(actual)
            )
            checks.append(cls._text_field_score(expected, actual_text))
        if query.text_overlay_preference:
            desired_overlay = query.text_overlay_preference in {"required", "prefer"}
            checks.append(float(bool(analysis.get("text_overlay")) == desired_overlay))
        if query.aspect_ratio:
            actual_ratio = media.width / max(media.height, 1)
            ratio_distance = abs(actual_ratio - query.aspect_ratio) / query.aspect_ratio
            checks.append(max(0.0, 1.0 - ratio_distance))
        return sum(checks) / len(checks) if checks else 0.5
