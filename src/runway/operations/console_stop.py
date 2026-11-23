"""Ask a console process to stop cleanly, as if Ctrl+C was pressed in its window.

The launcher starts the API and the worker hidden, each in its own console.
Windows has no SIGTERM, so the clean way to stop them from outside is a
CTRL_C_EVENT on that console: uvicorn and `runway worker` treat it as SIGINT
and shut down gracefully (finish requests, run shutdown hooks, release locks).
This must run in its own short-lived process, because attaching to another
console detaches the caller from its own.

Usage: python -m runway.operations.console_stop <pid>
Exit codes: 0 event sent, 2 could not attach to the process's console.
"""

from __future__ import annotations

import os
import signal
import sys
import time

EXIT_SENT = 0
EXIT_NOT_ATTACHED = 2
CTRL_C_EVENT = 0


def request_console_stop(pid: int) -> bool:
    if sys.platform != "win32":
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            return False
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # The event goes to every process on the target console, this one included
    # once attached; ignore it here.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        return False
    try:
        kernel32.SetConsoleCtrlHandler(None, True)
        sent = bool(kernel32.GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0))
        # Delivery is asynchronous; stay attached long enough for it to land.
        time.sleep(0.5)
        return sent
    finally:
        kernel32.FreeConsole()


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not argv[0].isdigit():
        print("usage: python -m runway.operations.console_stop <pid>", file=sys.stderr)
        return EXIT_NOT_ATTACHED
    return EXIT_SENT if request_console_stop(int(argv[0])) else EXIT_NOT_ATTACHED


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
