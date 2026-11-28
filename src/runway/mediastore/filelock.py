"""Exclusive advisory file locks that work across threads, store instances and processes.

Same mechanism as :class:`runway.platforms.tokens.TokenStore` (``msvcrt.locking`` on
Windows, ``fcntl.flock`` elsewhere). Each :func:`file_lock` opens its own handle, so two
callers in one process exclude each other exactly as two processes do. The operating system
releases the lock when a process dies, so a crash never leaves an upload locked.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

# How long a blocking acquire waits before giving up.
DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0
_POLL_SECONDS = 0.02


class LockUnavailable(Exception):
    """The lock is held by someone else (non-blocking) or the wait timed out."""


def _try_lock(handle: BinaryIO) -> bool:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(
    path: Path, *, blocking: bool = True, timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` (created if missing) for the ``with`` block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        # msvcrt locks a byte range; make sure the first byte exists.
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while not _try_lock(handle):
            if not blocking or time.monotonic() >= deadline:
                raise LockUnavailable(f"lock {path.name} is held by another writer")
            time.sleep(_POLL_SECONDS)
        try:
            yield
        finally:
            _unlock(handle)
