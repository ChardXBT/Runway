"""Exclusive OS file locks shared by the token store and the worker."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_POLL_SECONDS = 0.05


class LockTimeout(Exception):
    """The lock was still held by someone else when the timeout ran out."""


@contextmanager
def exclusive_file_lock(path: Path, *, timeout: float | None) -> Iterator[None]:
    """Hold an exclusive OS lock on ``path`` across threads and processes.

    Each call opens its own handle, so two threads of one process exclude each
    other as well (Windows byte-range locks and POSIX ``flock`` are per open
    file). ``timeout=None`` waits forever; ``timeout=0`` tries once. The OS
    drops the lock if the holder dies, so a crash never leaves it stuck.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = None if timeout is None else time.monotonic() + timeout
    with path.open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            while True:
                handle.seek(0)
                try:
                    # LK_NBLCK fails at once; LK_LOCK would give up after ~10 s.
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise LockTimeout(f"timed out waiting for {path.name}") from None
                    time.sleep(_POLL_SECONDS)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise LockTimeout(f"timed out waiting for {path.name}") from None
                    time.sleep(_POLL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
