from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass

from .providers.base import HostWindowSpec

_PACKAGED_SHELL_CLASS = "applicationframewindow"


@dataclass(frozen=True)
class HostWindowState:
    """The part of the host desktop app's lifecycle visible to the widget."""

    supported: bool
    running: bool
    window_found: bool
    minimized: bool
    # True when every host window is minimized or cloaked and at least one is
    # cloaked, i.e. the app lives on another virtual desktop.
    on_other_desktop: bool = False
    # Whether a running process without a visible window counts as closed.
    # See HostWindowSpec.require_window for why the providers differ.
    window_required: bool = False

    @property
    def should_show_widget(self) -> bool:
        if not self.supported:
            return True
        # The widget is a Tool window, which Windows shows on every virtual
        # desktop, so follow the host explicitly: hide while it is elsewhere.
        if self.on_other_desktop:
            return False
        if self.window_required:
            # A background helper can outlive the desktop shell. Require a
            # matching top-level window so closing the app hides the rings and
            # a newly opened shell can trigger them to return.
            return self.running and self.window_found and not self.minimized
        # A console/background host may expose no top-level window that
        # EnumWindows can observe. Treat that state as unknown rather than
        # hiding a manually launched widget. Hide only when the app is
        # definitely closed or an observed window is minimized.
        return self.running and (not self.window_found or not self.minimized)


def get_host_window_state(spec: HostWindowSpec) -> HostWindowState:
    """Find the host app's processes and top-level windows on Windows.

    Matching only the executable can miss a packaged MSIX frame, so the
    packaged shell class and title are checked as a fallback signal too.
    """

    if sys.platform != "win32":
        return HostWindowState(
            supported=False, running=False, window_found=False, minimized=False,
            window_required=spec.require_window,
        )

    process_ids = _host_process_ids(spec)
    if not process_ids:
        return HostWindowState(
            supported=True, running=False, window_found=False, minimized=False,
            window_required=spec.require_window,
        )

    windows = _host_windows(spec, process_ids)
    minimized = bool(windows) and all(_USER32.IsIconic(hwnd) for hwnd in windows)
    cloaked = [_is_cloaked(hwnd) for hwnd in windows]
    # Only visible-or-iconic windows are collected, so a cloaked one is a real
    # window sitting on another virtual desktop, not a hidden helper.
    on_other_desktop = any(cloaked) and all(
        c or _USER32.IsIconic(hwnd) for c, hwnd in zip(cloaked, windows)
    )
    return HostWindowState(
        supported=True,
        running=True,
        window_found=bool(windows),
        minimized=minimized,
        on_other_desktop=on_other_desktop,
        window_required=spec.require_window,
    )


def _is_cloaked(hwnd: int) -> bool:
    """Whether DWM hides the window, as it does for other virtual desktops."""

    cloaked = ctypes.c_uint()
    # DWMWA_CLOAKED = 14. A non-zero HRESULT means the query failed; treat that
    # as "not cloaked" so the widget errs towards staying visible.
    result = _DWMAPI.DwmGetWindowAttribute(
        ctypes.c_void_p(hwnd), 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return result == 0 and cloaked.value != 0


def _host_process_ids(spec: HostWindowSpec) -> set[int]:
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
            if name in spec.executable_names:
                process_ids.add(int(entry.th32ProcessID))
            has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return process_ids


def _host_windows(spec: HostWindowSpec, process_ids: set[int]) -> list[int]:
    windows: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def collect(hwnd, _lparam):
        process_id = ctypes.c_ulong()
        _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        title = ctypes.create_unicode_buffer(256)
        window_class = ctypes.create_unicode_buffer(256)
        _USER32.GetWindowTextW(hwnd, title, len(title))
        _USER32.GetClassNameW(hwnd, window_class, len(window_class))
        owned_by_host = process_id.value in process_ids
        # MSIX-packaged Windows apps can put their top-level frame in
        # ApplicationFrameHost.exe instead of the app process. In that case
        # the frame class and title are the reliable lifecycle signals.
        packaged_host_frame = (
            window_class.value.lower() == _PACKAGED_SHELL_CLASS
            and any(token in title.value.lower() for token in spec.title_tokens)
        )
        if (owned_by_host or packaged_host_frame) and (
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
    _DWMAPI = ctypes.windll.dwmapi
else:
    _KERNEL32 = None
    _USER32 = None
    _DWMAPI = None
