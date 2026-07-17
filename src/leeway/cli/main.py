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
from datetime import date
from pathlib import Path

import typer
from PIL import Image, ImageDraw

from leeway.analysis.runtime import AgentRuntimeError, CodexAgentRuntime
from leeway.analysis.service import AnalysisService
from leeway.capture.browser import BrowserCaptureService, CapturePaused
from leeway.capture.service import CaptureService
from leeway.catalog.service import CatalogService
from leeway.config import get_settings
from leeway.db import initialize_database
from leeway.discovery.service import DiscoveryService
from leeway.intelligence.profile import StyleProfileService
from leeway.logging import configure_logging
from leeway.proposals.service import ProposalService
from leeway.publishing.youtube import PlaywrightYouTubeAdapter, YouTubeBrowserPublisher

app = typer.Typer(
    name="leeway",
    help="Local-only Qlob Community-post intelligence and planning.",
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

app.add_typer(capture_app, name="capture")
app.add_typer(catalog_app, name="catalog")
app.add_typer(analyze_app, name="analyze")
app.add_typer(profile_app, name="profile")
app.add_typer(discover_app, name="discover")
app.add_typer(generate_app, name="generate")
app.add_typer(queue_app, name="queue")
app.add_typer(agent_app, name="agent")
app.add_typer(publisher_app, name="publisher")


@app.callback()
def root() -> None:
    configure_logging()


@app.command("init")
def initialize() -> None:
    """Create local directories, run migrations, and seed the Qlob channel."""
    settings = get_settings()
    database = initialize_database(settings)
    typer.echo(f"Initialized {settings.product_name} at {settings.resolved_data_dir}")
    typer.echo(f"Database: {database.settings.database_path}")
    typer.echo("Publishing: disabled")


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
    checks.append(
        (
            "API port",
            _port_available(settings.host, settings.api_port),
            f"{settings.host}:{settings.api_port}",
        )
    )

    required_failures = 0
    for name, ok, detail in checks:
        optional = name == "Playwright Chromium"
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
    """Run one small Luna/low structured-image request without touching the Qlob database."""
    settings = get_settings()
    smoke_settings = settings.model_copy(update={"agent_runtime": "codex"})
    try:
        codex = CodexAgentRuntime(smoke_settings)
        with tempfile.TemporaryDirectory(prefix="leeway-agent-smoke-") as temporary:
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
    """Open the dedicated publisher profile for manual Qlob Editor sign-in."""
    settings = get_settings()
    settings.ensure_directories()
    PlaywrightYouTubeAdapter(settings).login_interactive()
    typer.echo("Publisher profile saved locally. Run `leeway publisher status` to validate it.")


@publisher_app.command("status")
def publisher_status() -> None:
    """Validate the feature gate, Qlob channel, Editor role, and composer."""
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
    """Re-check Qlob's Scheduled tab without resubmitting."""
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
        raise typer.BadParameter("Leeway may only bind to a loopback host")
    uvicorn.run("leeway.api.app:app", host=bind_host, port=port or settings.api_port, reload=False)


@capture_app.command("status")
def capture_status() -> None:
    settings = get_settings()
    database = initialize_database(settings)
    result = CaptureService(database, settings).latest_status()
    if result is None:
        typer.echo(
            "No capture has been run yet. Use `leeway capture youtube-posts --fixture --yes`."
        )
        return
    typer.echo(json.dumps(result, indent=2, default=str))


@capture_app.command("youtube-posts")
def capture_youtube_posts(
    channel_url: str = typer.Option(
        "https://www.youtube.com/@Qlob/posts", help="Requested channel Posts URL."
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
    typer.echo("Leeway never requests or stores your Google password.")
    typer.echo("This operation is read-only and will not create, edit, delete, or publish.")
    typer.echo(f"Requested channel: @{settings.channel_handle} — {channel_url}")
    typer.echo("Press Ctrl+C once to pause safely; rerun with --resume to continue.")
    if not yes and not typer.confirm("Open the visible browser and begin read-only capture?"):
        raise typer.Abort()
    try:
        result = BrowserCaptureService(service, settings).run(
            channel_url,
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
def queue_status(days: int = typer.Option(10, min=1, max=30)) -> None:
    settings = get_settings()
    database = initialize_database(settings)
    result = ProposalService(database, settings).queue_status(days=days)
    typer.echo(json.dumps(result, indent=2, default=str))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
