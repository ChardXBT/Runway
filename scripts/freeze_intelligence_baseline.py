from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import desc, select

from runway.analysis.service import AnalysisService
from runway.captions.service import CaptionService
from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db import initialize_database
from runway.db.models import CandidateImage, ModelRun
from runway.discovery.service import DiscoveryService
from runway.intelligence.profile import StyleProfileService
from runway.ranking.service import ranking_weights_json

BASELINE_COMMIT = "876fe5f814b1a58f0d11b9eaf29f4c508f20595d"
BASELINE_ID = "baseline-876fe5f"
CASE_COUNT = 8


def _json_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _schema_revision(database_path: Path) -> str:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    if row is None:
        raise RuntimeError("baseline database has no Alembic revision")
    return str(row[0])


def _prompt_manifest(root: Path) -> dict[str, str]:
    prompt_dir = root / "src" / "runway" / "analysis" / "prompts"
    return {
        path.name: _file_hash(path)
        for path in sorted(prompt_dir.glob("*.txt"), key=lambda item: item.name)
    }


def _milliseconds(started: datetime, completed: datetime | None) -> float | None:
    if completed is None:
        return None
    return round(max(0.0, (completed - started).total_seconds() * 1000), 3)


async def _freeze(root: Path, output_dir: Path) -> None:
    current_sha = _git_sha(root)
    if current_sha != BASELINE_COMMIT:
        raise RuntimeError(
            f"baseline must run at {BASELINE_COMMIT}; current checkout is {current_sha}"
        )
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite immutable baseline: {output_dir}")

    with tempfile.TemporaryDirectory(
        prefix="runway-intelligence-baseline-",
        ignore_cleanup_errors=True,
    ) as temporary:
        settings = Settings(
            data_dir=Path(temporary) / "data",
            agent_runtime="mock",
            publishing_enabled=False,
            enable_browser_search=False,
        )
        database = initialize_database(settings)
        capture = CaptureService(database, settings).run_fixture()
        analysis = await AnalysisService(database, settings).analyze_history()
        profiles = StyleProfileService(database, settings)
        profile = await profiles.build()
        profile_evaluation = profiles.evaluate()
        discovery_service = DiscoveryService(database, settings)
        discovery = await discovery_service.discover(
            days=10,
            provider_name="fixture",
            dry_run=True,
        )
        candidates = discovery_service.list_candidates(
            run_id=int(discovery["run_id"]),
            accepted_only=True,
            limit=CASE_COUNT,
        )

        cases: list[dict[str, object]] = []
        dataset_cases: list[dict[str, object]] = []
        caption_service = CaptionService(database, settings)
        split_names = ["development"] * 3 + ["tuning"] * 3 + ["locked_holdout"] * 2
        for index, candidate_row in enumerate(candidates[:CASE_COUNT]):
            candidate_id = int(candidate_row["id"])
            options = await caption_service.generate(candidate_id)
            with database.session() as session:
                candidate = session.get(CandidateImage, candidate_id)
                if candidate is None:
                    raise LookupError(f"candidate {candidate_id} disappeared during baseline")
                model_run = session.scalar(
                    select(ModelRun)
                    .where(ModelRun.task_type == "generate_caption_options")
                    .order_by(desc(ModelRun.id))
                    .limit(1)
                )
                if model_run is None:
                    raise LookupError(f"candidate {candidate_id} has no caption model run")
                model_output = cast(dict[str, Any], json.loads(model_run.structured_output_json))
                request = cast(dict[str, Any], json.loads(model_run.request_summary_json))
                analysis_payload = cast(dict[str, Any], json.loads(candidate.detected_topic_json))
                direct_url = candidate.direct_image_url or f"candidate:{candidate_id}"
                case_id = direct_url.removeprefix("fixture://")
                case_id = f"qlob-fixture-{case_id}"
                ranking = cast(list[dict[str, Any]], model_output["ranking"])
                selected_texts = [options.recommended, *options.alternatives]
                selected_rows = [
                    row for row in ranking if str(row.get("text")) in set(selected_texts)
                ]
                cases.append(
                    {
                        "case_id": case_id,
                        "split": split_names[index],
                        "input": {
                            "candidate_analysis": analysis_payload,
                            "image": {
                                "fixture_uri": direct_url,
                                "source_domain": candidate.source_domain,
                                "rights_status": candidate.rights_status,
                                "width": candidate.original_width,
                                "height": candidate.original_height,
                            },
                            "profile_version": profile["version"],
                            "channel": settings.channel_handle,
                        },
                        "retrieval": request["retrieval_context"],
                        "generated_pool": model_output["raw_candidates"]["candidates"],
                        "displayed_captions": selected_texts,
                        "recommendation": options.recommended,
                        "ranking": ranking,
                        "grounding": {
                            "selected": [
                                {
                                    "text": row["text"],
                                    "grounded": row["grounded"],
                                }
                                for row in selected_rows
                            ],
                            "factual_uncertainty_warning": (options.factual_uncertainty_warning),
                        },
                        "duplicate": {
                            "hard_rejection_reason": candidate.hard_rejection_reason,
                            "novelty_score": candidate.novelty_score,
                            "closest_historical_matches": request["retrieval_context"][
                                "visual_examples"
                            ][:5],
                        },
                        "runtime": {
                            "provider": model_run.provider,
                            "model": model_run.model,
                            "prompt_version": model_run.prompt_version,
                            "token_usage": json.loads(model_run.token_usage_json),
                            "latency_ms": _milliseconds(
                                model_run.started_at,
                                model_run.completed_at,
                            ),
                            "error": model_run.error_summary,
                            "abstained": False,
                        },
                    }
                )
                dataset_cases.append(
                    {
                        "case_id": case_id,
                        "split": split_names[index],
                        "channel": settings.channel_handle,
                        "fixture_uri": direct_url,
                        "candidate_analysis": analysis_payload,
                        "rights_status": candidate.rights_status,
                    }
                )

        schema_revision = _schema_revision(settings.database_path)
        config = {
            "agent_runtime": "mock",
            "caption_question_first": settings.caption_question_first,
            "caption_duplicate_threshold": settings.caption_duplicate_threshold,
            "duplicate_perceptual_threshold": settings.duplicate_perceptual_threshold,
            "duplicate_semantic_threshold": settings.duplicate_semantic_threshold,
            "ranking_weights": json.loads(ranking_weights_json()),
            "retrieval_limits": {
                "visual_examples": 8,
                "caption_style_examples": 8,
                "negative_examples": 5,
            },
        }
        recommendations = [str(case["recommendation"]) for case in cases]
        displayed = [
            str(caption)
            for case in cases
            for caption in cast(list[object], case["displayed_captions"])
        ]
        selected_grounding = [
            bool(item["grounded"])
            for case in cases
            for item in cast(dict[str, Any], case["grounding"])["selected"]
        ]
        metrics = {
            "case_count": len(cases),
            "unique_recommendation_rate": (
                round(len(set(recommendations)) / len(recommendations), 6)
                if recommendations
                else 0.0
            ),
            "unique_displayed_caption_rate": (
                round(len(set(displayed)) / len(displayed), 6) if displayed else 0.0
            ),
            "open_question_recommendation_rate": (
                round(
                    sum(value.rstrip().endswith("?") for value in recommendations)
                    / len(recommendations),
                    6,
                )
                if recommendations
                else 0.0
            ),
            "selected_grounding_flag_rate": (
                round(sum(selected_grounding) / len(selected_grounding), 6)
                if selected_grounding
                else 0.0
            ),
            "abstention_rate": 0.0,
            "profile_evaluation": profile_evaluation,
            "fixture_pipeline": {
                "capture_status": capture.status,
                "analysis": analysis,
                "discovery": {
                    "candidates": discovery["candidates"],
                    "accepted": discovery["accepted"],
                    "hard_rejected": discovery["hard_rejected"],
                },
            },
        }
        manifest = {
            "artifact_version": "runway-frozen-intelligence-baseline-v1",
            "baseline_id": BASELINE_ID,
            "baseline_commit": current_sha,
            "schema_revision": schema_revision,
            "profile_schema_version": StyleProfileService.schema_version,
            "profile_version": profile["version"],
            "annotation_version": AnalysisService.annotation_version,
            "configuration": config,
            "configuration_hash": _json_hash(config),
            "prompt_sha256": _prompt_manifest(root),
            "dataset": "dataset.json",
            "outputs": "outputs.json",
            "metrics": "metrics.json",
            "offline": True,
            "publishing_enabled": False,
            "paid_provider_calls": 0,
        }

        output_dir.mkdir(parents=True)
        payloads = {
            "manifest.json": manifest,
            "dataset.json": {
                "dataset_version": "runway-baseline-fixture-v1",
                "cluster_policy": (
                    "Each synthetic fixture URI is one deterministic source cluster; "
                    "clusters do not cross splits."
                ),
                "cases": dataset_cases,
            },
            "outputs.json": {
                "artifact_version": "runway-frozen-intelligence-outputs-v1",
                "baseline_commit": current_sha,
                "cases": cases,
            },
            "metrics.json": metrics,
        }
        for name, payload in payloads.items():
            (output_dir / name).write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        checksums = [f"{_file_hash(output_dir / name)}  {name}" for name in sorted(payloads)]
        (output_dir / "checksums.sha256").write_text(
            "\n".join(checksums) + "\n",
            encoding="utf-8",
        )
        (output_dir / "README.md").write_text(
            "\n".join(
                [
                    "# Frozen RunWay intelligence baseline",
                    "",
                    f"- Baseline commit: `{current_sha}`",
                    f"- Schema revision: `{schema_revision}`",
                    f"- Cases: {len(cases)}",
                    "- Runtime: deterministic offline mock",
                    "- Network, paid providers, browsers, and publishing: disabled",
                    "",
                    "These files are immutable comparison evidence. The freeze command refuses "
                    "to overwrite this directory and refuses to run from another commit.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        database.engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze the pre-upgrade intelligence benchmark.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks") / "intelligence" / BASELINE_ID,
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    asyncio.run(_freeze(root, (root / args.output).resolve()))
    print(f"Frozen baseline written to {(root / args.output).resolve()}")


if __name__ == "__main__":
    main()
