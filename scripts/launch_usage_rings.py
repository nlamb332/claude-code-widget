#!/usr/bin/env python3
from __future__ import annotations

import sys
import ctypes
import subprocess
from ctypes import wintypes
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

for path in (PROJECT_ROOT, SRC_DIR):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)

from claude_code_usage_rings.app import main


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _desktop_name(handle: int) -> str | None:
    user32 = ctypes.windll.user32
    buffer = ctypes.create_unicode_buffer(256)
    returned = ctypes.c_ulong()
    if not user32.GetUserObjectInformationW(
        handle,
        2,  # UOI_NAME
        buffer,
        ctypes.sizeof(buffer),
        ctypes.byref(returned),
    ):
        return None
    return buffer.value or None


def _input_desktop_name() -> str | None:
    user32 = ctypes.windll.user32
    handle = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_READOBJECTS
    if not handle:
        return None
    try:
        return _desktop_name(handle)
    finally:
        user32.CloseDesktop(handle)


def _current_desktop_name() -> str | None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    handle = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
    return _desktop_name(handle) if handle else None


def _relay_to_interactive_desktop() -> int | None:
    """Run the GUI on the user's desktop when launched from an isolated shell.

    Returns the relayed child's exit code, or None when no relay was needed.
    """

    if sys.platform != "win32":
        return None

    current = _current_desktop_name()
    target = _input_desktop_name() or "Default"
    if current is None or current.casefold() == target.casefold():
        return None

    command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
    startup_info = _StartupInfo(cb=ctypes.sizeof(_StartupInfo))
    startup_info.lpDesktop = f"winsta0\\{target}"
    process_info = _ProcessInformation()
    creation_flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    created = ctypes.windll.kernel32.CreateProcessW(
        sys.executable,
        command_line,
        None,
        None,
        False,
        creation_flags,
        None,
        str(PROJECT_ROOT),
        ctypes.byref(startup_info),
        ctypes.byref(process_info),
    )
    if not created:
        raise ctypes.WinError()
    # Keep the launcher alive while the interactive child owns the widget.
    # This also lets the watchdog supervise the real GUI process.
    ctypes.windll.kernel32.CloseHandle(process_info.hThread)
    ctypes.windll.kernel32.WaitForSingleObject(process_info.hProcess, 0xFFFFFFFF)
    # Pass the child's exit code through so a deliberate quit reaches the watchdog.
    exit_code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(exit_code))
    ctypes.windll.kernel32.CloseHandle(process_info.hProcess)
    return int(exit_code.value)


if __name__ == "__main__":
    relayed_exit_code = _relay_to_interactive_desktop()
    if relayed_exit_code is not None:
        raise SystemExit(relayed_exit_code)
    raise SystemExit(main())
