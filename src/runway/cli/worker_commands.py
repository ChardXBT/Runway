"""`runway worker`: the background process that runs scheduled and queued jobs."""

from __future__ import annotations

import logging
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType

import typer

from runway.config import get_settings
from runway.db.base import Database
from runway.db.bootstrap import DatabaseNotCurrent, require_database_at_head
from runway.jobs.runtime import build_worker, worker_lock_path
from runway.locks import LockTimeout, exclusive_file_lock
from runway.platforms.sync import SyncService

logger = logging.getLogger(__name__)

EXIT_NOT_MIGRATED = 1
EXIT_ALREADY_RUNNING = 3


def _stop_signals() -> list[signal.Signals]:
    signals = [signal.SIGINT, signal.SIGTERM]
    # Ctrl+Break, and what a console process group receives on Windows.
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    return signals


@contextmanager
def _stop_on_signals(stop: Callable[[], None]) -> Iterator[None]:
    def handle(signum: int, _frame: FrameType | None) -> None:
        logger.info("worker stop requested by signal %s", signal.Signals(signum).name)
        stop()

    previous = {sig: signal.signal(sig, handle) for sig in _stop_signals()}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def worker_command(
    once: bool = typer.Option(
        False, "--once", help="Run every job that is due now, then exit instead of waiting."
    ),
) -> None:
    """Run queued jobs (scheduled platform sync) until stopped with Ctrl+C or SIGTERM."""
    settings = get_settings()
    try:
        require_database_at_head(settings)
    except DatabaseNotCurrent as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=EXIT_NOT_MIGRATED) from exc
    database = Database(settings)
    try:
        with exclusive_file_lock(worker_lock_path(settings), timeout=0):
            worker = build_worker(settings, database)
            SyncService(database, settings).mark_stale_runs_failed()
            with _stop_on_signals(worker.stop):
                if once:
                    worker.produce()
                    while not worker.stop_event.is_set() and worker.run_once():
                        pass
                else:
                    worker.run_forever()
    except LockTimeout as exc:
        typer.echo("another runway worker is already running for this data directory", err=True)
        raise typer.Exit(code=EXIT_ALREADY_RUNNING) from exc
    finally:
        database.engine.dispose()
