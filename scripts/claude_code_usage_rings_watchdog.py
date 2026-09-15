#!/usr/bin/env python3
"""Keep the standalone Claude Code usage rings app available across restarts."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WIDGET_SCRIPT = PROJECT_ROOT / "scripts" / "launch_usage_rings.py"
_SUPERVISOR_MUTEX = "Local\\ClaudeCodeUsageRingsSupervisor"
# Must match USER_QUIT_EXIT_CODE in src/claude_code_usage_rings/app.py.
USER_QUIT_EXIT_CODE = 10


def main() -> int:
    mutex = _acquire_supervisor_mutex()
    if mutex == 0:
        return 0

    try:
        while True:
            try:
                creationflags = 0
                if sys.platform == "win32":
                    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                child = subprocess.Popen(
                    [sys.executable, str(WIDGET_SCRIPT)],
                    cwd=str(PROJECT_ROOT),
                    close_fds=True,
                    creationflags=creationflags,
                )
            except OSError:
                # Keep supervising if a transient process or filesystem error
                # prevents one child from starting.
                time.sleep(5)
                continue
            if child.wait() == USER_QUIT_EXIT_CODE:
                # The user quit on purpose; restarting would undo that.
                return 0
            # A normal close or a transient startup failure should not leave
            # the widget unavailable, but avoid a tight respawn loop.
            time.sleep(3)
    finally:
        _release_supervisor_mutex(mutex)


def _acquire_supervisor_mutex() -> int | None:
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, True, _SUPERVISOR_MUTEX)
    if not mutex:
        return None
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(mutex)
        return 0
    return mutex


def _release_supervisor_mutex(mutex: int | None) -> None:
    if mutex:
        ctypes.windll.kernel32.ReleaseMutex(mutex)
        ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
