from __future__ import annotations

import ctypes
import sys
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass

SNAP_DISTANCE = 20
MIN_OVERLAP_RATIO = 0.75
CONNECTED_TOLERANCE = 3
PEER_WINDOW_TITLES = frozenset(("Claude Usage", "Codex Usage"))
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
_DPI_AWARENESS_CONTEXT_UNAWARE = ctypes.c_void_p(-1)


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


@contextmanager
def _logical_window_coordinates():
    """Make Win32 window geometry use the same logical pixels as Qt.

    Qt uses device-independent coordinates for mouse events and QWidget
    geometry. On a scaled Windows desktop, a per-monitor-aware process would
    otherwise receive physical-pixel rectangles from Win32, so snap tests
    would compare values in different coordinate systems.
    """

    if sys.platform != "win32":
        yield
        return

    set_thread_dpi_context = getattr(ctypes.windll.user32, "SetThreadDpiAwarenessContext", None)
    if set_thread_dpi_context is None:
        yield
        return

    set_thread_dpi_context.argtypes = [wintypes.HANDLE]
    set_thread_dpi_context.restype = wintypes.HANDLE
    previous_context = set_thread_dpi_context(_DPI_AWARENESS_CONTEXT_UNAWARE)
    try:
        yield
    finally:
        if previous_context:
            set_thread_dpi_context(previous_context)


@dataclass(frozen=True)
class SnapCandidate:
    x: int
    y: int
    peer_hwnd: int


def _overlap_ratio(start_a: int, length_a: int, start_b: int, length_b: int) -> float:
    overlap = max(0, min(start_a + length_a, start_b + length_b) - max(start_a, start_b))
    return overlap / max(1, min(length_a, length_b))


def get_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if sys.platform != "win32" or not hwnd:
        return None
    with _logical_window_coordinates():
        rect = _Rect()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def _peer_windows(own_hwnd: int) -> list[tuple[int, tuple[int, int, int, int]]]:
    if sys.platform != "win32":
        return []

    user32 = ctypes.windll.user32
    peers: list[tuple[int, tuple[int, int, int, int]]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd: int, _lparam: int) -> bool:
        if int(hwnd) == own_hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True

        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, len(title))
        if title.value not in PEER_WINDOW_TITLES:
            return True

        rect = get_window_rect(int(hwnd))
        if rect is not None:
            peers.append((int(hwnd), rect))
        return True

    callback = callback_type(visit)
    user32.EnumWindows(callback, 0)
    return peers


def windows_are_connected(
    own_rect: tuple[int, int, int, int],
    peer_rect: tuple[int, int, int, int],
) -> bool:
    own_left, own_top, own_right, own_bottom = own_rect
    peer_left, peer_top, peer_right, peer_bottom = peer_rect
    horizontal_boundary = abs(own_right - peer_left) <= CONNECTED_TOLERANCE or abs(own_left - peer_right) <= CONNECTED_TOLERANCE
    vertical_boundary = abs(own_bottom - peer_top) <= CONNECTED_TOLERANCE or abs(own_top - peer_bottom) <= CONNECTED_TOLERANCE
    vertical_overlap = _overlap_ratio(own_top, own_bottom - own_top, peer_top, peer_bottom - peer_top)
    horizontal_overlap = _overlap_ratio(own_left, own_right - own_left, peer_left, peer_right - peer_left)
    horizontal_alignment = abs(own_top - peer_top) <= CONNECTED_TOLERANCE or abs(own_bottom - peer_bottom) <= CONNECTED_TOLERANCE
    vertical_alignment = abs(own_left - peer_left) <= CONNECTED_TOLERANCE or abs(own_right - peer_right) <= CONNECTED_TOLERANCE
    return (
        horizontal_boundary
        and vertical_overlap >= MIN_OVERLAP_RATIO
        and horizontal_alignment
    ) or (
        vertical_boundary
        and horizontal_overlap >= MIN_OVERLAP_RATIO
        and vertical_alignment
    )


def find_connected_peer(own_hwnd: int) -> int | None:
    own_rect = get_window_rect(own_hwnd)
    if own_rect is None:
        return None
    for peer_hwnd, peer_rect in _peer_windows(own_hwnd):
        if windows_are_connected(own_rect, peer_rect):
            return peer_hwnd
    return None


def find_snap_candidate(
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    own_hwnd: int,
) -> SnapCandidate | None:
    candidates: list[tuple[int, int, int, int]] = []
    for peer_hwnd, peer_rect in _peer_windows(own_hwnd):
        peer_left, peer_top, peer_right, peer_bottom = peer_rect

        # Horizontal snapping: left/right sides meet and at least 75% of the
        # vertical span overlaps before the top/bottom edges are aligned.
        if _overlap_ratio(y, height, peer_top, peer_bottom - peer_top) >= MIN_OVERLAP_RATIO:
            aligned_y = min((peer_top, peer_bottom - height), key=lambda candidate: abs(y - candidate))
            right_gap = abs((x + width) - peer_left)
            if right_gap <= SNAP_DISTANCE:
                candidates.append((right_gap + abs(y - aligned_y), peer_left - width, aligned_y, peer_hwnd))
            left_gap = abs(x - peer_right)
            if left_gap <= SNAP_DISTANCE:
                candidates.append((left_gap + abs(y - aligned_y), peer_right, aligned_y, peer_hwnd))

        # Vertical snapping: top/bottom sides meet and at least 75% of the
        # horizontal span overlaps before the left/right edges are aligned.
        if _overlap_ratio(x, width, peer_left, peer_right - peer_left) >= MIN_OVERLAP_RATIO:
            aligned_x = min((peer_left, peer_right - width), key=lambda candidate: abs(x - candidate))
            bottom_gap = abs((y + height) - peer_top)
            if bottom_gap <= SNAP_DISTANCE:
                candidates.append((bottom_gap + abs(x - aligned_x), aligned_x, peer_top - height, peer_hwnd))
            top_gap = abs(y - peer_bottom)
            if top_gap <= SNAP_DISTANCE:
                candidates.append((top_gap + abs(x - aligned_x), aligned_x, peer_bottom, peer_hwnd))

    if not candidates:
        return None
    _, snapped_x, snapped_y, peer_hwnd = min(candidates, key=lambda candidate: candidate[0])
    return SnapCandidate(snapped_x, snapped_y, peer_hwnd)


def snap_to_peer(
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    own_hwnd: int,
) -> tuple[int, int]:
    candidate = find_snap_candidate(x, y, width, height, own_hwnd=own_hwnd)
    return (candidate.x, candidate.y) if candidate is not None else (x, y)


def move_window(hwnd: int, x: int, y: int) -> bool:
    if sys.platform != "win32" or not hwnd:
        return False
    with _logical_window_coordinates():
        return bool(
            ctypes.windll.user32.SetWindowPos(
                hwnd,
                0,
                int(x),
                int(y),
                0,
                0,
                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
            )
        )
