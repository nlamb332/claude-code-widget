from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass


# The Windows Claude desktop shell ships as Claude.exe. "claude.exe" also
# covers the Claude Code command-line host running as a console process.
_CLAUDE_EXECUTABLE_NAMES = {"claude.exe"}
_PACKAGED_SHELL_CLASS = "applicationframewindow"
_CLAUDE_TITLE_TOKENS = ("claude",)


@dataclass(frozen=True)
class ClaudeWindowState:
    """The part of the Claude desktop lifecycle visible to the widget."""

    supported: bool
    running: bool
    window_found: bool
    minimized: bool

    @property
    def should_show_widget(self) -> bool:
        if not self.supported:
            return True
        # Claude Code can run as a console/background process without exposing
        # a top-level window that EnumWindows can observe. Treat that state as
        # unknown rather than hiding a manually launched widget. Hide only
        # when Claude is definitely closed or an observed window is minimized.
        return self.running and (not self.window_found or not self.minimized)


def get_claude_window_state() -> ClaudeWindowState:
    """Find Claude processes and their top-level windows on Windows.

    Matching only ``claude.exe`` can miss a packaged MSIX frame, so the
    packaged shell class and title are checked as a fallback signal too.
    """

    if sys.platform != "win32":
        return ClaudeWindowState(supported=False, running=False, window_found=False, minimized=False)

    process_ids = _claude_process_ids()
    if not process_ids:
        return ClaudeWindowState(supported=True, running=False, window_found=False, minimized=False)

    windows = _claude_windows(process_ids)
    minimized = bool(windows) and all(_USER32.IsIconic(hwnd) for hwnd in windows)
    return ClaudeWindowState(
        supported=True,
        running=True,
        window_found=bool(windows),
        minimized=minimized,
    )


def _claude_process_ids() -> set[int]:
    kernel32 = _KERNEL32
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot in (0, -1):
        return set()

    process_ids: set[int] = set()
    entry = _ProcessEntry32W(dwSize=ctypes.sizeof(_ProcessEntry32W))
    try:
        has_entry = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while has_entry:
            name = entry.szExeFile.lower()
            if name in _CLAUDE_EXECUTABLE_NAMES:
                process_ids.add(int(entry.th32ProcessID))
            has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return process_ids


def _claude_windows(process_ids: set[int]) -> list[int]:
    windows: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def collect(hwnd, _lparam):
        process_id = ctypes.c_ulong()
        _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        title = ctypes.create_unicode_buffer(256)
        window_class = ctypes.create_unicode_buffer(256)
        _USER32.GetWindowTextW(hwnd, title, len(title))
        _USER32.GetClassNameW(hwnd, window_class, len(window_class))
        owned_by_claude = process_id.value in process_ids
        # MSIX-packaged Windows apps can put their top-level frame in
        # ApplicationFrameHost.exe instead of the app process. In that case
        # the frame class and title are the reliable lifecycle signals.
        packaged_claude_frame = (
            window_class.value.lower() == _PACKAGED_SHELL_CLASS
            and any(token in title.value.lower() for token in _CLAUDE_TITLE_TOKENS)
        )
        if (owned_by_claude or packaged_claude_frame) and (
            _USER32.IsWindowVisible(hwnd) or _USER32.IsIconic(hwnd)
        ):
            windows.append(int(hwnd))
        return True

    _USER32.EnumWindows(collect, 0)
    return windows


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


if sys.platform == "win32":
    _KERNEL32 = ctypes.windll.kernel32
    _USER32 = ctypes.windll.user32
else:
    _KERNEL32 = None
    _USER32 = None
