"""An OS-level exclusive file lock.

The lock is a byte-range (``msvcrt.locking``) or whole-file (``fcntl.flock``)
lock on a small file. The OS releases it when the holding process exits, so a
crashed holder never leaves the file locked. Concurrent holders in other
processes — the CLI versus the API — serialize on it exactly like threads.

Used to serialize private-instance operations on one clone
(``runway.operations.instance``) and to mark a running Runway
(``runway.operations.runtime_files.worker_lock_path``).
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO


class FileLockTimeout(TimeoutError):
    """The file lock was still held when the timeout expired."""


_LOCK_POLL_SECONDS = 0.05


@contextmanager
def exclusive_file_lock(path: Path, *, timeout: float | None = None) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` for the duration of the ``with`` block.

    ``timeout`` is in seconds: ``None`` waits forever, ``0`` makes a single
    attempt. :class:`FileLockTimeout` is raised when the lock is still held by
    another handle or process once the timeout expires.
    """
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = None if timeout is None else time.monotonic() + timeout
    with lock_path.open("a+b") as handle:
        if sys.platform == "win32":
            _lock_windows(handle, lock_path, deadline)
            try:
                yield
            finally:
                _unlock_windows(handle)
        else:
            _lock_posix(handle, lock_path, deadline)
            try:
                yield
            finally:
                _unlock_posix(handle)


def _retry_or_raise(deadline: float | None, path: Path, exc: OSError) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise FileLockTimeout(f"{path} is still locked by another process") from exc
    time.sleep(_LOCK_POLL_SECONDS)


def _lock_windows(handle: IO[bytes], path: Path, deadline: float | None) -> None:
    import msvcrt

    # msvcrt locks a byte range, so the file must hold at least one byte.
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    while True:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            _retry_or_raise(deadline, path, exc)


def _unlock_windows(handle: IO[bytes]) -> None:
    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _lock_posix(handle: IO[bytes], path: Path, deadline: float | None) -> None:
    import fcntl

    while True:
        try:
            fcntl.flock(  # type: ignore[attr-defined]
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
            )
            return
        except OSError as exc:
            _retry_or_raise(deadline, path, exc)


def _unlock_posix(handle: IO[bytes]) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
