from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy import select

from runway.analysis.service import AnalysisService
from runway.captions.service import CaptionService
from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db import initialize_database
from runway.db.models import (
    CandidateImage,
    CaptionSlate,
    ModelRun,
    RetrievalEvidenceRecord,
)
from runway.discovery.service import DiscoveryService
from runway.evaluation.datasets import DatasetRepository, EvaluationSplit
from runway.evaluation.reports import write_json, write_markdown
from runway.intelligence.embeddings import configuration_hash
from runway.intelligence.profile import StyleProfileService

EvaluationPurpose = Literal[
    "development",
    "tuning",
    "holdout_release",
]


def frozen_baseline_directory() -> Path:
    return Settings().project_root / "benchmarks" / "intelligence" / "baseline-876fe5f"


def load_frozen_baseline() -> dict[str, Any]:
    root = frozen_baseline_directory()
    return {
        "manifest": json.loads((root / "manifest.json").read_text(encoding="utf-8")),
        "dataset": json.loads((root / "dataset.json").read_text(encoding="utf-8")),
        "metrics": json.loads((root / "metrics.json").read_text(encoding="utf-8")),
        "outputs": json.loads((root / "outputs.json").read_text(encoding="utf-8")),
    }


async def run_candidate_evaluation(
    *,
    splits: set[EvaluationSplit],
    purpose: EvaluationPurpose,
    destination: Path,
) -> dict[str, Any]:
    if "locked_holdout" in splits and purpose != "holdout_release":
        raise PermissionError("holdout inputs may run only in the release evaluation")
    if purpose == "tuning" and splits != {"tuning"}:
        raise PermissionError("a tuning run may execute only tuning cases")
    if purpose == "development" and splits != {"development"}:
        raise PermissionError("a development run may execute only development cases")
    repository = DatasetRepository()
    integrity = repository.validate_split_integrity()
    baseline = load_frozen_baseline()
    baseline_cases = [
        row
        for row in cast(list[dict[str, Any]], baseline["dataset"]["cases"])
        if row["split"] in splits
    ]
    started_at = datetime.now(UTC)
    with tempfile.TemporaryDirectory(
        prefix="runway-canonical-evaluation-",
        ignore_cleanup_errors=True,
    ) as temporary:
        settings = Settings(
            data_dir=Path(temporary) / "data",
            agent_runtime="mock",
            publishing_enabled=False,
            enable_browser_search=False,
        )
        database = initialize_database(settings)
        try:
            capture = CaptureService(database, settings).run_fixture()
            analysis = await AnalysisService(database, settings).analyze_history()
            profile_service = StyleProfileService(database, settings)
            profile = await profile_service.build()
            discovery_service = DiscoveryService(database, settings)
            discovery = await discovery_service.discover(
                days=10,
                provider_name="fixture",
                dry_run=True,
            )
            cases: list[dict[str, Any]] = []
            caption_service = CaptionService(database, settings)
            for baseline_case in baseline_cases:
                fixture_uri = str(baseline_case["fixture_uri"])
                with database.session() as session:
                    candidate = session.scalar(
                        select(CandidateImage).where(CandidateImage.direct_image_url == fixture_uri)
                    )
                    if candidate is None:
                        raise LookupError(f"evaluation candidate {fixture_uri} was not discovered")
                    if candidate.hard_rejection_reason:
                        raise ValueError(
                            f"evaluation candidate {fixture_uri} was rejected: "
                            f"{candidate.hard_rejection_reason}"
                        )
                    candidate.detected_topic_json = json.dumps(
                        baseline_case["candidate_analysis"],
                        sort_keys=True,
                    )
                    candidate_id = candidate.id
                    duplicate = {
                        "hard_rejection_reason": candidate.hard_rejection_reason,
                        "novelty_score": candidate.novelty_score,
                        "warnings": json.loads(candidate.soft_warnings_json),
                    }
                options = await caption_service.generate(candidate_id)
                with database.session() as session:
                    slate = (
                        session.get(CaptionSlate, options.slate_id)
                        if options.slate_id is not None
                        else None
                    )
                    if slate is None:
                        raise LookupError(
                            f"evaluation case {baseline_case['case_id']} has no slate"
                        )
                    model_run = (
                        session.get(ModelRun, slate.model_run_id)
                        if slate.model_run_id is not None
                        else None
                    )
                    structured = (
                        cast(
                            dict[str, Any],
                            json.loads(model_run.structured_output_json),
                        )
                        if model_run is not None
                        else {}
                    )
                    ranking = cast(
                        list[dict[str, Any]],
                        structured.get("ranking", []),
                    )
                    evidence = session.scalars(
                        select(RetrievalEvidenceRecord)
                        .where(RetrievalEvidenceRecord.retrieval_run_id == slate.retrieval_run_id)
                        .order_by(
                            RetrievalEvidenceRecord.selected.desc(),
                            RetrievalEvidenceRecord.selected_rank,
                            RetrievalEvidenceRecord.id,
                        )
                    ).all()
                    selected_evidence = [
                        {
                            "entity_type": row.entity_type,
                            "entity_id": row.entity_id,
                            "retrieval_channel": row.retrieval_channel,
                            "evidence_role": row.evidence_role,
                            "fusion_score": row.fusion_score,
                            "selected_rank": row.selected_rank,
                        }
                        for row in evidence
                        if row.selected
                    ]
                    considered_count = len(evidence)
                    retrieval_channels = sorted({row.retrieval_channel for row in evidence})
                displayed = (
                    [options.recommended, *options.alternatives] if not options.abstained else []
                )
                selected_rows = [
                    row for row in ranking if str(row.get("text", "")) in set(displayed)
                ]
                cases.append(
                    {
                        "case_id": baseline_case["case_id"],
                        "split": baseline_case["split"],
                        "fixture_uri": fixture_uri,
                        "candidate_analysis": baseline_case["candidate_analysis"],
                        "retrieval": {
                            "retrieval_run_id": slate.retrieval_run_id,
                            "considered_count": considered_count,
                            "channels": retrieval_channels,
                            "selected_evidence": selected_evidence,
                            "role_coverage": sorted(
                                {
                                    str(row["evidence_role"])
                                    for row in selected_evidence
                                    if row["evidence_role"]
                                }
                            ),
                        },
                        "generated_pool": ranking,
                        "displayed_captions": displayed,
                        "recommendation": options.recommended,
                        "selected_candidates": selected_rows,
                        "grounding": {
                            "passed": all(
                                bool(
                                    cast(
                                        dict[str, Any],
                                        row.get("verification", {}),
                                    ).get("passed", False)
                                )
                                for row in selected_rows
                            )
                            if selected_rows
                            else False,
                            "unsupported_claims": [
                                claim
                                for row in selected_rows
                                for claim in cast(
                                    list[str],
                                    cast(
                                        dict[str, Any],
                                        row.get("verification", {}),
                                    ).get("unsupported_claims", []),
                                )
                            ],
                        },
                        "duplicate": duplicate,
                        "abstained": options.abstained,
                        "abstention_reason": options.abstention_reason,
                        "latency_ms": slate.latency_ms,
                        "model_usage": json.loads(slate.token_usage_json),
                        "prompt_version": (model_run.prompt_version if model_run else None),
                        "profile_version": profile["version"],
                        "configuration_hash": slate.configuration_hash,
                    }
                )
            artifact = _evaluation_artifact(
                cases=cases,
                splits=splits,
                purpose=purpose,
                started_at=started_at,
                capture=capture.model_dump(),
                analysis={key: value for key, value in analysis.items()},
                discovery=discovery,
                profile=profile,
                integrity=integrity,
            )
        finally:
            database.engine.dispose()
    write_json(destination / "candidate-results.json", artifact)
    write_markdown(
        destination / "candidate-results.md",
        "RunWay Canonical Intelligence Evaluation",
        artifact,
    )
    if "locked_holdout" in splits:
        write_json(
            destination / "HOLDOUT_USE.json",
            {
                "dataset_version": repository.canonical().dataset_version,
                "used_at": datetime.now(UTC).isoformat(),
                "purpose": purpose,
                "case_ids": [case["case_id"] for case in cases],
                "use_count": 1,
            },
        )
    return artifact


def _evaluation_artifact(
    *,
    cases: list[dict[str, Any]],
    splits: set[EvaluationSplit],
    purpose: EvaluationPurpose,
    started_at: datetime,
    capture: dict[str, object],
    analysis: dict[str, object],
    discovery: dict[str, object],
    profile: dict[str, object],
    integrity: dict[str, object],
) -> dict[str, Any]:
    recommendations = [str(case["recommendation"]) for case in cases if case["recommendation"]]
    displayed = [
        str(caption) for case in cases for caption in cast(list[str], case["displayed_captions"])
    ]
    unsupported = sum(
        len(
            cast(
                list[object],
                cast(dict[str, object], case["grounding"])["unsupported_claims"],
            )
        )
        for case in cases
    )
    metrics = {
        "case_count": len(cases),
        "unique_recommendation_rate": (
            round(len(set(recommendations)) / len(recommendations), 6) if recommendations else 0.0
        ),
        "unique_displayed_caption_rate": (
            round(len(set(displayed)) / len(displayed), 6) if displayed else 0.0
        ),
        "grounding_pass_rate": (
            round(
                sum(bool(cast(dict[str, object], case["grounding"])["passed"]) for case in cases)
                / len(cases),
                6,
            )
            if cases
            else 0.0
        ),
        "unsupported_claim_rate": (round(unsupported / len(cases), 6) if cases else 0.0),
        "abstention_rate": (
            round(sum(bool(case["abstained"]) for case in cases) / len(cases), 6) if cases else 0.0
        ),
        "mean_retrieval_role_coverage": (
            round(
                sum(
                    len(
                        cast(
                            list[object],
                            cast(dict[str, object], case["retrieval"])["role_coverage"],
                        )
                    )
                    for case in cases
                )
                / len(cases),
                6,
            )
            if cases
            else 0.0
        ),
    }
    return {
        "artifact_version": "canonical-candidate-evaluation-v1",
        "dataset_version": "canonical-v1",
        "purpose": purpose,
        "splits": sorted(splits),
        "configuration_hash": configuration_hash(
            {
                "caption": CaptionService.generation_configuration,
                "runtime": "mock",
                "publishing_enabled": False,
            }
        ),
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "offline": True,
        "paid_provider_calls": 0,
        "publishing_mutations": 0,
        "dataset_integrity": integrity,
        "pipeline": {
            "capture": capture,
            "analysis": analysis,
            "discovery": discovery,
            "profile_version": profile["version"],
        },
        "metrics": metrics,
        "cases": cases,
    }
