"""Named-mutex helpers shared by the widget, its watchdog, and the startup script."""

from __future__ import annotations

import ctypes
import sys

_ERROR_ALREADY_EXISTS = 183
_SYNCHRONIZE = 0x00100000


def acquire(name: str) -> int | None:
    """Take the named mutex.

    Returns the handle, ``0`` when another process already holds it, or
    ``None`` where mutexes are unavailable (non-Windows, or creation failed)
    so callers can carry on unguarded.
    """

    if sys.platform != "win32":
        return None
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, True, name)
    if not mutex:
        return None
    if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(mutex)
        return 0
    return mutex


def release(mutex: int | None) -> None:
    if mutex:
        ctypes.windll.kernel32.ReleaseMutex(mutex)
        ctypes.windll.kernel32.CloseHandle(mutex)


def is_held(name: str) -> bool:
    """Whether some process currently owns the named mutex, without taking it."""

    if sys.platform != "win32":
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, name)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True
