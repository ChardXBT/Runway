"""`runway worker`: the background process that runs scheduled and queued jobs.

Also runnable as `python -m runway.cli.worker_commands` (same command, faster
start than the full CLI).
"""

from __future__ import annotations

import logging
import os
import signal
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType

import typer
from sqlalchemy.exc import OperationalError

from runway.config import get_settings
from runway.db.base import Database
from runway.db.bootstrap import DatabaseNotCurrent, require_database_at_head
from runway.jobs.runtime import build_worker
from runway.locks import LockTimeout, exclusive_file_lock
from runway.logging import configure_logging
from runway.operations.revision import checkout_revision
from runway.operations.runtime_files import (
    remove_pid_file,
    worker_lock_path,
    worker_pid_path,
    write_pid_file,
)
from runway.platforms.sync import SyncService

logger = logging.getLogger(__name__)

EXIT_NOT_MIGRATED = 1
EXIT_ALREADY_RUNNING = 3
STALE_SWEEP_ATTEMPTS = 5


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


def sweep_stale_runs(
    service: SyncService,
    *,
    attempts: int = STALE_SWEEP_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Best effort: a locked database (the API is busy) must never stop the worker."""
    for attempt in range(1, attempts + 1):
        try:
            service.mark_stale_runs_failed()
            return
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == attempts:
                logger.warning("stale sync-run sweep skipped: %s", exc)
                return
            sleep(min(0.5 * 2 ** (attempt - 1), 5.0))
        except Exception:  # noqa: BLE001
            logger.exception("stale sync-run sweep failed; the worker continues")
            return


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
    pid_path = worker_pid_path(settings)
    try:
        with exclusive_file_lock(worker_lock_path(settings), timeout=0):
            # Written only while holding the lock, so it always names the live
            # worker; operators and the launcher stop the worker through it.
            write_pid_file(
                pid_path,
                {
                    "pid": os.getpid(),
                    "revision": checkout_revision(),
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
            try:
                worker = build_worker(settings, database)
                sweep_stale_runs(SyncService(database, settings))
                with _stop_on_signals(worker.stop):
                    if once:
                        worker.produce()
                        while not worker.stop_event.is_set() and worker.run_once():
                            pass
                    else:
                        worker.run_forever()
            finally:
                remove_pid_file(pid_path, os.getpid())
    except LockTimeout as exc:
        typer.echo("another runway worker is already running for this data directory", err=True)
        raise typer.Exit(code=EXIT_ALREADY_RUNNING) from exc
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    configure_logging()
    typer.run(worker_command)
