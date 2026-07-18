from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import httpx
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from runway.analysis.runtime import AgentRuntime, AgentTerminalError, runtime_for
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    BlockedSource,
    CandidateImage,
    MediaAsset,
    ModelRun,
    SearchRun,
    StyleProfile,
    utcnow,
)
from runway.db.repositories import audit, get_channel
from runway.discovery.providers import (
    ApiSearchProvider,
    BrowserSearchProvider,
    FixtureSearchProvider,
    ManualUrlProvider,
    SearchProvider,
)
from runway.discovery.schemas import ImageSearchResult
from runway.domain.enums import MediaKind, RunStatus
from runway.media.service import (
    content_addressed_copy,
    create_square_preview,
    ensure_fixture_images,
    inspect_image,
)
from runway.ranking.duplicates import DuplicateDetector
from runway.ranking.service import CandidateRanker, ranking_weights_json


class DiscoveryService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime: AgentRuntime | None = None,
    ):
        self.database = database
        self.settings = settings
        self.runtime = runtime or runtime_for(settings)
        self.detector = DuplicateDetector(database, settings)
        self.ranker = CandidateRanker(database, settings)

    async def discover(
        self,
        *,
        days: int = 10,
        provider_name: str = "fixture",
        manual_urls: list[str] | None = None,
        dry_run: bool = False,
        live: bool = False,
    ) -> dict[str, object]:
        profile_record, profile = self._active_profile()
        plan_payload = self._plan_payload(profile, days)
        plan_started = utcnow()
        plan = await self.runtime.create_search_plan(plan_payload)
        self._record_model_run(
            "create_search_plan",
            "search-plan-v1",
            [profile_record.id],
            plan_payload,
            plan.model_dump(),
            plan_started,
        )
        provider = self._provider(provider_name, manual_urls or [], live=live)
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            run = SearchRun(
                channel_id=channel.id,
                style_profile_id=profile_record.id,
                query_plan_json=json.dumps(
                    {
                        "plan": plan.model_dump(),
                        "days": days,
                        "dry_run": dry_run,
                        "ranking_weights": json.loads(ranking_weights_json()),
                    },
                    sort_keys=True,
                ),
                provider=provider.name,
                status=RunStatus.RUNNING.value,
            )
            session.add(run)
            session.flush()
            run_id = run.id
            audit(session, "search_started", "search_run", run.id, {"provider": provider.name})

        if dry_run and provider.name != "fixture":
            with self.database.session() as session:
                loaded_run = session.get(SearchRun, run_id)
                if loaded_run:
                    loaded_run.status = RunStatus.COMPLETED.value
                    loaded_run.completed_at = datetime.now(UTC)
            return {
                "run_id": run_id,
                "status": "planned_only",
                "provider": provider.name,
                "query_plan": plan.model_dump(),
                "candidates": 0,
            }

        try:
            page = await provider.search(plan)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            with self.database.session() as session:
                loaded_run = session.get(SearchRun, run_id)
                if loaded_run:
                    loaded_run.status = RunStatus.FAILED.value
                    loaded_run.completed_at = datetime.now(UTC)
                    loaded_run.error_summary = error
                    audit(
                        session,
                        "search_failed",
                        "search_run",
                        loaded_run.id,
                        {"provider": provider.name, "error": error},
                    )
            raise
        provider_results = [result.model_dump() for result in page.results]
        with self.database.session() as session:
            loaded_run = session.get(SearchRun, run_id)
            if loaded_run:
                saved = json.loads(loaded_run.query_plan_json)
                saved["provider_results"] = provider_results
                loaded_run.query_plan_json = json.dumps(saved, sort_keys=True)

        created = 0
        accepted = 0
        rejected = 0
        errors: list[str] = []
        for result in page.results:
            try:
                hard_rejected = await self._process_result(run_id, result)
            except Exception as exc:
                errors.append(f"rank {result.result_rank}: {type(exc).__name__}: {exc}")
                with self.database.session() as session:
                    audit(
                        session,
                        "candidate_download_error",
                        "search_run",
                        run_id,
                        {"result": result.model_dump(), "error": errors[-1]},
                    )
                if isinstance(exc, AgentTerminalError):
                    with self.database.session() as session:
                        loaded_run = session.get(SearchRun, run_id)
                        if loaded_run:
                            loaded_run.status = RunStatus.FAILED.value
                            loaded_run.completed_at = datetime.now(UTC)
                            loaded_run.error_summary = errors[-1]
                    raise
                continue
            created += 1
            rejected += hard_rejected
            accepted += not hard_rejected

        with self.database.session() as session:
            loaded_run = session.get(SearchRun, run_id)
            if loaded_run:
                loaded_run.status = (
                    RunStatus.COMPLETED.value if not errors else RunStatus.FAILED.value
                )
                loaded_run.completed_at = datetime.now(UTC)
                loaded_run.result_count = created
                loaded_run.error_summary = "\n".join(errors) or None
                audit(
                    session,
                    "search_completed",
                    "search_run",
                    loaded_run.id,
                    {"accepted": accepted, "rejected": rejected, "errors": len(errors)},
                )
        return {
            "run_id": run_id,
            "status": "completed" if not errors else "completed_with_errors",
            "provider": provider.name,
            "query_plan": plan.model_dump(),
            "candidates": created,
            "accepted": accepted,
            "hard_rejected": rejected,
            "errors": errors,
        }

    def list_candidates(
        self, *, run_id: int | None = None, accepted_only: bool = False, limit: int = 100
    ) -> list[dict[str, object]]:
        with self.database.session() as session:
            statement = select(CandidateImage).order_by(
                desc(CandidateImage.final_rank_score), CandidateImage.id
            )
            if run_id is not None:
                statement = statement.where(CandidateImage.search_run_id == run_id)
            if accepted_only:
                statement = statement.where(CandidateImage.hard_rejection_reason.is_(None))
            rows = session.scalars(statement.limit(limit)).all()
            return [self._candidate_dict(session, row) for row in rows]

    def detail(self, candidate_id: int) -> dict[str, object]:
        with self.database.session() as session:
            row = session.get(CandidateImage, candidate_id)
            if row is None:
                raise LookupError(f"candidate {candidate_id} not found")
            return self._candidate_dict(session, row)

    def run_status(self, run_id: int) -> dict[str, object]:
        with self.database.session() as session:
            run = session.get(SearchRun, run_id)
            if run is None:
                raise LookupError(f"search run {run_id} not found")
            return {
                "id": run.id,
                "provider": run.provider,
                "status": run.status,
                "started_at": run.started_at.isoformat(),
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "result_count": run.result_count,
                "query_plan": json.loads(run.query_plan_json),
                "error_summary": run.error_summary,
            }

    def reject_candidate(
        self, candidate_id: int, reason: str = "user_rejected"
    ) -> dict[str, object]:
        with self.database.session() as session:
            candidate = session.get(CandidateImage, candidate_id)
            if candidate is None:
                raise LookupError(f"candidate {candidate_id} not found")
            candidate.hard_rejection_reason = reason
            candidate.final_rank_score = 0.0
            audit(
                session,
                "candidate_rejected",
                "candidate_image",
                candidate.id,
                {"reason": reason},
            )
        return self.detail(candidate_id)

    def block_domain(self, candidate_id: int) -> dict[str, object]:
        with self.database.session() as session:
            candidate = session.get(CandidateImage, candidate_id)
            if candidate is None:
                raise LookupError(f"candidate {candidate_id} not found")
            if not candidate.source_domain:
                raise ValueError("candidate has no source domain")
            existing = session.scalar(
                select(BlockedSource).where(
                    BlockedSource.source_type == "domain",
                    BlockedSource.value == candidate.source_domain,
                )
            )
            if existing is None:
                session.add(
                    BlockedSource(
                        source_type="domain",
                        value=candidate.source_domain,
                        reason="user blocked from candidate review",
                    )
                )
            for item in session.scalars(
                select(CandidateImage).where(
                    CandidateImage.source_domain == candidate.source_domain,
                    CandidateImage.hard_rejection_reason.is_(None),
                )
            ):
                item.hard_rejection_reason = "blocked_domain"
                item.final_rank_score = 0.0
            audit(
                session,
                "domain_blocked",
                "candidate_image",
                candidate.id,
                {"domain": candidate.source_domain},
            )
        return self.detail(candidate_id)

    async def _process_result(self, run_id: int, result: ImageSearchResult) -> bool:
        source = await self._resolve_result(result)
        features = inspect_image(source)
        duplicate = self.detector.inspect(features, source_url=result.direct_image_url)
        analysis_started = utcnow()
        analysis = await self.runtime.analyze_candidate_image(
            {
                "candidate_id": result.result_rank,
                "search_query": result.search_query,
                "source_domain": result.source_domain,
                "width": features.width,
                "height": features.height,
                "quality_metrics": features.quality_metrics,
                "_image_path": str(source),
            }
        )
        self._record_model_run(
            "analyze_candidate_image",
            "candidate-analysis-v1",
            [run_id, result.result_rank],
            result.model_dump(),
            analysis.model_dump(),
            analysis_started,
        )
        ranking = self.ranker.rank(
            features,
            analysis,
            duplicate,
            source_domain=result.source_domain,
            rights_status=result.rights_status,
        )

        with self.database.session() as session:
            asset = session.scalar(
                select(MediaAsset).where(MediaAsset.sha256 == features.sha256).limit(1)
            )
            if asset is None:
                _destination, relative = content_addressed_copy(
                    source, self.settings, MediaKind.CANDIDATE.value, features
                )
                asset = MediaAsset(
                    kind=MediaKind.CANDIDATE.value,
                    local_path=relative,
                    original_url=result.direct_image_url,
                    source_page_url=result.source_page_url,
                    source_domain=result.source_domain,
                    original_filename=Path(urlparse(result.direct_image_url).path).name,
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
                    quality_metrics_json=json.dumps(features.quality_metrics, sort_keys=True),
                    downloaded_at=datetime.now(UTC),
                    rights_status=result.rights_status,
                )
                session.add(asset)
                session.flush()

            preview_asset_id = None
            if ranking.hard_rejection_reason is None:
                preview_path = (
                    self.settings.resolved_data_dir
                    / "media"
                    / "previews"
                    / f"{features.sha256}-square.jpg"
                )
                create_square_preview(
                    self.settings.resolved_data_dir / asset.local_path, preview_path
                )
                preview_features = inspect_image(preview_path)
                preview = session.scalar(
                    select(MediaAsset).where(MediaAsset.sha256 == preview_features.sha256).limit(1)
                )
                if preview is None:
                    preview = MediaAsset(
                        kind=MediaKind.PREVIEW.value,
                        local_path=preview_path.relative_to(
                            self.settings.resolved_data_dir
                        ).as_posix(),
                        original_url=result.direct_image_url,
                        source_page_url=result.source_page_url,
                        source_domain=result.source_domain,
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
                            preview_features.quality_metrics, sort_keys=True
                        ),
                        downloaded_at=datetime.now(UTC),
                        rights_status=result.rights_status,
                    )
                    session.add(preview)
                    session.flush()
                preview_asset_id = preview.id

            candidate = CandidateImage(
                search_run_id=run_id,
                media_asset_id=asset.id,
                preview_asset_id=preview_asset_id,
                search_query=result.search_query,
                result_rank=result.result_rank,
                source_page_url=result.source_page_url,
                direct_image_url=result.direct_image_url,
                source_domain=result.source_domain,
                rights_status=result.rights_status,
                original_width=result.original_width or features.width,
                original_height=result.original_height or features.height,
                provider_result_json=result.model_dump_json(),
                retrieved_at=datetime.now(UTC),
                download_status="downloaded",
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
            )
            session.add(candidate)
            session.flush()
            audit(
                session,
                "candidate_ranked",
                "candidate_image",
                candidate.id,
                {
                    "hard_rejection": ranking.hard_rejection_reason,
                    "score": ranking.final_rank_score,
                    "source_page_url": result.source_page_url,
                },
            )
        return ranking.hard_rejection_reason is not None

    async def _resolve_result(self, result: ImageSearchResult) -> Path:
        if result.direct_image_url.startswith("fixture://"):
            assets = ensure_fixture_images(self.settings)
            key = result.direct_image_url.removeprefix("fixture://")
            if key not in assets:
                raise FileNotFoundError(key)
            return assets[key]
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(result.direct_image_url)
            response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";")[0]
        if content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError(f"unsupported MIME type {content_type or 'unknown'}")
        if len(response.content) > self.settings.maximum_image_bytes:
            raise ValueError("candidate exceeds configured maximum size")
        digest = hashlib.sha256(response.content).hexdigest()
        extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[content_type]
        path = self.settings.resolved_data_dir / "raw" / "downloads" / f"{digest}{extension}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(response.content)
        return path

    def _active_profile(self) -> tuple[StyleProfile, dict[str, object]]:
        with self.database.session() as session:
            record = session.scalar(
                select(StyleProfile)
                .where(StyleProfile.is_active.is_(True))
                .order_by(desc(StyleProfile.version))
                .limit(1)
            )
            if record is None:
                raise LookupError("build a style profile before discovery")
            session.expunge(record)
            return record, cast(dict[str, object], json.loads(record.profile_json))

    @staticmethod
    def _plan_payload(profile: dict[str, object], days: int) -> dict[str, object]:
        distribution = profile.get("franchise_distribution", [])
        distribution_rows = cast(list[list[Any]], distribution)
        sample_size = int(
            cast(dict[str, Any], profile.get("caption_statistics", {})).get("sample_size", 0)
        )
        minimum_support = max(2, round(sample_size * 0.01))
        known_franchises = [
            (str(item[0]), int(item[1]))
            for item in distribution_rows
            if len(item) >= 2
            and str(item[0]).strip().lower() not in {"", "unknown", "none", "null"}
        ]
        primary_franchise = known_franchises[0][0] if known_franchises else None
        supported = [item for item in known_franchises if item[1] >= minimum_support]
        focus_franchises = [
            name for name, _count in sorted(supported, key=lambda item: (item[1], item[0]))
        ]
        if primary_franchise and primary_franchise not in focus_franchises:
            focus_franchises.append(primary_franchise)
        return {
            "profile_version": profile.get("version"),
            "days": days,
            "primary_franchise": primary_franchise,
            "underused_franchises": focus_franchises[:5],
            "preferred_compositions": profile.get("visual_compositions", []),
            "preferred_visual_formats": profile.get("visual_formats", []),
            "caption_structures": profile.get("dominant_caption_structures", []),
            "recent_exclusions": [
                "fan art",
                "personal artwork",
                "artist portfolios",
                "commissions",
                "independent illustrations",
            ],
            "current_queue_distribution": [],
        }

    def _provider(self, name: str, manual_urls: list[str], *, live: bool) -> SearchProvider:
        providers: dict[str, SearchProvider] = {
            "fixture": FixtureSearchProvider(),
            "manual": ManualUrlProvider(manual_urls),
        }
        if name == "browser":
            return BrowserSearchProvider(self.settings, live=live)
        if name == "api":
            return ApiSearchProvider(self.settings)
        if name not in providers:
            raise ValueError(f"unknown search provider {name}")
        return providers[name]

    def _record_model_run(
        self,
        task: str,
        prompt_version: str,
        input_ids: list[int],
        request: dict[str, object],
        output: dict[str, object],
        started: datetime,
    ) -> None:
        with self.database.session() as session:
            session.add(
                ModelRun(
                    task_type=task,
                    provider=self.runtime.provider,
                    model=self.runtime.model_name,
                    prompt_version=prompt_version,
                    input_record_ids_json=json.dumps(input_ids),
                    request_summary_json=json.dumps(request, sort_keys=True, default=str),
                    structured_output_json=json.dumps(output, sort_keys=True, default=str),
                    token_usage_json=json.dumps(self.runtime.last_token_usage, sort_keys=True),
                    started_at=started,
                    completed_at=datetime.now(UTC),
                    status="completed",
                )
            )

    def _candidate_dict(self, session: Session, candidate: CandidateImage) -> dict[str, object]:
        media = session.get(MediaAsset, candidate.media_asset_id)
        preview = (
            session.get(MediaAsset, candidate.preview_asset_id)
            if candidate.preview_asset_id
            else None
        )
        return {
            "id": candidate.id,
            "search_run_id": candidate.search_run_id,
            "search_query": candidate.search_query,
            "result_rank": candidate.result_rank,
            "source_page_url": candidate.source_page_url,
            "direct_image_url": candidate.direct_image_url,
            "source_domain": candidate.source_domain,
            "rights_status": candidate.rights_status,
            "original_url": (
                f"/media/{Path(media.local_path).relative_to('media').as_posix()}"
                if media
                else None
            ),
            "preview_url": (
                f"/media/{Path(preview.local_path).relative_to('media').as_posix()}"
                if preview
                else None
            ),
            "detected_topic": json.loads(candidate.detected_topic_json),
            "scores": json.loads(candidate.score_components_json),
            "final_rank_score": candidate.final_rank_score,
            "hard_rejection_reason": candidate.hard_rejection_reason,
            "warnings": json.loads(candidate.soft_warnings_json),
            "selection_reason": candidate.selection_reason,
        }
