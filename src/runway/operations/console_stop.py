"""Ask a console process to stop cleanly, as if Ctrl+Break was pressed in its window.

The launcher starts the API and the worker hidden, each in its own console.
Windows has no SIGTERM, so the clean way to stop them from outside is a console
control event: uvicorn and `runway worker` both handle SIGBREAK and shut down
gracefully (finish requests, run shutdown hooks, release locks). Ctrl+Break,
not Ctrl+C, because a process started with Ctrl+C disabled (for example below
a CREATE_NEW_PROCESS_GROUP parent such as a scheduler or supervisor) passes that
setting to its children and would ignore CTRL_C_EVENT; CTRL_BREAK_EVENT cannot
be disabled. This must run in its own short-lived process, because attaching
to another console detaches the caller from its own.

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
CTRL_BREAK_EVENT = 1


def request_console_stop(pid: int) -> bool:
    if sys.platform != "win32":
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # The event goes to every process on the target console, this one included
    # once attached. A handler that claims every event keeps this process alive
    # (SIG_IGN and SetConsoleCtrlHandler(NULL) cover Ctrl+C only).
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    swallow = handler_type(lambda _event: True)
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        return False
    try:
        kernel32.SetConsoleCtrlHandler(swallow, True)
        sent = bool(kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, 0))
        # Delivery is asynchronous; stay attached long enough for it to land.
        time.sleep(0.5)
        return sent
    finally:
        kernel32.FreeConsole()
        kernel32.SetConsoleCtrlHandler(swallow, False)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not argv[0].isdigit():
        print("usage: python -m runway.operations.console_stop <pid>", file=sys.stderr)
        return EXIT_NOT_ATTACHED
    return EXIT_SENT if request_console_stop(int(argv[0])) else EXIT_NOT_ATTACHED


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
