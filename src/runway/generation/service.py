from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import desc, or_, select

from runway.analysis.runtime import AgentRuntime, runtime_for
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CandidateImage,
    GeneratedAssetLineage,
    ImageGenerationRun,
    MediaAsset,
    Post,
    PostMedia,
    SearchRun,
    StyleProfile,
)
from runway.db.repositories import audit, get_channel
from runway.domain.enums import RunStatus
from runway.generation.providers import ImageGenerationProviderRegistry
from runway.generation.schemas import CreativeBrief, ProviderRequest
from runway.intelligence.agent_harness import (
    AgentBudget,
    AgentInputEnvelope,
    AgentOutputEnvelope,
    AgentStepResult,
    IntelligenceAgentHarness,
)
from runway.intelligence.embeddings import ActiveRepresentationResolver, cosine
from runway.intelligence.policies import ChannelPolicyService
from runway.media.service import (
    content_addressed_copy,
    create_square_preview,
    inspect_image,
)
from runway.ranking.duplicates import DuplicateDetector
from runway.ranking.service import CandidateRanker


class ImageGenerationService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        runtime: AgentRuntime | None = None,
        registry: ImageGenerationProviderRegistry | None = None,
    ):
        self.database = database
        self.settings = settings
        self.runtime = runtime or runtime_for(settings)
        self.registry = registry or ImageGenerationProviderRegistry()
        self.policy = ChannelPolicyService(database, settings)
        self.detector = DuplicateDetector(database, settings)
        self.ranker = CandidateRanker(database, settings)
        self.harness = IntelligenceAgentHarness(database)
        self.representations = ActiveRepresentationResolver(database)

    async def generate(
        self,
        brief: CreativeBrief,
        *,
        provider_name: str = "mock",
        output_count: int = 1,
    ) -> dict[str, object]:
        provider = self.registry.get(provider_name)
        if provider.capabilities.paid_usage:
            raise ValueError("paid image-generation providers are disabled")
        if brief.capability not in provider.capabilities.capabilities:
            raise ValueError(f"provider {provider.name} does not support {brief.capability}")
        if len(brief.reference_media_ids) > provider.capabilities.maximum_references:
            raise ValueError("the provider reference limit would be exceeded")

        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            channel_id = channel.id
            references = self._reference_assets(
                session,
                channel_id,
                brief.reference_media_ids,
            )
            profile = session.scalar(
                select(StyleProfile)
                .where(
                    StyleProfile.channel_id == channel_id,
                    StyleProfile.is_active.is_(True),
                )
                .order_by(desc(StyleProfile.version))
                .limit(1)
            )
            reference_rights: list[dict[str, object]] = []
            approved = set(brief.explicitly_approved_reference_ids)
            for reference in references:
                decision = self.policy.rights_decision(
                    channel_id=channel_id,
                    rights_status=reference.rights_status,
                    explicitly_approved=reference.id in approved,
                    for_generation=True,
                )
                reference_rights.append(
                    {
                        "media_asset_id": reference.id,
                        "rights_status": reference.rights_status,
                        "decision": decision.model_dump(),
                    }
                )
                if decision.outcome != "allowed":
                    raise ValueError(
                        f"reference media {reference.id} is not eligible: {decision.reason}"
                    )
            if (
                brief.capability in {"reference_edit", "variation"}
                and brief.consent_state == "missing"
            ):
                raise ValueError("reference generation requires confirmed consent state")
            run = ImageGenerationRun(
                channel_id=channel_id,
                provider=provider.name,
                model=provider.model,
                model_version=provider.model_version,
                capability=brief.capability,
                creative_brief_json=brief.model_dump_json(),
                reference_media_ids_json=json.dumps(brief.reference_media_ids),
                reference_rights_json=json.dumps(reference_rights, sort_keys=True),
                consent_state=brief.consent_state,
                instructions=brief.instruction,
                negative_instructions=brief.negative_instruction,
                seed=brief.seed,
                parameters_json=json.dumps(brief.parameters, sort_keys=True),
                status=RunStatus.RUNNING.value,
            )
            session.add(run)
            session.flush()
            run_id = run.id
            search_run = SearchRun(
                channel_id=channel_id,
                style_profile_id=profile.id if profile else None,
                query_plan_json=json.dumps(
                    {
                        "source": "image_generation",
                        "generation_run_id": run_id,
                        "brief": brief.model_dump(),
                    },
                    sort_keys=True,
                ),
                provider=f"generated:{provider.name}",
                status=RunStatus.RUNNING.value,
            )
            session.add(search_run)
            session.flush()
            search_run_id = search_run.id
            reference_paths = [
                self.settings.resolved_data_dir / reference.local_path for reference in references
            ]

        request = ProviderRequest(
            run_identity=f"generation-{run_id}",
            brief=brief,
            reference_paths=reference_paths,
            output_directory=(
                self.settings.resolved_data_dir / "raw" / "generated" / f"run-{run_id}"
            ),
            output_count=output_count,
        )
        try:

            async def invoke_provider(
                _input: AgentInputEnvelope,
                _attempt: int,
            ) -> AgentStepResult:
                generated = await provider.generate(request)
                return AgentStepResult(
                    output=AgentOutputEnvelope(
                        payload={
                            "outputs": [output.model_dump(mode="json") for output in generated]
                        }
                    ),
                    usage={"total_tokens": 0, "paid_provider_calls": 0},
                    artifact_type="image_generation_run",
                    artifact_id=str(run_id),
                )

            agent_run_id, agent_output = await self.harness.execute(
                channel_id=channel_id,
                capability="image_generation_mock",
                provider=provider.name,
                model=f"{provider.model}:{provider.model_version}",
                prompt_version="image-generation-provider-v1",
                input_value=AgentInputEnvelope(
                    entity_ids=brief.reference_media_ids,
                    payload={
                        "generation_run_id": run_id,
                        "creative_brief": brief.model_dump(mode="json"),
                        "output_count": output_count,
                        "paid_usage": False,
                    },
                ),
                handler=invoke_provider,
                budget=AgentBudget(
                    max_steps=1,
                    max_attempts_per_step=1,
                    max_total_tokens=0,
                    max_seconds=300,
                    timeout_seconds=300,
                ),
                run_key=f"image-generation:{run_id}",
            )
            raw_outputs = agent_output.payload.get("outputs")
            if not isinstance(raw_outputs, list):
                raise ValueError("image-generation agent returned malformed outputs")
            from runway.generation.schemas import ProviderImage

            outputs = [ProviderImage.model_validate(output) for output in raw_outputs]
            with self.database.session() as session:
                loaded_generation = session.get(ImageGenerationRun, run_id)
                if loaded_generation is not None:
                    loaded_generation.agent_run_id = agent_run_id
            candidate_ids = [
                await self._ingest_output(
                    generation_run_id=run_id,
                    search_run_id=search_run_id,
                    channel_id=channel_id,
                    output_path=output.path,
                    output_index=index,
                    seed=output.seed,
                    provider_metadata=output.provider_metadata,
                    provider_safety=output.safety_result,
                    references=references,
                    brief=brief,
                )
                for index, output in enumerate(outputs, start=1)
            ]
        except Exception as exc:
            with self.database.session() as session:
                loaded_generation = session.get(ImageGenerationRun, run_id)
                loaded_search = session.get(SearchRun, search_run_id)
                if loaded_generation is not None:
                    loaded_generation.status = RunStatus.FAILED.value
                    loaded_generation.error_summary = f"{type(exc).__name__}: {exc}"
                    loaded_generation.completed_at = datetime.now(UTC)
                if loaded_search is not None:
                    loaded_search.status = RunStatus.FAILED.value
                    loaded_search.error_summary = f"{type(exc).__name__}: {exc}"
                    loaded_search.completed_at = datetime.now(UTC)
            raise
        with self.database.session() as session:
            loaded_generation = session.get(ImageGenerationRun, run_id)
            loaded_search = session.get(SearchRun, search_run_id)
            if loaded_generation is not None:
                loaded_generation.status = RunStatus.COMPLETED.value
                loaded_generation.completed_at = datetime.now(UTC)
                loaded_generation.provider_metadata_json = json.dumps(
                    {"output_count": len(candidate_ids)},
                    sort_keys=True,
                )
            if loaded_search is not None:
                loaded_search.status = RunStatus.COMPLETED.value
                loaded_search.completed_at = datetime.now(UTC)
                loaded_search.result_count = len(candidate_ids)
            audit(
                session,
                "image_generation_completed",
                "image_generation_run",
                run_id,
                {
                    "provider": provider.name,
                    "candidate_ids": candidate_ids,
                    "paid_usage": False,
                },
            )
        return {
            "generation_run_id": run_id,
            "search_run_id": search_run_id,
            "status": "completed",
            "candidate_ids": candidate_ids,
            "provider": provider.name,
            "paid_usage": False,
        }

    async def _ingest_output(
        self,
        *,
        generation_run_id: int,
        search_run_id: int,
        channel_id: int,
        output_path: Path,
        output_index: int,
        seed: int,
        provider_metadata: dict[str, object],
        provider_safety: dict[str, object],
        references: list[MediaAsset],
        brief: CreativeBrief,
    ) -> int:
        features = inspect_image(output_path)
        duplicate = self.detector.inspect(features)
        analysis = await self.runtime.analyze_candidate_image(
            {
                "candidate_id": generation_run_id * 100 + output_index,
                "source": "generated",
                "creative_brief": brief.model_dump(),
                "width": features.width,
                "height": features.height,
                "quality_metrics": features.quality_metrics,
                "_image_path": str(output_path),
            }
        )
        ranking = self.ranker.rank(
            features,
            analysis,
            duplicate,
            source_domain="runway.local",
            rights_status="creator_owned",
            image_path=output_path,
        )
        _stored, relative = content_addressed_copy(
            output_path,
            self.settings,
            "generated",
            features,
        )
        with self.database.session() as session:
            asset = session.scalar(
                select(MediaAsset).where(MediaAsset.sha256 == features.sha256).limit(1)
            )
            if asset is None:
                asset = MediaAsset(
                    kind="generated",
                    local_path=relative,
                    original_url=f"generated://{generation_run_id}/{output_index}",
                    source_page_url=None,
                    source_domain="runway.local",
                    original_filename=output_path.name,
                    mime_type=features.mime_type,
                    width=features.width,
                    height=features.height,
                    file_size=features.file_size,
                    sha256=features.sha256,
                    perceptual_hash=features.perceptual_hash,
                    crop_resistant_hash=features.crop_resistant_hash,
                    embedding_model=features.embedding_model,
                    embedding_vector=features.embedding_bytes(),
                    blur_score=features.blur_score,
                    quality_metrics_json=json.dumps(
                        features.quality_metrics,
                        sort_keys=True,
                    ),
                    downloaded_at=datetime.now(UTC),
                    rights_status="creator_owned",
                )
                session.add(asset)
                session.flush()
            preview_id = None
            if ranking.hard_rejection_reason is None:
                preview_path = (
                    self.settings.resolved_data_dir
                    / "media"
                    / "previews"
                    / f"{features.sha256}-generated-square.jpg"
                )
                create_square_preview(
                    self.settings.resolved_data_dir / asset.local_path,
                    preview_path,
                )
                preview_features = inspect_image(preview_path)
                preview = session.scalar(
                    select(MediaAsset).where(MediaAsset.sha256 == preview_features.sha256).limit(1)
                )
                if preview is None:
                    preview = MediaAsset(
                        kind="preview",
                        local_path=preview_path.relative_to(
                            self.settings.resolved_data_dir
                        ).as_posix(),
                        original_url=asset.original_url,
                        source_page_url=None,
                        source_domain="runway.local",
                        original_filename=preview_path.name,
                        mime_type=preview_features.mime_type,
                        width=preview_features.width,
                        height=preview_features.height,
                        file_size=preview_features.file_size,
                        sha256=preview_features.sha256,
                        perceptual_hash=preview_features.perceptual_hash,
                        crop_resistant_hash=preview_features.crop_resistant_hash,
                        embedding_model=preview_features.embedding_model,
                        embedding_vector=preview_features.embedding_bytes(),
                        blur_score=preview_features.blur_score,
                        quality_metrics_json=json.dumps(
                            preview_features.quality_metrics,
                            sort_keys=True,
                        ),
                        downloaded_at=datetime.now(UTC),
                        rights_status="creator_owned",
                    )
                    session.add(preview)
                    session.flush()
                preview_id = preview.id
            candidate = CandidateImage(
                search_run_id=search_run_id,
                media_asset_id=asset.id,
                preview_asset_id=preview_id,
                search_query=brief.instruction,
                result_rank=output_index,
                source_page_url=None,
                direct_image_url=asset.original_url,
                source_domain="runway.local",
                rights_status="creator_owned",
                original_width=features.width,
                original_height=features.height,
                provider_result_json=json.dumps(
                    {
                        "provider_metadata": provider_metadata,
                        "seed": seed,
                        "generation_run_id": generation_run_id,
                    },
                    sort_keys=True,
                ),
                retrieved_at=datetime.now(UTC),
                download_status="generated",
                detected_topic_json=analysis.model_dump_json(),
                quality_score=ranking.quality_score,
                style_score=ranking.style_score,
                novelty_score=ranking.novelty_score,
                caption_potential_score=ranking.caption_potential_score,
                source_risk_score=ranking.source_risk_score,
                final_rank_score=ranking.final_rank_score,
                hard_rejection_reason=ranking.hard_rejection_reason,
                soft_warnings_json=json.dumps(ranking.warnings),
                score_components_json=ranking.model_dump_json(),
                selection_reason=ranking.selection_reason,
                topic_eligibility_class=ranking.topic_eligibility_class,
                topic_eligibility_json=json.dumps(
                    ranking.topic_eligibility,
                    sort_keys=True,
                ),
                representation_provenance_json=json.dumps(
                    ranking.representation_provenance,
                    sort_keys=True,
                ),
            )
            session.add(candidate)
            session.flush()
            reference_similarity, reference_representation = self._reference_similarity(
                channel_id,
                output_path,
                references,
            )
            lineage = GeneratedAssetLineage(
                channel_id=channel_id,
                generation_run_id=generation_run_id,
                media_asset_id=asset.id,
                candidate_image_id=candidate.id,
                content_hash=features.sha256,
                safety_result_json=json.dumps(
                    {
                        **provider_safety,
                        "unsafe_probability": analysis.unsafe_probability,
                        "watermark_probability": analysis.watermark_probability,
                    },
                    sort_keys=True,
                ),
                rights_result_json=json.dumps(
                    {
                        "output_rights_status": "creator_owned",
                        "reference_media_ids": brief.reference_media_ids,
                    },
                    sort_keys=True,
                ),
                reference_similarity=reference_similarity,
                historical_similarity=duplicate.highest_semantic_similarity,
                duplicate_result_json=duplicate.model_dump_json(),
                annotation_json=analysis.model_dump_json(),
                ranking_json=json.dumps(
                    {
                        **ranking.model_dump(),
                        "reference_similarity_representation": reference_representation,
                    },
                    sort_keys=True,
                ),
                review_status="pending",
            )
            session.add(lineage)
            audit(
                session,
                "generated_asset_validated",
                "candidate_image",
                candidate.id,
                {
                    "generation_run_id": generation_run_id,
                    "hard_rejection": ranking.hard_rejection_reason,
                    "content_hash": features.sha256,
                },
            )
            return candidate.id

    def _reference_assets(
        self,
        session: Any,
        channel_id: int,
        media_ids: list[int],
    ) -> list[MediaAsset]:
        if not media_ids:
            return []
        historical_ids = (
            select(PostMedia.media_asset_id)
            .join(Post, Post.id == PostMedia.post_id)
            .where(Post.channel_id == channel_id)
        )
        candidate_ids = (
            select(CandidateImage.media_asset_id)
            .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
            .where(SearchRun.channel_id == channel_id)
        )
        generated_ids = select(GeneratedAssetLineage.media_asset_id).where(
            GeneratedAssetLineage.channel_id == channel_id
        )
        references = session.scalars(
            select(MediaAsset).where(
                MediaAsset.id.in_(media_ids),
                or_(
                    MediaAsset.id.in_(historical_ids),
                    MediaAsset.id.in_(candidate_ids),
                    MediaAsset.id.in_(generated_ids),
                ),
            )
        ).all()
        by_id = {reference.id: reference for reference in references}
        missing = [media_id for media_id in media_ids if media_id not in by_id]
        if missing:
            raise LookupError(
                "references are missing or outside the configured channel: "
                + ", ".join(str(value) for value in missing)
            )
        return [by_id[media_id] for media_id in media_ids]

    def _reference_similarity(
        self,
        channel_id: int,
        output_path: Path,
        references: list[MediaAsset],
    ) -> tuple[float | None, dict[str, object]]:
        if not references:
            return None, {}
        output_vector, resolution = self.representations.image_vector(
            channel_id,
            output_path,
            score_purpose="generated_reference_similarity_output",
        )
        similarities = [
            cosine(
                output_vector,
                self.representations.image_vector(
                    channel_id,
                    self._media_path(reference),
                    score_purpose="generated_reference_similarity_reference",
                )[0],
            )
            for reference in references
        ]
        return max(similarities), resolution.as_dict()

    def _media_path(self, media: MediaAsset) -> Path:
        raw = Path(media.local_path)
        resolved = (raw if raw.is_absolute() else self.settings.resolved_data_dir / raw).resolve()
        root = self.settings.resolved_data_dir.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"media asset {media.id} escapes the configured data root")
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return resolved
