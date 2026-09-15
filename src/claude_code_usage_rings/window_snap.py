from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

SNAP_DISTANCE = 20
MIN_OVERLAP_RATIO = 0.75
PEER_WINDOW_TITLES = frozenset(("Claude Usage", "Codex Usage"))


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def _overlap_ratio(start_a: int, length_a: int, start_b: int, length_b: int) -> float:
    overlap = max(0, min(start_a + length_a, start_b + length_b) - max(start_a, start_b))
    return overlap / max(1, min(length_a, length_b))


def snap_to_peer(
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    own_hwnd: int,
) -> tuple[int, int]:
    """Snap a dragged widget flush to a nearby peer on any of four sides.

    A candidate must have at least 75% overlap along the axis that will be
    aligned and have adjoining edges within ``SNAP_DISTANCE``. Side-by-side
    placements align their top or bottom edges. Stacked placements align
    their left or right edges. The nearest valid candidate wins, so dragging
    near a corner can choose either orientation naturally.
    """

    if sys.platform != "win32":
        return x, y

    user32 = ctypes.windll.user32
    candidates: list[tuple[int, int, int]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd: int, _lparam: int) -> bool:
        if int(hwnd) == own_hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True

        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, len(title))
        if title.value not in PEER_WINDOW_TITLES:
            return True

        rect = _Rect()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True

        peer_left, peer_top = int(rect.left), int(rect.top)
        peer_right, peer_bottom = int(rect.right), int(rect.bottom)

        # Horizontal snapping: left/right sides meet and at least 75% of the
        # vertical span overlaps before the top/bottom edges are aligned.
        if _overlap_ratio(y, height, peer_top, peer_bottom - peer_top) >= MIN_OVERLAP_RATIO:
            aligned_y = min((peer_top, peer_bottom - height), key=lambda candidate: abs(y - candidate))
            right_gap = abs((x + width) - peer_left)
            if right_gap <= SNAP_DISTANCE:
                candidates.append((right_gap + abs(y - aligned_y), peer_left - width, aligned_y))
            left_gap = abs(x - peer_right)
            if left_gap <= SNAP_DISTANCE:
                candidates.append((left_gap + abs(y - aligned_y), peer_right, aligned_y))

        # Vertical snapping: top/bottom sides meet and at least 75% of the
        # horizontal span overlaps before the left/right edges are aligned.
        if _overlap_ratio(x, width, peer_left, peer_right - peer_left) >= MIN_OVERLAP_RATIO:
            aligned_x = min((peer_left, peer_right - width), key=lambda candidate: abs(x - candidate))
            bottom_gap = abs((y + height) - peer_top)
            if bottom_gap <= SNAP_DISTANCE:
                candidates.append((bottom_gap + abs(x - aligned_x), aligned_x, peer_top - height))
            top_gap = abs(y - peer_bottom)
            if top_gap <= SNAP_DISTANCE:
                candidates.append((top_gap + abs(x - aligned_x), aligned_x, peer_bottom))

        return True

    callback = callback_type(visit)
    user32.EnumWindows(callback, 0)
    if not candidates:
        return x, y
    _, snapped_x, snapped_y = min(candidates, key=lambda candidate: candidate[0])
    return snapped_x, snapped_y
