from __future__ import annotations

import asyncio
import hashlib
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
from sqlalchemy import func, select

from runway.analysis.runtime import AgentRuntimeError, CodexAgentRuntime
from runway.analysis.service import AnalysisService
from runway.capture.browser import BrowserCaptureService, CapturePaused
from runway.capture.service import CaptureService
from runway.catalog.service import CatalogService
from runway.config import get_settings
from runway.db import initialize_database
from runway.discovery.service import DiscoveryService
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
from runway.intelligence.embeddings import RepresentationProviderRegistry
from runway.intelligence.profile import StyleProfileService
from runway.intelligence.retrieval import RetrievalService
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
    return (
        get_settings().project_root
        / "benchmarks"
        / "intelligence"
    )


def _load_json(path: Path) -> dict[str, object]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise typer.BadParameter(f"{path} does not contain a JSON object")
    return value


@intelligence_app.command("baseline")
def intelligence_baseline() -> None:
    """Verify and summarize the immutable pre-upgrade benchmark."""
    root = _intelligence_root() / "baseline-876fe5f"
    failures: list[str] = []
    for line in (root / "checksums.sha256").read_text(encoding="utf-8").splitlines():
        expected, filename = line.split(maxsplit=1)
        path = root / filename.strip()
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
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
    destination = output or (
        _intelligence_root() / "evaluations" / f"canonical-{split}"
    )
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
        isinstance(key, str) and isinstance(value, (int, float))
        for key, value in weights.items()
    ):
        raise typer.BadParameter("tuning winner weights are malformed")
    result = ExperimentRunner().ablate(
        _load_json(candidate),
        winning_weights={
            str(key): float(value) for key, value in weights.items()
        },
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
        str(key): float(value)
        for key, value in weights.items()
        if isinstance(value, (int, float))
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
        (
            row
            for row in cases
            if isinstance(row, dict) and row.get("case_id") == case_id
        ),
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
    labels = {
        row.case_id: row
        for row in DatasetRepository().canonical().blind_preferences
    }
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
            "Creator choices are stored as primary labels; "
            "automated labels were not substituted."
        ),
    }
    destination = output or (
        _intelligence_root() / "experiments" / "blind-review-import.json"
    )
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
                if isinstance(row, dict)
                and isinstance(row.get("final_score"), (int, float))
            ],
            reverse=True,
        )
        gap = scores[0] - scores[1] if len(scores) > 1 else 1.0
        grounding = case.get("grounding")
        unsupported = (
            grounding.get("unsupported_claims", [])
            if isinstance(grounding, dict)
            else []
        )
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
            -row["priority"]
            if isinstance(row["priority"], (int, float))
            else 0.0,
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
    typer.echo(
        json.dumps(ImageGenerationProviderRegistry().status(), indent=2)
    )


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
    return response.status == 200 and isinstance(product, str) and product == "RunWay"


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
                f"{settings.host}:{settings.api_port} already serving RunWay"
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
        raise typer.BadParameter("RunWay may only bind to a loopback host")
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
        channel_url
        or f"https://www.youtube.com/@{settings.channel_handle}/posts"
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
    typer.echo("RunWay never requests or stores your Google password.")
    typer.echo("This operation is read-only and will not create, edit, delete, or publish.")
    typer.echo(
        f"Requested channel: @{settings.channel_handle} — {requested_channel_url}"
    )
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


@discover_app.command("images")
def discover_images(
    days: int = typer.Option(10, min=1, max=30),
    provider: str = typer.Option("fixture", help="fixture, manual, browser, or api"),
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
    days: int = typer.Option(10, min=1, max=30),
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
