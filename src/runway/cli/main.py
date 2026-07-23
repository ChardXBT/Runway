from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from datetime import date
from pathlib import Path

import typer
from PIL import Image, ImageDraw
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine

from runway.analysis.runtime import AgentRuntimeError, CodexAgentRuntime
from runway.analysis.service import AnalysisService
from runway.capture.browser import BrowserCaptureService, CapturePaused
from runway.capture.service import CaptureService
from runway.catalog.service import CatalogService
from runway.config import get_settings
from runway.db import initialize_database
from runway.discovery.service import DiscoveryService
from runway.evaluation.checksums import canonical_text_sha256
from runway.evaluation.datasets import DatasetRepository
from runway.evaluation.experiments import ExperimentRunner
from runway.evaluation.generalization import evaluate_generalization_fixtures
from runway.evaluation.reports import write_json
from runway.evaluation.runner import (
    load_frozen_baseline,
    run_candidate_evaluation,
)
from runway.generation.providers import ImageGenerationProviderRegistry
from runway.generation.schemas import CreativeBrief
from runway.generation.service import ImageGenerationService
from runway.intelligence.candidate_diversity import CandidateDiversityService
from runway.intelligence.embeddings import RepresentationProviderRegistry
from runway.intelligence.profile import StyleProfileService
from runway.intelligence.retrieval import RetrievalService
from runway.intelligence.shadow_editorial import ShadowEditorialService
from runway.logging import configure_logging
from runway.proposals.service import ProposalService
from runway.publishing.youtube import PlaywrightYouTubeAdapter, YouTubeBrowserPublisher

app = typer.Typer(
    name="runway",
    help="Local-only, channel-adaptive Community-post intelligence and planning.",
    no_args_is_help=True,
)
capture_app = typer.Typer(help="One-time, user-initiated historical capture.")
catalog_app = typer.Typer(help="Inspect and verify the canonical local catalogue.")
analyze_app = typer.Typer(help="Analyze historical catalogue records.")
profile_app = typer.Typer(help="Build, inspect, and evaluate versioned style profiles.")
discover_app = typer.Typer(help="Discover and rank candidate images.")
generate_app = typer.Typer(help="Generate proposal batches.")
queue_app = typer.Typer(help="Inspect and operate the approval queue.")
agent_app = typer.Typer(help="Manage the local ChatGPT-authenticated Codex runtime.")
publisher_app = typer.Typer(help="Operate the guarded visible YouTube publisher.")
intelligence_app = typer.Typer(help="Evaluate and inspect the canonical intelligence engine.")
embeddings_app = typer.Typer(help="Inspect or backfill versioned local representations.")
retrieval_app = typer.Typer(help="Inspect persisted hybrid-retrieval evidence.")
image_app = typer.Typer(help="Operate the safe image-generation provider boundary.")
database_app = typer.Typer(help="Inspect and verify the canonical database contract.")
representations_app = typer.Typer(
    help="Plan, backfill, validate, activate, and roll back representation sets."
)
annotations_app = typer.Typer(help="Plan and checkpoint immutable annotation refreshes.")
preference_app = typer.Typer(
    help="Build, train, inspect, activate, and roll back preference models."
)
feedback_app = typer.Typer(help="Reconcile and verify canonical feedback signals.")
runs_app = typer.Typer(help="Inspect persisted intelligence-agent runs.")
study_app = typer.Typer(help="Operate preregistered blind creator studies.")
active_learning_app = typer.Typer(help="Select and export high-information creator-label queues.")
arena_app = typer.Typer(help="Operate the blinded three-arm raw-frontier arena.")
neural_app = typer.Typer(help="Evaluate local neural representation challengers offline.")
composed_app = typer.Typer(help="Prepare learned composed-image retrieval datasets.")

app.add_typer(capture_app, name="capture")
app.add_typer(catalog_app, name="catalog")
app.add_typer(analyze_app, name="analyze")
app.add_typer(profile_app, name="profile")
app.add_typer(discover_app, name="discover")
app.add_typer(generate_app, name="generate")
app.add_typer(queue_app, name="queue")
app.add_typer(agent_app, name="agent")
app.add_typer(publisher_app, name="publisher")
app.add_typer(intelligence_app, name="intelligence")
app.add_typer(embeddings_app, name="embeddings")
app.add_typer(retrieval_app, name="retrieval")
app.add_typer(image_app, name="images")
app.add_typer(database_app, name="database")
intelligence_app.add_typer(representations_app, name="representations")
intelligence_app.add_typer(annotations_app, name="annotations")
intelligence_app.add_typer(preference_app, name="preference")
intelligence_app.add_typer(feedback_app, name="feedback")
intelligence_app.add_typer(runs_app, name="runs")
intelligence_app.add_typer(study_app, name="study")
intelligence_app.add_typer(active_learning_app, name="active-learning")
intelligence_app.add_typer(arena_app, name="arena")
intelligence_app.add_typer(neural_app, name="neural")
intelligence_app.add_typer(composed_app, name="composed-retrieval")


@app.callback()
def root() -> None:
    configure_logging()


@app.command("init")
def initialize() -> None:
    """Create local directories, run migrations, and seed the configured channel."""
    settings = get_settings()
    database = initialize_database(settings)
    typer.echo(f"Initialized {settings.product_name} at {settings.resolved_data_dir}")
    typer.echo(f"Database: {database.settings.database_path}")
    state = "enabled" if settings.publishing_enabled else "disabled"
    typer.echo(f"Publishing: {state}")


def _intelligence_root() -> Path:
    return get_settings().project_root / "benchmarks" / "intelligence"


def _result_integer(result: dict[str, object], key: str) -> int:
    value = result.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"intelligence operation returned invalid {key!r}")
    return value


def _load_json(path: Path) -> dict[str, object]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise typer.BadParameter(f"{path} does not contain a JSON object")
    return value


def _load_json_list_or_field(
    path: Path,
    *,
    field: str,
) -> list[object]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get(field)
    if not isinstance(value, list):
        raise typer.BadParameter(f"{path} must contain a JSON list or an object with {field!r}")
    return value


def _read_only_schema_engine(database_path: Path) -> Engine:
    path = database_path.resolve()
    if not path.is_file():
        raise typer.BadParameter(f"database does not exist: {path}")
    return create_engine(
        f"sqlite+pysqlite:///file:{path.as_posix()}?mode=ro&uri=true",
        future=True,
    )


@database_app.command("schema-status")
def database_schema_status() -> None:
    """Report the current migration and deterministic schema fingerprint."""
    from runway.db.schema_contract import schema_snapshot

    settings = get_settings()
    engine = _read_only_schema_engine(settings.database_path)
    try:
        observed = schema_snapshot(engine)
    finally:
        engine.dispose()
    typer.echo(
        json.dumps(
            {
                "database": str(settings.database_path),
                "migration": observed["migration"],
                "fingerprint": observed["fingerprint"],
                "table_count": len(observed["contract"]["tables"]),  # type: ignore[index]
            },
            indent=2,
            default=str,
        )
    )


@database_app.command("schema-verify")
def database_schema_verify() -> None:
    """Compare the live schema to the committed immutable contract."""
    from runway.db.schema_contract import (
        compare_schema_snapshots,
        load_schema_snapshot,
        schema_snapshot,
    )

    settings = get_settings()
    engine = _read_only_schema_engine(settings.database_path)
    expected = load_schema_snapshot(
        settings.project_root / "docs" / "schema" / "intelligence-data-flywheel.json"
    )
    try:
        observed = schema_snapshot(engine)
    finally:
        engine.dispose()
    result = compare_schema_snapshots(expected, observed)
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result["matches"]:
        raise typer.Exit(code=1)


@database_app.command("intelligence-doctor")
def database_intelligence_doctor(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON.",
    ),
    verify_media_files: bool = typer.Option(
        True,
        "--verify-media-files/--skip-media-files",
        help="Hash active representation source files.",
    ),
) -> None:
    """Audit schema, provenance, representations, learning, and safety state."""
    from runway.intelligence.doctor import IntelligenceDoctor

    settings = get_settings()
    database = initialize_database(settings)
    report = IntelligenceDoctor(
        database,
        settings,
        verify_media_files=verify_media_files,
    ).run()
    typer.echo(
        json.dumps(report.as_dict(), indent=2, default=str) if json_output else report.human_text()
    )
    if report.critical_count:
        raise typer.Exit(code=1)


@representations_app.command("status")
def representation_status(
    set_id: int | None = typer.Option(None, min=1),
) -> None:
    """Show exact set coverage and active resolution state."""
    from runway.intelligence.neural_providers import configured_provider_registry
    from runway.intelligence.representation_sets import RepresentationSetService

    settings = get_settings()
    database = initialize_database(settings)
    service = RepresentationSetService(
        database,
        settings,
        configured_provider_registry(settings),
    )
    result: object = service.status(set_id) if set_id is not None else service.list_sets()
    typer.echo(json.dumps(result, indent=2, default=str))


@representations_app.command("plan")
def representation_plan(
    modality: str = typer.Option(
        ...,
        help="text, image, or multimodal",
    ),
    provider: str = typer.Option("runway-local"),
) -> None:
    """Create an immutable, deterministic historical representation plan."""
    from runway.intelligence.neural_providers import configured_provider_registry
    from runway.intelligence.representation_sets import RepresentationSetService

    if modality not in {"text", "image", "multimodal"}:
        raise typer.BadParameter("modality must be text, image, or multimodal")
    settings = get_settings()
    database = initialize_database(settings)
    try:
        result = RepresentationSetService(
            database,
            settings,
            configured_provider_registry(settings),
        ).plan_history(
            modality,  # type: ignore[arg-type]
            provider_name=provider,
        )
    except (LookupError, ValueError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2, default=str))


@representations_app.command("backfill")
def representation_backfill(
    set_id: int = typer.Option(..., min=1),
    batch_size: int = typer.Option(100, min=1, max=10000),
    until_complete: bool = typer.Option(
        False,
        "--until-complete",
        help="Run bounded batches until no pending item remains.",
    ),
) -> None:
    """Backfill one resumable representation set without activating it."""
    from runway.intelligence.neural_providers import configured_provider_registry
    from runway.intelligence.representation_sets import RepresentationSetService

    settings = get_settings()
    database = initialize_database(settings)
    service = RepresentationSetService(
        database,
        settings,
        configured_provider_registry(settings),
    )
    result = service.backfill(set_id, batch_size=batch_size)
    while (
        until_complete
        and _result_integer(result, "complete") < _result_integer(result, "expected")
        and _result_integer(result, "batch_attempted") > 0
    ):
        result = service.backfill(set_id, batch_size=batch_size)
    typer.echo(json.dumps(result, indent=2, default=str))
    if _result_integer(result, "failed"):
        raise typer.Exit(code=1)


@representations_app.command("verify")
def representation_verify(
    set_id: int = typer.Option(..., min=1),
) -> None:
    """Validate exact coverage, hashes, dimensions, and provider identity."""
    from runway.intelligence.neural_providers import configured_provider_registry
    from runway.intelligence.representation_sets import RepresentationSetService

    settings = get_settings()
    database = initialize_database(settings)
    result = RepresentationSetService(
        database,
        settings,
        configured_provider_registry(settings),
    ).validate(set_id)
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result["valid"]:
        raise typer.Exit(code=1)


@representations_app.command("activate")
def representation_activate(
    set_id: int = typer.Option(..., min=1),
    reason: str = typer.Option(..., min=3),
    gate_results: Path = typer.Option(
        ...,
        exists=True,
        dir_okay=False,
        help="JSON object of explicit evaluation gates; every value must be true.",
    ),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Atomically activate a validated set after explicit evaluation gates."""
    from runway.intelligence.neural_providers import configured_provider_registry
    from runway.intelligence.representation_sets import RepresentationSetService

    gates = _load_json(gate_results)
    if not gates or not all(value is True for value in gates.values()):
        raise typer.BadParameter("every supplied activation gate must be true")
    if not yes and not typer.confirm(
        f"Activate representation set {set_id} and supersede its current set?"
    ):
        raise typer.Abort()
    settings = get_settings()
    database = initialize_database(settings)
    result = RepresentationSetService(
        database,
        settings,
        configured_provider_registry(settings),
    ).activate(set_id, reason=reason, gate_results=gates)
    typer.echo(json.dumps(result, indent=2, default=str))


@representations_app.command("rollback")
def representation_rollback(
    set_id: int = typer.Option(..., min=1),
    to_set_id: int | None = typer.Option(None, min=1),
    reason: str = typer.Option(..., min=3),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Restore a prior complete representation set without deleting evidence."""
    from runway.intelligence.neural_providers import configured_provider_registry
    from runway.intelligence.representation_sets import RepresentationSetService

    if not yes and not typer.confirm(f"Roll back active representation set {set_id}?"):
        raise typer.Abort()
    settings = get_settings()
    database = initialize_database(settings)
    result = RepresentationSetService(
        database,
        settings,
        configured_provider_registry(settings),
    ).rollback(set_id, to_set_id=to_set_id, reason=reason)
    typer.echo(json.dumps(result, indent=2, default=str))


@annotations_app.command("status")
def annotation_refresh_status(
    run_id: int | None = typer.Option(None, min=1),
) -> None:
    """Show annotation-refresh checkpoint and coverage state."""
    from runway.db.models import AnnotationRefreshRun
    from runway.db.repositories import get_channel
    from runway.intelligence.annotation_refresh import AnnotationRefreshService

    settings = get_settings()
    database = initialize_database(settings)
    service = AnnotationRefreshService(database, settings)
    if run_id is not None:
        result: object = service.status(run_id)
    else:
        with database.session() as session:
            channel = get_channel(session, settings.channel_handle)
            ids = list(
                session.scalars(
                    select(AnnotationRefreshRun.id)
                    .where(AnnotationRefreshRun.channel_id == channel.id)
                    .order_by(AnnotationRefreshRun.id.desc())
                )
            )
        result = [service.status(value) for value in ids]
    typer.echo(json.dumps(result, indent=2, default=str))


@annotations_app.command("plan")
def annotation_refresh_plan(
    annotation_version: str = typer.Option(..., min=3),
    prompt_version: str = typer.Option(..., min=3),
) -> None:
    """Plan an immutable annotation refresh without making model calls."""
    from runway.intelligence.annotation_refresh import AnnotationRefreshService

    settings = get_settings()
    database = initialize_database(settings)
    result = AnnotationRefreshService(database, settings).plan(
        annotation_version=annotation_version,
        prompt_version=prompt_version,
    )
    typer.echo(json.dumps(result, indent=2, default=str))


@annotations_app.command("backfill")
def annotation_refresh_backfill(
    run_id: int = typer.Option(..., min=1),
    batch_size: int = typer.Option(5, min=1, max=100),
    allow_model_calls: bool = typer.Option(
        False,
        "--allow-model-calls",
        help="Explicitly authorize configured non-mock allowance use.",
    ),
) -> None:
    """Run one bounded annotation-refresh batch; real model use is gated."""
    from runway.intelligence.annotation_refresh import AnnotationRefreshService

    settings = get_settings()
    database = initialize_database(settings)
    result = asyncio.run(
        AnnotationRefreshService(database, settings).run_batch(
            run_id,
            batch_size=batch_size,
            allow_model_calls=allow_model_calls,
        )
    )
    typer.echo(json.dumps(result, indent=2, default=str))


@annotations_app.command("verify")
def annotation_refresh_verify(
    run_id: int = typer.Option(..., min=1),
) -> None:
    """Verify annotation-refresh coverage without replacing older versions."""
    from runway.intelligence.annotation_refresh import AnnotationRefreshService

    settings = get_settings()
    database = initialize_database(settings)
    result = AnnotationRefreshService(database, settings).validate(run_id)
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result["valid"]:
        raise typer.Exit(code=1)


@preference_app.command("status")
def preference_status() -> None:
    """List persisted target-separated datasets and model versions."""
    from runway.db.models import PreferenceDataset, PreferenceModelVersion
    from runway.db.repositories import get_channel

    settings = get_settings()
    database = initialize_database(settings)
    with database.session() as session:
        channel = get_channel(session, settings.channel_handle)
        datasets = session.scalars(
            select(PreferenceDataset)
            .where(PreferenceDataset.channel_id == channel.id)
            .order_by(PreferenceDataset.created_at.desc())
        ).all()
        models = session.scalars(
            select(PreferenceModelVersion)
            .where(PreferenceModelVersion.channel_id == channel.id)
            .order_by(PreferenceModelVersion.id.desc())
        ).all()
    typer.echo(
        json.dumps(
            {
                "datasets": [
                    {
                        "dataset_id": row.dataset_id,
                        "target": row.target,
                        "status": row.status,
                        "row_count": row.row_count,
                        "content_hash": row.content_hash,
                    }
                    for row in datasets
                ],
                "models": [
                    {
                        "model_version_id": row.id,
                        "target": row.target,
                        "status": row.status,
                        "active": row.active,
                        "dataset_id": row.dataset_id,
                        "label_count": row.label_count,
                        "artifact_hash": row.artifact_hash,
                    }
                    for row in models
                ],
            },
            indent=2,
        )
    )


@preference_app.command("dataset")
def preference_dataset(
    target: str = typer.Option(..., help="caption, image, or pairing"),
    seed: int = typer.Option(20260718),
) -> None:
    """Freeze a reproducible, group-protected preference dataset."""
    from runway.captions.preference_models import PreferenceDatasetService

    settings = get_settings()
    database = initialize_database(settings)
    try:
        result = PreferenceDatasetService(database, settings).build(
            target,
            seed=seed,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2, default=str))


@preference_app.command("train")
def preference_train(
    target: str = typer.Option(..., help="caption, image, or pairing"),
    dataset_id: str | None = typer.Option(None),
    seed: int = typer.Option(20260718),
    minimum_labels: int | None = typer.Option(None, min=1),
) -> None:
    """Train and persist a deterministic pairwise model; never activate it."""
    from runway.captions.preference_models import (
        InsufficientPreferenceData,
        PreferenceModelService,
    )

    settings = get_settings()
    database = initialize_database(settings)
    try:
        result = PreferenceModelService(database, settings).train(
            target,
            dataset_id=dataset_id,
            seed=seed,
            minimum_label_count=minimum_labels,
        )
    except InsufficientPreferenceData as exc:
        typer.echo(json.dumps({"status": "insufficient_data", "error": str(exc)}, indent=2))
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2, default=str))


@preference_app.command("evaluate")
def preference_evaluate(
    model_id: int = typer.Option(..., min=1),
) -> None:
    """Inspect persisted train/validation/test and calibration metrics."""
    from runway.captions.preference_models import PreferenceModelService

    settings = get_settings()
    database = initialize_database(settings)
    result = PreferenceModelService(database, settings).inspect(model_id)
    typer.echo(json.dumps(result, indent=2, default=str))


@preference_app.command("activate")
def preference_activate(
    model_id: int = typer.Option(..., min=1),
    reason: str = typer.Option(..., min=3),
    gate_results: Path = typer.Option(..., exists=True, dir_okay=False),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Atomically activate a qualified model after explicit hard gates."""
    from runway.captions.preference_models import PreferenceModelService

    gates = _load_json(gate_results)
    if not gates or not all(value is True for value in gates.values()):
        raise typer.BadParameter("every supplied activation gate must be true")
    if not yes and not typer.confirm(f"Activate preference model {model_id}?"):
        raise typer.Abort()
    settings = get_settings()
    database = initialize_database(settings)
    result = PreferenceModelService(database, settings).activate(
        model_id,
        reason=reason,
        gate_results={key: bool(value) for key, value in gates.items()},
    )
    typer.echo(json.dumps(result, indent=2, default=str))


@preference_app.command("rollback")
def preference_rollback(
    model_id: int = typer.Option(..., min=1),
    reason: str = typer.Option(..., min=3),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Restore the prior validated model without deleting the challenger."""
    from runway.captions.preference_models import PreferenceModelService

    if not yes and not typer.confirm(f"Roll back preference model {model_id}?"):
        raise typer.Abort()
    settings = get_settings()
    database = initialize_database(settings)
    result = PreferenceModelService(database, settings).rollback(
        model_id,
        reason=reason,
    )
    typer.echo(json.dumps(result, indent=2, default=str))


@feedback_app.command("status")
def intelligence_feedback_status() -> None:
    """Report legacy and normalized target-separated feedback counts."""
    from runway.db.models import CaptionFeedback, FeedbackSignal, Proposal
    from runway.db.repositories import get_channel

    settings = get_settings()
    database = initialize_database(settings)
    with database.session() as session:
        channel = get_channel(session, settings.channel_handle)
        legacy = int(
            session.scalar(
                select(func.count(CaptionFeedback.id))
                .join(Proposal, Proposal.id == CaptionFeedback.proposal_id)
                .where(Proposal.channel_id == channel.id)
            )
            or 0
        )
        targets = {
            str(target): int(count)
            for target, count in session.execute(
                select(FeedbackSignal.target, func.count(FeedbackSignal.id))
                .where(FeedbackSignal.channel_id == channel.id)
                .group_by(FeedbackSignal.target)
            )
        }
    typer.echo(json.dumps({"legacy": legacy, "normalized_by_target": targets}, indent=2))


@feedback_app.command("reconcile")
def intelligence_feedback_reconcile() -> None:
    """Idempotently derive canonical signals from preserved legacy feedback."""
    from runway.captions.feedback import CaptionFeedbackService

    settings = get_settings()
    database = initialize_database(settings)
    result = CaptionFeedbackService(database, settings).reconcile_legacy()
    typer.echo(json.dumps(result, indent=2, default=str))


@feedback_app.command("verify")
def intelligence_feedback_verify() -> None:
    """Run the doctor and return only canonical-feedback findings."""
    from runway.intelligence.doctor import IntelligenceDoctor

    settings = get_settings()
    database = initialize_database(settings)
    report = IntelligenceDoctor(
        database,
        settings,
        verify_media_files=False,
    ).run()
    findings = [item.as_dict() for item in report.findings if item.code.startswith("feedback.")]
    result = {
        "status": (
            "failed" if any(item["severity"] == "critical" for item in findings) else "passed"
        ),
        "findings": findings,
    }
    typer.echo(json.dumps(result, indent=2, default=str))
    if result["status"] == "failed":
        raise typer.Exit(code=1)


@runs_app.command("list")
def intelligence_runs_list(
    limit: int = typer.Option(50, min=1, max=1000),
) -> None:
    """List recent typed agent runs without exposing secret inputs."""
    from runway.db.models import IntelligenceAgentRun
    from runway.db.repositories import get_channel

    settings = get_settings()
    database = initialize_database(settings)
    with database.session() as session:
        channel = get_channel(session, settings.channel_handle)
        rows = session.scalars(
            select(IntelligenceAgentRun)
            .where(IntelligenceAgentRun.channel_id == channel.id)
            .order_by(IntelligenceAgentRun.id.desc())
            .limit(limit)
        ).all()
    typer.echo(
        json.dumps(
            [
                {
                    "id": row.id,
                    "run_key": row.run_key,
                    "capability": row.capability,
                    "provider": row.provider,
                    "model": row.model,
                    "status": row.status,
                    "attempt_count": row.attempt_count,
                    "started_at": row.started_at,
                    "completed_at": row.completed_at,
                }
                for row in rows
            ],
            indent=2,
            default=str,
        )
    )


@runs_app.command("show")
def intelligence_runs_show(
    run_id: int = typer.Option(..., min=1),
) -> None:
    """Inspect typed steps, budgets, usage, errors, and artifact links."""
    from runway.intelligence.agent_harness import IntelligenceAgentHarness

    settings = get_settings()
    database = initialize_database(settings)
    typer.echo(
        json.dumps(
            IntelligenceAgentHarness(database).inspect(run_id),
            indent=2,
            default=str,
        )
    )


@study_app.command("plan")
def intelligence_study_plan(
    cases: Path = typer.Option(..., exists=True, dir_okay=False),
    baseline_identity: str = typer.Option(..., min=1),
    challenger_identity: str = typer.Option(..., min=1),
    target: str = typer.Option("caption"),
    seed: int = typer.Option(20260718),
) -> None:
    """Preregister and persist blinded cases from an explicit JSON plan."""
    from runway.intelligence.studies import BlindStudyCasePlan, BlindStudyService

    if target not in {"caption", "image", "pairing"}:
        raise typer.BadParameter("target must be caption, image, or pairing")
    parsed = [
        BlindStudyCasePlan.model_validate(value)
        for value in _load_json_list_or_field(cases, field="cases")
    ]
    settings = get_settings()
    database = initialize_database(settings)
    result = BlindStudyService(database, settings).plan(
        parsed,
        baseline_identity=baseline_identity,
        challenger_identity=challenger_identity,
        target=target,  # type: ignore[arg-type]
        seed=seed,
    )
    typer.echo(json.dumps(result, indent=2, default=str))


@study_app.command("export")
def intelligence_study_export(
    study_id: int = typer.Option(..., min=1),
    output: Path = typer.Option(...),
    minimum_cases: int = typer.Option(50, min=1),
) -> None:
    """Export a model-origin-blind creator review artifact."""
    from runway.intelligence.studies import BlindStudyService

    settings = get_settings()
    database = initialize_database(settings)
    destination = BlindStudyService(database, settings).export(
        study_id,
        output,
        minimum_cases=minimum_cases,
    )
    typer.echo(str(destination))


@study_app.command("import")
def intelligence_study_import(
    study_id: int = typer.Option(..., min=1),
    review: Path = typer.Option(..., exists=True, dir_okay=False),
    review_session: str = typer.Option(..., min=1),
    reviewer_label: str = typer.Option("local-creator", min=1),
) -> None:
    """Import genuine creator responses; duplicate labels are rejected."""
    from runway.intelligence.studies import (
        BlindStudyImportResponse,
        BlindStudyService,
    )

    parsed = [
        BlindStudyImportResponse.model_validate(value)
        for value in _load_json_list_or_field(review, field="responses")
    ]
    settings = get_settings()
    database = initialize_database(settings)
    result = BlindStudyService(database, settings).import_responses(
        study_id,
        parsed,
        review_session=review_session,
        reviewer_label=reviewer_label,
    )
    typer.echo(json.dumps(result, indent=2, default=str))


@study_app.command("status")
def intelligence_study_status(
    study_id: int = typer.Option(..., min=1),
) -> None:
    """Show case, response, split, and export readiness."""
    from runway.intelligence.studies import BlindStudyService

    settings = get_settings()
    database = initialize_database(settings)
    typer.echo(
        json.dumps(
            BlindStudyService(database, settings).status(study_id),
            indent=2,
            default=str,
        )
    )


@study_app.command("report")
def intelligence_study_report(
    study_id: int = typer.Option(..., min=1),
) -> None:
    """Report descriptive human outcomes without fabricating significance."""
    from runway.intelligence.studies import BlindStudyService

    settings = get_settings()
    database = initialize_database(settings)
    typer.echo(
        json.dumps(
            BlindStudyService(database, settings).report(study_id),
            indent=2,
            default=str,
        )
    )


@active_learning_app.command("select")
def intelligence_active_learning_select(
    cases: Path | None = typer.Option(None, exists=True, dir_okay=False),
    target: str = typer.Option("caption"),
    limit: int = typer.Option(20, min=1, max=1000),
    seed: int = typer.Option(20260718),
) -> None:
    """Select uncertain, disagreeing, diverse cases and persist the queue."""
    from runway.intelligence.studies import (
        ActiveLearningCase,
        ActiveLearningService,
    )

    settings = get_settings()
    database = initialize_database(settings)
    service = ActiveLearningService(database, settings)
    if cases is None:
        if target != "caption":
            raise typer.BadParameter(
                "automatic selection currently supports caption slates; "
                "provide --cases for another target"
            )
        result = service.select_caption_slates(limit=limit, seed=seed)
    else:
        parsed = [
            ActiveLearningCase.model_validate(value)
            for value in _load_json_list_or_field(cases, field="cases")
        ]
        result = service.select(
            parsed,
            target=target,  # type: ignore[arg-type]
            limit=limit,
            seed=seed,
        )
    typer.echo(json.dumps(result, indent=2, default=str))


@active_learning_app.command("export")
def intelligence_active_learning_export(
    batch_id: int = typer.Option(..., min=1),
    output: Path = typer.Option(...),
) -> None:
    """Export a persisted active-learning queue for human labeling."""
    from runway.intelligence.studies import ActiveLearningService

    settings = get_settings()
    database = initialize_database(settings)
    typer.echo(str(ActiveLearningService(database, settings).export(batch_id, output)))


@arena_app.command("plan")
def intelligence_arena_plan(
    cases: Path = typer.Option(..., exists=True, dir_okay=False),
    study_key: str = typer.Option(..., min=1),
    seed: int = typer.Option(20260722),
    target_cases: int = typer.Option(50, min=1, max=1000),
) -> None:
    """Persist a fixed-identity, same-image, three-arm arena plan."""
    from runway.intelligence.frontier_arena import ArenaCasePlan, FrontierArenaService

    parsed = [
        ArenaCasePlan.model_validate(value)
        for value in _load_json_list_or_field(cases, field="cases")
    ]
    settings = get_settings()
    database = initialize_database(settings)
    result = FrontierArenaService(database, settings).plan(
        study_key=study_key,
        cases=parsed,
        seed=seed,
        target_case_count=target_cases,
    )
    typer.echo(json.dumps(result, indent=2, default=str))


@arena_app.command("export")
def intelligence_arena_export(
    study_id: int = typer.Option(..., min=1),
    output: Path = typer.Option(...),
) -> None:
    """Export a provenance-blind three-caption creator review file."""
    from runway.intelligence.frontier_arena import FrontierArenaService

    settings = get_settings()
    database = initialize_database(settings)
    typer.echo(str(FrontierArenaService(database, settings).export(study_id, output)))


@arena_app.command("import")
def intelligence_arena_import(
    study_id: int = typer.Option(..., min=1),
    review: Path = typer.Option(..., exists=True, dir_okay=False),
    review_session: str = typer.Option(..., min=1),
    reviewer_label: str = typer.Option("local-creator", min=1),
) -> None:
    """Import genuine creator arena responses; model origins remain hidden."""
    from runway.intelligence.frontier_arena import (
        ArenaResponseImport,
        FrontierArenaService,
    )

    parsed = [
        ArenaResponseImport.model_validate(value)
        for value in _load_json_list_or_field(review, field="responses")
    ]
    settings = get_settings()
    database = initialize_database(settings)
    result = FrontierArenaService(database, settings).import_responses(
        study_id,
        responses=parsed,
        review_session=review_session,
        reviewer_label=reviewer_label,
    )
    typer.echo(json.dumps(result, indent=2, default=str))


@arena_app.command("status")
def intelligence_arena_status(study_id: int = typer.Option(..., min=1)) -> None:
    from runway.intelligence.frontier_arena import FrontierArenaService

    settings = get_settings()
    database = initialize_database(settings)
    typer.echo(
        json.dumps(
            FrontierArenaService(database, settings).status(study_id),
            indent=2,
            default=str,
        )
    )


@arena_app.command("report")
def intelligence_arena_report(study_id: int = typer.Option(..., min=1)) -> None:
    from runway.intelligence.frontier_arena import FrontierArenaService

    settings = get_settings()
    database = initialize_database(settings)
    typer.echo(
        json.dumps(
            FrontierArenaService(database, settings).report(study_id),
            indent=2,
            default=str,
        )
    )


def _neural_model_paths(values: list[str] | None) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values or []:
        key, separator, raw_path = value.partition("=")
        if not separator or not key.strip() or not raw_path.strip():
            raise typer.BadParameter("model paths must use challenger-key=local-directory")
        result[key.strip()] = Path(raw_path.strip())
    return result


@neural_app.command("readiness")
def intelligence_neural_readiness(
    model_path: list[str] | None = typer.Option(None, "--model-path"),
    output: Path | None = typer.Option(None),
) -> None:
    """Report local challenger readiness without loading or downloading weights."""
    from runway.evaluation.neural_challengers import NeuralChallengerEvaluator

    settings = get_settings()
    evaluator = NeuralChallengerEvaluator(settings)
    report = evaluator.readiness(_neural_model_paths(model_path))
    if output is not None:
        evaluator.write_report(report, output)
    typer.echo(json.dumps(report, indent=2, default=str))


@neural_app.command("evaluate-text")
def intelligence_neural_evaluate_text(
    challenger: str = typer.Option(..., min=1),
    model_path: Path = typer.Option(..., exists=True, file_okay=False),
    revision: str = typer.Option(..., min=1),
    dataset: Path = typer.Option(..., exists=True, dir_okay=False),
    license_verified: bool = typer.Option(False, "--license-verified"),
    output: Path | None = typer.Option(None),
) -> None:
    """Compare a pinned local text encoder with the deterministic baseline."""
    from runway.evaluation.neural_challengers import NeuralChallengerEvaluator

    evaluator = NeuralChallengerEvaluator(get_settings())
    report = evaluator.evaluate_text(
        challenger,
        model_path=model_path,
        revision=revision,
        cases=evaluator.load_text_cases(dataset),
        license_verified=license_verified,
    )
    if output is not None:
        evaluator.write_report(report, output)
    typer.echo(json.dumps(report, indent=2, default=str))


@neural_app.command("evaluate-image-text")
def intelligence_neural_evaluate_image_text(
    challenger: str = typer.Option(..., min=1),
    model_path: Path = typer.Option(..., exists=True, file_okay=False),
    revision: str = typer.Option(..., min=1),
    dataset: Path = typer.Option(..., exists=True, dir_okay=False),
    license_verified: bool = typer.Option(False, "--license-verified"),
    output: Path | None = typer.Option(None),
) -> None:
    """Compare a pinned local aligned encoder with the deterministic baseline."""
    from runway.evaluation.neural_challengers import NeuralChallengerEvaluator

    evaluator = NeuralChallengerEvaluator(get_settings())
    report = evaluator.evaluate_image_text(
        challenger,
        model_path=model_path,
        revision=revision,
        cases=evaluator.load_image_text_cases(dataset),
        license_verified=license_verified,
    )
    if output is not None:
        evaluator.write_report(report, output)
    typer.echo(json.dumps(report, indent=2, default=str))


@composed_app.command("readiness")
def intelligence_composed_readiness() -> None:
    from runway.intelligence.composed_retrieval import ComposedRetrievalService

    settings = get_settings()
    database = initialize_database(settings)
    typer.echo(
        json.dumps(
            ComposedRetrievalService(database, settings).readiness(),
            indent=2,
            default=str,
        )
    )


@composed_app.command("add-example")
def intelligence_composed_add_example(
    reference_media_id: int = typer.Option(..., min=1),
    instruction: str = typer.Option(..., min=1),
    target_media_id: int = typer.Option(..., min=1),
    label_source: str = typer.Option("policy"),
    split: str = typer.Option("development"),
    reviewed: bool = typer.Option(False, "--reviewed"),
) -> None:
    """Add one provenance-labelled reference/instruction/target example."""
    from runway.intelligence.composed_retrieval import ComposedRetrievalService

    settings = get_settings()
    database = initialize_database(settings)
    result = ComposedRetrievalService(database, settings).add_example(
        reference_media_asset_id=reference_media_id,
        modification_instruction=instruction,
        target_media_asset_id=target_media_id,
        label_source=label_source,
        split=split,
        reviewed=reviewed,
        instruction_source={"source": "explicit_cli_input"},
    )
    typer.echo(json.dumps(result, indent=2, default=str))


@intelligence_app.command("hard-negatives-mine")
def intelligence_hard_negatives_mine(
    limit: int = typer.Option(500, min=1, max=10_000),
) -> None:
    """Mine policy-labelled contrasts without creating creator-truth labels."""
    from runway.intelligence.hard_negatives import HardNegativeMiningService

    settings = get_settings()
    database = initialize_database(settings)
    typer.echo(
        json.dumps(
            HardNegativeMiningService(database, settings).mine(limit=limit),
            indent=2,
            default=str,
        )
    )


@intelligence_app.command("exposure-report")
def intelligence_exposure_report() -> None:
    from runway.intelligence.exposure_bias import CandidateExposureService

    settings = get_settings()
    database = initialize_database(settings)
    typer.echo(
        json.dumps(
            CandidateExposureService(database, settings).report(),
            indent=2,
            default=str,
        )
    )


@intelligence_app.command("providers")
def intelligence_providers() -> None:
    """Report deterministic and optional provider readiness without loading weights."""
    from runway.intelligence.neural_providers import provider_diagnostics

    typer.echo(json.dumps(provider_diagnostics(get_settings()), indent=2, default=str))


@intelligence_app.command("provider-status")
def intelligence_provider_status() -> None:
    """Alias for the explicit, no-download provider diagnostic."""
    intelligence_providers()


@intelligence_app.command("baseline")
def intelligence_baseline() -> None:
    """Verify and summarize the immutable pre-upgrade benchmark."""
    root = _intelligence_root() / "baseline-876fe5f"
    failures: list[str] = []
    for line in (root / "checksums.sha256").read_text(encoding="utf-8").splitlines():
        expected, filename = line.split(maxsplit=1)
        path = root / filename.strip()
        observed = canonical_text_sha256(path)
        if observed != expected:
            failures.append(filename.strip())
    payload = load_frozen_baseline()
    result = {
        "status": "verified" if not failures else "failed",
        "directory": str(root),
        "checksum_failures": failures,
        "manifest": payload["manifest"],
        "metrics": payload["metrics"],
    }
    typer.echo(json.dumps(result, indent=2, default=str))
    if failures:
        raise typer.Exit(code=1)


@intelligence_app.command("evaluate")
def intelligence_evaluate(
    split: str = typer.Option(
        "development",
        help="development, tuning, or locked_holdout",
    ),
    output: Path | None = typer.Option(None),
    release_candidate: bool = typer.Option(
        False,
        "--release-candidate",
        help="Required for the one-time locked-holdout run.",
    ),
) -> None:
    """Run deterministic canonical-engine cases without paid calls or publishing."""
    allowed = {"development", "tuning", "locked_holdout"}
    if split not in allowed:
        raise typer.BadParameter(f"split must be one of {', '.join(sorted(allowed))}")
    if split == "locked_holdout" and not release_candidate:
        raise typer.BadParameter(
            "locked_holdout requires --release-candidate after tuning is complete"
        )
    purpose = (
        "holdout_release"
        if split == "locked_holdout"
        else ("tuning" if split == "tuning" else "development")
    )
    destination = output or (_intelligence_root() / "evaluations" / f"canonical-{split}")
    result = asyncio.run(
        run_candidate_evaluation(
            splits={split},  # type: ignore[arg-type]
            purpose=purpose,  # type: ignore[arg-type]
            destination=destination,
        )
    )
    typer.echo(json.dumps(result["metrics"], indent=2))
    typer.echo(f"Artifacts: {destination}")


@intelligence_app.command("compare")
def intelligence_compare(
    candidate: Path = typer.Option(..., exists=True, dir_okay=False),
) -> None:
    """Compare saved canonical results to identical frozen benchmark case IDs."""
    candidate_payload = _load_json(candidate)
    runner = ExperimentRunner()
    result = runner.compare(load_frozen_baseline(), candidate_payload)
    typer.echo(json.dumps(result, indent=2, default=str))


@intelligence_app.command("tune")
def intelligence_tune(
    candidate: Path = typer.Option(..., exists=True, dir_okay=False),
) -> None:
    """Run the bounded, seeded, model-free search on tuning cases only."""
    result = ExperimentRunner().tune(_load_json(candidate))
    typer.echo(json.dumps(result, indent=2, default=str))


@intelligence_app.command("ablate")
def intelligence_ablate(
    candidate: Path = typer.Option(..., exists=True, dir_okay=False),
    tuning_summary: Path = typer.Option(..., exists=True, dir_okay=False),
) -> None:
    """Measure full-engine-minus-component effects on tuning cases."""
    summary = _load_json(tuning_summary)
    winner = summary.get("winner")
    if not isinstance(winner, dict):
        raise typer.BadParameter("tuning summary has no winner")
    configuration = winner.get("configuration")
    if not isinstance(configuration, dict):
        raise typer.BadParameter("tuning winner has no configuration")
    weights = configuration.get("weights")
    if not isinstance(weights, dict) or not all(
        isinstance(key, str) and isinstance(value, (int, float)) for key, value in weights.items()
    ):
        raise typer.BadParameter("tuning winner weights are malformed")
    result = ExperimentRunner().ablate(
        _load_json(candidate),
        winning_weights={str(key): float(value) for key, value in weights.items()},
    )
    typer.echo(json.dumps(result, indent=2, default=str))


@intelligence_app.command("holdout")
def intelligence_holdout(
    candidate: Path = typer.Option(..., exists=True, dir_okay=False),
    tuning_summary: Path = typer.Option(..., exists=True, dir_okay=False),
) -> None:
    """Score the selected release configuration on the locked holdout once."""
    summary = _load_json(tuning_summary)
    winner = summary.get("winner")
    if not isinstance(winner, dict):
        raise typer.BadParameter("tuning summary has no winner")
    configuration = winner.get("configuration")
    weights = configuration.get("weights") if isinstance(configuration, dict) else None
    if not isinstance(weights, dict):
        raise typer.BadParameter("tuning winner weights are malformed")
    numeric_weights = {
        str(key): float(value) for key, value in weights.items() if isinstance(value, (int, float))
    }
    if len(numeric_weights) != len(weights):
        raise typer.BadParameter("tuning winner weights are malformed")
    result = ExperimentRunner().evaluate_holdout(
        _load_json(candidate),
        winning_weights=numeric_weights,
    )
    typer.echo(json.dumps(result, indent=2, default=str))


@intelligence_app.command("generalization")
def intelligence_generalization() -> None:
    """Run the five-channel same-image adaptation fixture."""
    result = asyncio.run(evaluate_generalization_fixtures())
    destination = _intelligence_root() / "experiments" / "generalization.json"
    write_json(destination, result)
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result["passed"]:
        raise typer.Exit(code=1)


@intelligence_app.command("experiments")
def intelligence_experiments() -> None:
    """List preserved experiment artifacts and selection outcomes."""
    root = _intelligence_root() / "experiments"
    rows = []
    for path in sorted(root.glob("exp-*/experiment.json")):
        payload = _load_json(path)
        rows.append(
            {
                "experiment_id": payload.get("experiment_id"),
                "configuration_hash": payload.get("configuration_hash"),
                "selection_decision": payload.get("selection_decision"),
                "metrics": payload.get("metrics"),
                "path": str(path),
            }
        )
    typer.echo(json.dumps({"count": len(rows), "experiments": rows}, indent=2))


@intelligence_app.command("inspect-case")
def intelligence_inspect_case(
    case_id: str = typer.Option(...),
    artifact: Path = typer.Option(..., exists=True, dir_okay=False),
) -> None:
    """Inspect one persisted evaluation case with its evidence and candidates."""
    payload = _load_json(artifact)
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise typer.BadParameter("artifact has no cases")
    match = next(
        (row for row in cases if isinstance(row, dict) and row.get("case_id") == case_id),
        None,
    )
    if match is None:
        raise typer.BadParameter(f"case {case_id} was not found")
    typer.echo(json.dumps(match, indent=2, default=str))


@intelligence_app.command("export-blind-review")
def intelligence_export_blind_review(
    output: Path = typer.Option(...),
    seed: int = typer.Option(20260718),
) -> None:
    """Export deterministically randomized caption pairs without hidden labels."""
    destination = DatasetRepository().export_blind_review(output, seed=seed)
    typer.echo(str(destination))


@intelligence_app.command("import-blind-review")
def intelligence_import_blind_review(
    review: Path = typer.Option(..., exists=True, dir_okay=False),
    output: Path | None = typer.Option(None),
) -> None:
    """Validate creator blind-review choices and preserve a scored report."""
    payload = _load_json(review)
    responses = payload.get("responses")
    if not isinstance(responses, list):
        raise typer.BadParameter("review must contain a responses list")
    labels = {row.case_id: row for row in DatasetRepository().canonical().blind_preferences}
    scored = []
    for response in responses:
        if not isinstance(response, dict):
            raise typer.BadParameter("each blind-review response must be an object")
        case_id = str(response.get("case_id", ""))
        choice = str(response.get("choice", ""))
        if case_id not in labels or choice not in {"first", "second", "tie"}:
            raise typer.BadParameter(f"invalid blind-review response for {case_id}")
        scored.append({"case_id": case_id, "creator_choice": choice})
    result = {
        "dataset_version": DatasetRepository().canonical().dataset_version,
        "response_count": len(scored),
        "responses": scored,
        "note": (
            "Creator choices are stored as primary labels; automated labels were not substituted."
        ),
    }
    destination = output or (_intelligence_root() / "experiments" / "blind-review-import.json")
    write_json(destination, result)
    typer.echo(json.dumps(result, indent=2))


@intelligence_app.command("active-learning")
def intelligence_active_learning(
    artifact: Path = typer.Option(..., exists=True, dir_okay=False),
    limit: int = typer.Option(10, min=1, max=100),
) -> None:
    """Prioritize close-score, uncertain, or abstained cases for creator labels."""
    payload = _load_json(artifact)
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise typer.BadParameter("artifact has no cases")
    rows = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        pool = case.get("generated_pool")
        candidates = pool if isinstance(pool, list) else []
        scores = sorted(
            [
                float(row.get("final_score", 0.0))
                for row in candidates
                if isinstance(row, dict) and isinstance(row.get("final_score"), (int, float))
            ],
            reverse=True,
        )
        gap = scores[0] - scores[1] if len(scores) > 1 else 1.0
        grounding = case.get("grounding")
        unsupported = grounding.get("unsupported_claims", []) if isinstance(grounding, dict) else []
        priority = (
            (1.0 - min(1.0, gap))
            + (1.0 if case.get("abstained") else 0.0)
            + (0.5 if unsupported else 0.0)
        )
        rows.append(
            {
                "case_id": case.get("case_id"),
                "priority": round(priority, 6),
                "top_score_gap": round(gap, 6),
                "abstained": bool(case.get("abstained")),
                "unsupported_claims": unsupported,
            }
        )
    rows.sort(
        key=lambda row: (
            -row["priority"] if isinstance(row["priority"], (int, float)) else 0.0,
            str(row["case_id"]),
        )
    )
    typer.echo(json.dumps({"cases": rows[:limit]}, indent=2))


@embeddings_app.command("status")
def embeddings_status() -> None:
    """Show registered providers and persisted representation counts."""
    from runway.db.models import RepresentationRecord
    from runway.db.repositories import get_channel

    settings = get_settings()
    database = initialize_database(settings)
    with database.session() as session:
        channel = get_channel(session, settings.channel_handle)
        count = int(
            session.scalar(
                select(func.count(RepresentationRecord.id)).where(
                    RepresentationRecord.channel_id == channel.id
                )
            )
            or 0
        )
        purposes = session.execute(
            select(
                RepresentationRecord.purpose,
                func.count(RepresentationRecord.id),
            )
            .where(RepresentationRecord.channel_id == channel.id)
            .group_by(RepresentationRecord.purpose)
        ).all()
    typer.echo(
        json.dumps(
            {
                "providers": RepresentationProviderRegistry().status(),
                "channel_id": channel.id,
                "representation_count": count,
                "purposes": {purpose: value for purpose, value in purposes},
            },
            indent=2,
        )
    )


@embeddings_app.command("backfill")
def embeddings_backfill(
    limit: int = typer.Option(100, min=1, max=10000),
    dry_run: bool = typer.Option(False),
) -> None:
    """Backfill canonical candidate representations through retrieval."""
    from runway.db.models import CandidateImage, SearchRun
    from runway.db.repositories import get_channel

    settings = get_settings()
    database = initialize_database(settings)
    with database.session() as session:
        channel = get_channel(session, settings.channel_handle)
        candidates = session.scalars(
            select(CandidateImage)
            .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
            .where(SearchRun.channel_id == channel.id)
            .order_by(CandidateImage.id)
            .limit(limit)
        ).all()
    if dry_run:
        typer.echo(
            json.dumps(
                {"dry_run": True, "candidate_count": len(candidates)},
                indent=2,
            )
        )
        return
    retrieval = RetrievalService(database, settings)
    run_ids = [
        retrieval.context_for_candidate(
            candidate.media_asset_id,
            candidate_id=candidate.id,
        )["retrieval_run_id"]
        for candidate in candidates
        if not candidate.hard_rejection_reason
    ]
    typer.echo(
        json.dumps(
            {
                "candidate_count": len(candidates),
                "retrieval_runs": run_ids,
                "paid_provider_calls": 0,
            },
            indent=2,
        )
    )


@retrieval_app.command("inspect")
def retrieval_inspect(run_id: int = typer.Option(..., min=1)) -> None:
    """Inspect considered, excluded, fused, and selected evidence."""
    settings = get_settings()
    database = initialize_database(settings)
    result = RetrievalService(database, settings).inspect_run(run_id)
    typer.echo(json.dumps(result, indent=2, default=str))


@image_app.command("providers")
def image_providers() -> None:
    """List explicit image providers and paid/local capability flags."""
    typer.echo(json.dumps(ImageGenerationProviderRegistry().status(), indent=2))


@image_app.command("mock")
def image_mock(
    instruction: str = typer.Option(..., min=3, max=2000),
    output_count: int = typer.Option(1, min=1, max=4),
    seed: int | None = typer.Option(None, min=0),
) -> None:
    """Generate offline deterministic fixtures and re-enter normal validation."""
    settings = get_settings()
    database = initialize_database(settings)
    result = asyncio.run(
        ImageGenerationService(database, settings).generate(
            CreativeBrief(
                capability="text_to_image",
                instruction=instruction,
                seed=seed,
            ),
            provider_name="mock",
            output_count=output_count,
        )
    )
    typer.echo(json.dumps(result, indent=2))


def _command_version(command: str, *args: str) -> str | None:
    executable = shutil.which(command)
    if not executable:
        return None
    result = subprocess.run(
        [executable, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0] if output else "available"


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _runway_api_healthy(host: str, port: int) -> bool:
    url_host = f"[{host}]" if ":" in host else host
    try:
        with urllib.request.urlopen(
            f"http://{url_host}:{port}/health",
            timeout=2,
        ) as response:
            payload: object = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    product = payload.get("product")
    return response.status == 200 and isinstance(product, str) and product.casefold() == "runway"


@app.command()
def doctor() -> None:
    """Check local dependencies and configuration without exposing secrets."""
    settings = get_settings()
    initialize_database(settings)
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Python", sys.version_info >= (3, 11), sys.version.split()[0]))
    node_version = _command_version("node", "--version")
    checks.append(("Node", node_version is not None, node_version or "missing"))
    npm_version = _command_version("npm", "--version")
    checks.append(("npm", npm_version is not None, npm_version or "missing"))
    checks.append(
        (
            "Playwright package",
            importlib.util.find_spec("playwright") is not None,
            "installed" if importlib.util.find_spec("playwright") else "missing",
        )
    )
    browser_detail = "not installed"
    browser_ok = False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            browser_ok = executable.exists()
            browser_detail = str(executable) if browser_ok else "run playwright install chromium"
    except Exception as exc:  # pragma: no cover - environment dependent
        browser_detail = f"unavailable: {type(exc).__name__}"
    checks.append(("Playwright Chromium", browser_ok, browser_detail))
    try:
        chrome = PlaywrightYouTubeAdapter(settings).chrome_executable()
        chrome_ok = True
        chrome_detail = str(chrome)
    except RuntimeError as exc:
        chrome_ok = False
        chrome_detail = str(exc)
    checks.append(("Google Chrome publisher", chrome_ok, chrome_detail))

    writable = all(path.exists() and path.is_dir() for path in settings.ensure_directories())
    checks.append(("Data directories", writable, str(settings.resolved_data_dir)))
    with sqlite3.connect(settings.database_path) as connection:
        migration = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    checks.append(
        ("Database migration", migration is not None, migration[0] if migration else "none")
    )
    if settings.agent_runtime == "codex":
        try:
            codex = CodexAgentRuntime(settings)
            codex_detail = (
                f"{codex.version()}; {codex.login_status()}; "
                f"{codex.model_name}/{codex.reasoning_effort}; API fallback disabled"
            )
            codex_ok = True
        except AgentRuntimeError as exc:
            codex_detail = str(exc)
            codex_ok = False
        checks.append(("Codex runtime", codex_ok, codex_detail))
    else:
        checks.append(
            (
                "Model runtime",
                settings.agent_runtime == "mock"
                or bool(settings.openai_api_key and settings.openai_model),
                (
                    "mock"
                    if settings.agent_runtime == "mock"
                    else "OpenAI configured (secret hidden)"
                ),
            )
        )
    port_available = _port_available(settings.host, settings.api_port)
    api_running = not port_available and _runway_api_healthy(settings.host, settings.api_port)
    checks.append(
        (
            "API port",
            port_available or api_running,
            (
                f"{settings.host}:{settings.api_port} already serving Runway"
                if api_running
                else f"{settings.host}:{settings.api_port}"
            ),
        )
    )

    required_failures = 0
    for name, ok, detail in checks:
        optional = name == "Playwright Chromium" or (
            name == "Google Chrome publisher" and not settings.publishing_enabled
        )
        marker = "OK" if ok else ("WARN" if optional else "FAIL")
        typer.echo(f"[{marker}] {name}: {detail}")
        if not ok and not optional:
            required_failures += 1
    if required_failures:
        raise typer.Exit(code=1)


@agent_app.command("status")
def agent_status() -> None:
    """Verify Codex CLI, ChatGPT authentication, model tier, and fallback policy."""
    settings = get_settings()
    try:
        codex = CodexAgentRuntime(settings)
        result = {
            "provider": codex.provider,
            "cli": codex.version(),
            "authentication": codex.login_status(),
            "model": codex.model_name,
            "reasoning_effort": codex.reasoning_effort,
            "serialized_jobs": True,
            "paid_api_fallback_enabled": False,
        }
    except AgentRuntimeError as exc:
        typer.echo(json.dumps({"status": "blocked", "error": str(exc)}, indent=2))
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(result, indent=2))


@agent_app.command("login")
def agent_login() -> None:
    """Open the official Codex browser sign-in and require ChatGPT authentication."""
    settings = get_settings()
    try:
        codex = CodexAgentRuntime(settings)
    except AgentRuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    code = codex.login_interactive()
    if code:
        raise typer.Exit(code=code)
    try:
        typer.echo(codex.login_status())
    except AgentRuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc


@agent_app.command("smoke")
def agent_smoke() -> None:
    """Run one small structured-image request without touching configured channel data."""
    settings = get_settings()
    smoke_settings = settings.model_copy(update={"agent_runtime": "codex"})
    try:
        codex = CodexAgentRuntime(smoke_settings)
        with tempfile.TemporaryDirectory(prefix="runway-agent-smoke-") as temporary:
            root = Path(temporary)
            image_path = root / "synthetic-reaction.png"
            image = Image.new("RGB", (640, 640), (44, 62, 92))
            draw = ImageDraw.Draw(image)
            draw.ellipse((170, 120, 470, 480), fill=(238, 190, 91))
            draw.ellipse((250, 220, 285, 265), fill=(30, 35, 45))
            draw.ellipse((355, 220, 390, 265), fill=(30, 35, 45))
            draw.arc((250, 260, 390, 390), start=15, end=165, fill=(30, 35, 45), width=12)
            image.save(image_path)
            result = asyncio.run(
                codex.analyze_candidate_image(
                    {
                        "candidate_id": 0,
                        "search_query": "synthetic runtime health check",
                        "source_domain": "local.test",
                        "width": 640,
                        "height": 640,
                        "quality_metrics": {"synthetic": 1.0},
                        "_image_path": str(image_path),
                    }
                )
            )
    except AgentRuntimeError as exc:
        typer.echo(json.dumps({"status": "blocked", "error": str(exc)}, indent=2))
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "status": "passed",
                "provider": codex.provider,
                "model": codex.model_name,
                "reasoning_effort": codex.reasoning_effort,
                "paid_api_fallback_enabled": False,
                "token_usage": codex.last_token_usage,
                "structured_output": result.model_dump(),
            },
            indent=2,
        )
    )


@publisher_app.command("login")
def publisher_login() -> None:
    """Open the dedicated publisher profile for manual channel Editor sign-in."""
    settings = get_settings()
    settings.ensure_directories()
    PlaywrightYouTubeAdapter(settings).login_interactive()
    typer.echo("Publisher profile saved locally. Run `runway publisher status` to validate it.")


@publisher_app.command("status")
def publisher_status() -> None:
    """Validate the feature gate, configured channel, Editor role, and composer."""
    settings = get_settings()
    database = initialize_database(settings)
    result = asyncio.run(YouTubeBrowserPublisher(database, settings).validate_session())
    typer.echo(result.model_dump_json(indent=2))
    if not result.valid:
        raise typer.Exit(code=1)


@publisher_app.command("prepare")
def publisher_prepare(proposal_id: int = typer.Option(..., min=1)) -> None:
    """Create a short-lived, no-submission confirmation for one proposal."""
    settings = get_settings()
    database = initialize_database(settings)
    result = asyncio.run(YouTubeBrowserPublisher(database, settings).prepare_attempt(proposal_id))
    typer.echo(result.model_dump_json(indent=2))
    typer.echo("Nothing was submitted to YouTube.")


@publisher_app.command("confirm")
def publisher_confirm(
    attempt_id: int = typer.Option(..., min=1),
    confirmation_token: str = typer.Option(..., prompt=True, hide_input=True),
    confirmation_phrase: str = typer.Option(..., prompt=True),
) -> None:
    """Submit one prepared attempt after the exact human confirmation."""
    settings = get_settings()
    database = initialize_database(settings)
    result = asyncio.run(
        YouTubeBrowserPublisher(database, settings).confirm_schedule(
            attempt_id,
            confirmation_token=confirmation_token,
            confirmation_phrase=confirmation_phrase,
        )
    )
    typer.echo(result.model_dump_json(indent=2))


@publisher_app.command("verify")
def publisher_verify(proposal_id: int = typer.Option(..., min=1)) -> None:
    """Re-check the configured channel's Scheduled tab without resubmitting."""
    settings = get_settings()
    database = initialize_database(settings)
    result = asyncio.run(
        YouTubeBrowserPublisher(database, settings).verify_scheduled_post(proposal_id)
    )
    typer.echo(result.model_dump_json(indent=2))


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Loopback host only."),
    port: int | None = typer.Option(None, help="API port."),
) -> None:
    """Run the local FastAPI service."""
    import uvicorn

    settings = get_settings()
    bind_host = host or settings.host
    if bind_host not in {"127.0.0.1", "localhost", "::1"}:
        raise typer.BadParameter("Runway may only bind to a loopback host")
    uvicorn.run("runway.api.app:app", host=bind_host, port=port or settings.api_port, reload=False)


@capture_app.command("status")
def capture_status() -> None:
    settings = get_settings()
    database = initialize_database(settings)
    result = CaptureService(database, settings).latest_status()
    if result is None:
        typer.echo(
            "No capture has been run yet. Use `runway capture youtube-posts --fixture --yes`."
        )
        return
    typer.echo(json.dumps(result, indent=2, default=str))


@capture_app.command("youtube-posts")
def capture_youtube_posts(
    channel_url: str | None = typer.Option(
        None,
        help="Requested channel Posts URL; defaults to the configured channel handle.",
    ),
    headed: bool = typer.Option(False, "--headed", help="Launch a visible managed browser."),
    cdp_url: str | None = typer.Option(None, help="Explicit user-started Chrome CDP endpoint."),
    resume: bool = typer.Option(True, "--resume/--no-resume"),
    max_posts: int | None = typer.Option(None, min=1),
    dry_run: bool = typer.Option(False, help="Extract only; write no catalogue data."),
    save_all_snapshots: bool = typer.Option(False),
    delay_ms: int | None = typer.Option(None, min=250),
    fixture: bool = typer.Option(False, help="Use the sanitized offline fixture."),
    yes: bool = typer.Option(False, "--yes", help="Accept the read-only confirmation gate."),
) -> None:
    """Capture Community posts through fixtures or an explicit visible browser."""
    settings = get_settings()
    requested_channel_url = (
        channel_url or f"https://www.youtube.com/@{settings.channel_handle}/posts"
    )
    database = initialize_database(settings)
    service = CaptureService(database, settings)
    if fixture:
        if not yes and not typer.confirm("Run the offline, read-only fixture capture?"):
            raise typer.Abort()
        result = service.run_fixture(
            resume=resume,
            max_posts=max_posts,
            dry_run=dry_run,
            save_all_snapshots=save_all_snapshots,
        )
        typer.echo(result.model_dump_json(indent=2))
        return

    if not headed and not cdp_url:
        raise typer.BadParameter("live capture requires --headed or an explicit --cdp-url")
    typer.echo(f"Dedicated browser profile: {settings.browser_profile_dir}")
    typer.echo("Runway never requests or stores your Google password.")
    typer.echo("This operation is read-only and will not create, edit, delete, or publish.")
    typer.echo(f"Requested channel: @{settings.channel_handle} — {requested_channel_url}")
    typer.echo("Press Ctrl+C once to pause safely; rerun with --resume to continue.")
    if not yes and not typer.confirm("Open the visible browser and begin read-only capture?"):
        raise typer.Abort()
    try:
        result = BrowserCaptureService(service, settings).run(
            requested_channel_url,
            cdp_url=cdp_url,
            resume=resume,
            max_posts=max_posts,
            delay_ms=delay_ms,
            save_all_snapshots=save_all_snapshots,
            progress=typer.echo,
        )
    except CapturePaused as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2) from exc
    typer.echo(result.model_dump_json(indent=2))


@capture_app.command("reparse-snapshots")
def reparse_snapshots(
    run_id: int | None = typer.Option(None, min=1),
    dry_run: bool = typer.Option(False),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Rebuild normalized metadata from immutable captured post-card snapshots."""
    settings = get_settings()
    database = initialize_database(settings)
    if (
        not dry_run
        and not yes
        and not typer.confirm(
            "Reparse saved DOM snapshots and update normalized catalogue metadata?"
        )
    ):
        raise typer.Abort()
    result = CaptureService(database, settings).reparse_snapshots(
        run_id=run_id,
        dry_run=dry_run,
    )
    typer.echo(json.dumps(result, indent=2, default=str))


@catalog_app.command("status")
def catalog_status() -> None:
    settings = get_settings()
    database = initialize_database(settings)
    typer.echo(json.dumps(CatalogService(database, settings).status(), indent=2, default=str))


@catalog_app.command("list")
def catalog_list(
    limit: int = typer.Option(20, min=1, max=500),
    search: str | None = typer.Option(None),
    post_type: str | None = typer.Option(None),
    training_eligible: bool | None = typer.Option(None),
) -> None:
    settings = get_settings()
    database = initialize_database(settings)
    rows = CatalogService(database, settings).list_posts(
        limit=limit,
        search=search,
        post_type=post_type,
        training_eligible=training_eligible,
    )
    typer.echo(json.dumps(rows, indent=2, default=str))


@catalog_app.command("show")
def catalog_show(post_id: int) -> None:
    settings = get_settings()
    database = initialize_database(settings)
    try:
        detail = CatalogService(database, settings).detail(post_id)
    except LookupError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(detail, indent=2, default=str))


def _verify_catalogue() -> None:
    settings = get_settings()
    database = initialize_database(settings)
    report = CatalogService(database, settings).verify(write_reports=True)
    typer.echo(json.dumps(report, indent=2, default=str))
    typer.echo(f"Reports: {settings.resolved_data_dir / 'reports'}")


@catalog_app.command("verify")
def catalog_verify() -> None:
    _verify_catalogue()


@catalog_app.command("export-report")
def catalog_export_report() -> None:
    _verify_catalogue()


@analyze_app.command("history")
def analyze_history(
    resume: bool = typer.Option(True, "--resume/--no-resume"),
    max_posts: int | None = typer.Option(None, min=1),
) -> None:
    settings = get_settings()
    database = initialize_database(settings)
    result = asyncio.run(
        AnalysisService(database, settings).analyze_history(
            resume=resume,
            max_posts=max_posts,
        )
    )
    typer.echo(json.dumps(result, indent=2))


@profile_app.command("build")
def profile_build() -> None:
    settings = get_settings()
    database = initialize_database(settings)
    try:
        result = asyncio.run(StyleProfileService(database, settings).build())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2, default=str))


@profile_app.command("report")
def profile_report() -> None:
    settings = get_settings()
    database = initialize_database(settings)
    try:
        result = StyleProfileService(database, settings).active()
    except LookupError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2, default=str))


@profile_app.command("evaluate")
def profile_evaluate() -> None:
    settings = get_settings()
    database = initialize_database(settings)
    try:
        result = StyleProfileService(database, settings).evaluate()
    except LookupError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2, default=str))


@profile_app.command("content-modes-backfill")
def profile_content_modes_backfill() -> None:
    """Add learned modes to the active profile without invoking a model runtime."""
    settings = get_settings()
    database = initialize_database(settings)
    try:
        result = StyleProfileService(database, settings).backfill_content_modes()
    except (LookupError, ValueError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2, default=str))


@intelligence_app.command("diversity-backfill")
def diversity_backfill(
    reconsider_legacy_policy: bool = typer.Option(
        True,
        "--reconsider-legacy-policy/--keep-legacy-policy-rejections",
    ),
) -> None:
    """Persist candidate clusters and reconsider copyright-only legacy rejections."""
    settings = get_settings()
    database = initialize_database(settings)
    result = CandidateDiversityService(database, settings).backfill(
        reconsider_legacy_policy=reconsider_legacy_policy
    )
    typer.echo(json.dumps(result, indent=2, default=str))


@intelligence_app.command("diversity-report")
def diversity_report() -> None:
    settings = get_settings()
    database = initialize_database(settings)
    result = CandidateDiversityService(database, settings).report()
    typer.echo(json.dumps(result, indent=2, default=str))


@intelligence_app.command("configure-image-policy")
def configure_image_policy() -> None:
    """Apply the creator's public-web, provenance-retaining, NSFW-only policy."""
    settings = get_settings()
    database = initialize_database(settings)
    result = CandidateDiversityService(database, settings).configure_creator_policy()
    typer.echo(json.dumps(result, indent=2, default=str))


@intelligence_app.command("shadow-review")
def shadow_review(
    limit: int = typer.Option(50, min=1, max=500),
) -> None:
    """Create isolated advisory decisions without mutating proposals or creator labels."""
    settings = get_settings()
    database = initialize_database(settings)
    result = asyncio.run(ShadowEditorialService(database, settings).review_pending(limit=limit))
    typer.echo(json.dumps(result, indent=2, default=str))


@intelligence_app.command("shadow-report")
def shadow_report() -> None:
    settings = get_settings()
    database = initialize_database(settings)
    result = ShadowEditorialService(database, settings).report()
    typer.echo(json.dumps(result, indent=2, default=str))


@discover_app.command("images")
def discover_images(
    days: int = typer.Option(10, min=1, max=500),
    provider: str = typer.Option(
        "fixture",
        help=(
            "fixture, manual, archives, duckduckgo, browser, frinkiac, "
            "morbotron, family-guy-wiki, ensemble, or api"
        ),
    ),
    manual_url: list[str] | None = typer.Option(None, "--manual-url"),
    dry_run: bool = typer.Option(False),
    live: bool = typer.Option(False, help="Explicitly allow a configured headed provider."),
) -> None:
    settings = get_settings()
    database = initialize_database(settings)
    try:
        result = asyncio.run(
            DiscoveryService(database, settings).discover(
                days=days,
                provider_name=provider,
                manual_urls=manual_url,
                dry_run=dry_run,
                live=live,
            )
        )
    except (LookupError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2, default=str))


@generate_app.command("batch")
def generate_batch(
    days: int = typer.Option(10, min=1, max=500),
    start_date: str | None = typer.Option(None, help="First local date as YYYY-MM-DD."),
) -> None:
    settings = get_settings()
    database = initialize_database(settings)
    parsed_start: date | None = None
    if start_date:
        try:
            parsed_start = date.fromisoformat(start_date)
        except ValueError as exc:
            raise typer.BadParameter("start-date must be YYYY-MM-DD") from exc
    try:
        result = asyncio.run(
            ProposalService(database, settings).generate_batch(days=days, start_date=parsed_start)
        )
    except (LookupError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2, default=str))


@queue_app.command("status")
def queue_status(
    days: int | None = typer.Option(
        None,
        min=1,
        max=500,
        help="Optional legacy calendar window; omit to list the full schedule.",
    ),
    limit: int = typer.Option(5000, min=1, max=10000),
) -> None:
    settings = get_settings()
    database = initialize_database(settings)
    result = ProposalService(database, settings).queue_status(days=days, limit=limit)
    typer.echo(json.dumps(result, indent=2, default=str))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
