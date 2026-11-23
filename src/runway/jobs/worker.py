"""The `runway worker` loop: claim a due job, run its handler, sleep until the next one."""

from __future__ import annotations

import logging
import os
import socket
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from runway.jobs.queue import DEFAULT_LEASE_SECONDS, Job, JobQueue, LeaseLost
from runway.platforms.base import safe_error_summary

logger = logging.getLogger(__name__)

# Never sleep longer than this, so new jobs from the API and a stop request are
# noticed promptly, yet an idle worker does not poll the database every second.
MAX_IDLE_SECONDS = 30.0

Handler = Callable[[Job], None]


class PermanentJobError(Exception):
    """A handler failure that retrying the same job cannot fix."""


class DeferJob(Exception):  # noqa: N818 - a control signal, not an error
    """Raised by a handler that did no work: run the job again at `run_at`."""

    def __init__(self, run_at: datetime, reason: str) -> None:
        super().__init__(reason)
        self.run_at = run_at
        self.reason = reason


class JobProducer(Protocol):
    """Something that enqueues jobs on a schedule (e.g. scheduled platform sync)."""

    def produce(self, now: datetime) -> None: ...

    def next_due(self, now: datetime) -> datetime | None: ...


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass
class Worker:
    queue: JobQueue
    handlers: Mapping[str, Handler]
    producers: list[JobProducer] = field(default_factory=list)
    worker_id: str = field(default_factory=default_worker_id)
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    max_idle_seconds: float = MAX_IDLE_SECONDS
    stop_event: threading.Event = field(default_factory=threading.Event)

    def stop(self) -> None:
        self.stop_event.set()

    def run_forever(self) -> None:
        logger.info(
            "runway worker start id=%s kinds=%s",
            self.worker_id,
            ",".join(sorted(self.handlers)),
            extra={"event_type": "worker_start"},
        )
        try:
            while not self.stop_event.is_set():
                ran = False
                try:
                    self.produce()
                    ran = self.run_once()
                except Exception:  # noqa: BLE001
                    # A database hiccup (locked, disk) must not kill the loop.
                    logger.exception("worker iteration failed")
                if not ran:
                    self.stop_event.wait(self.idle_seconds())
        finally:
            logger.info(
                "runway worker stop id=%s", self.worker_id, extra={"event_type": "worker_stop"}
            )

    def produce(self) -> None:
        now = self.queue.clock()
        for producer in self.producers:
            try:
                producer.produce(now)
            except Exception:  # noqa: BLE001
                logger.exception("job producer %s failed", type(producer).__name__)

    def idle_seconds(self) -> float:
        """Seconds until the earliest due job or producer slot, capped at 30 s."""
        now = self.queue.clock()
        due = [self.queue.next_run_at(self.handlers.keys())]
        due.extend(producer.next_due(now) for producer in self.producers)
        upcoming = [moment for moment in due if moment is not None]
        if not upcoming:
            return self.max_idle_seconds
        wait = (min(upcoming) - now).total_seconds()
        return min(max(wait, 0.0), self.max_idle_seconds)

    def run_once(self) -> bool:
        """Claim and run one due job. Returns False when nothing was due."""
        job = self.queue.claim(
            self.worker_id, self.handlers.keys(), lease_seconds=self.lease_seconds
        )
        if job is None:
            return False
        handler = self.handlers[job.kind]
        heartbeat = _LeaseHeartbeat(self.queue, job.id, self.worker_id, self.lease_seconds)
        heartbeat.start()
        try:
            handler(job)
        except DeferJob as deferred:
            heartbeat.stop()
            try:
                self.queue.defer(job.id, self.worker_id, deferred.run_at, reason=deferred.reason)
            except LeaseLost:
                logger.error("job %s was deferred after its lease was recovered", job.id)
        except PermanentJobError as exc:
            heartbeat.stop()
            self._fail(job, exc, retryable=False)
        except Exception as exc:  # noqa: BLE001
            heartbeat.stop()
            logger.exception("job %s (%s) failed", job.id, job.kind)
            self._fail(job, exc, retryable=True)
        else:
            heartbeat.stop()
            try:
                self.queue.complete(job.id, self.worker_id)
            except LeaseLost:
                logger.error("job %s finished after its lease was recovered", job.id)
        return True

    def _fail(self, job: Job, exc: BaseException, *, retryable: bool) -> None:
        try:
            state = self.queue.fail(
                job.id, self.worker_id, safe_error_summary(exc), retryable=retryable
            )
        except LeaseLost:
            logger.error("job %s failed after its lease was recovered", job.id)
            return
        logger.warning("job %s (%s) attempt %s -> %s", job.id, job.kind, job.attempts, state)


class _LeaseHeartbeat:
    """Extends a running job's lease so a long handler is not mistaken for a dead one."""

    def __init__(self, queue: JobQueue, job_id: int, worker_id: str, lease_seconds: int) -> None:
        self.queue = queue
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"runway-job-{job_id}-lease", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _run(self) -> None:
        interval = max(self.lease_seconds / 3, 1.0)
        while not self._stop.wait(interval):
            try:
                self.queue.extend_lease(
                    self.job_id, self.worker_id, lease_seconds=self.lease_seconds
                )
            except LeaseLost:
                logger.error("job %s lost its lease while running", self.job_id)
                return
            except Exception:  # noqa: BLE001
                logger.exception("could not extend the lease of job %s", self.job_id)
