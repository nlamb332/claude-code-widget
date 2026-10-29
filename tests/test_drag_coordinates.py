from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PyQt6 import QtCore

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from usage_rings.rings_window import UsageRingsWindow  # noqa: E402
from usage_rings.window_snap import SnapCandidate  # noqa: E402


class DragCoordinateTests(unittest.TestCase):
    def _window(self, *, peer_hwnd: int | None = None) -> UsageRingsWindow:
        window = UsageRingsWindow.__new__(UsageRingsWindow)
        window._snap_peer_hwnd = peer_hwnd
        window._snap_peer_offset = QtCore.QPoint(110, 0) if peer_hwnd is not None else None
        window.winId = Mock(return_value=1)
        window.move = Mock()
        window._current_window_rect = Mock(return_value=(100, 100, 210, 234))
        window._clear_snap_state = Mock()
        return window

    @patch("usage_rings.rings_window.move_window")
    def test_unsnapped_drag_uses_qt_logical_coordinates(self, move_window: Mock) -> None:
        window = self._window()
        desired = QtCore.QPoint(420, 360)

        window._move_dragged_window(desired)

        window.move.assert_called_once_with(desired)
        move_window.assert_not_called()

    @patch("usage_rings.rings_window.get_window_rect", return_value=(210, 100, 320, 234))
    @patch("usage_rings.rings_window.move_window")
    def test_snapped_drag_keeps_peer_offset_without_reconverting_dragged_window(
        self,
        move_window: Mock,
        get_window_rect: Mock,
    ) -> None:
        window = self._window(peer_hwnd=2)
        desired = QtCore.QPoint(420, 360)

        window._move_dragged_window(desired)

        window.move.assert_called_once_with(desired)
        move_window.assert_called_once_with(2, 530, 360)
        get_window_rect.assert_called_once_with(2)

    @patch("usage_rings.rings_window.move_window")
    @patch("usage_rings.rings_window.get_window_rect", return_value=(210, 100, 320, 234))
    @patch(
        "usage_rings.rings_window.find_snap_candidate",
        return_value=SnapCandidate(210, 100, 2),
    )
    def test_snap_release_places_dragged_window_in_qt_coordinates(
        self,
        find_snap_candidate: Mock,
        get_window_rect: Mock,
        move_window: Mock,
    ) -> None:
        window = self._window()
        desired = QtCore.QPoint(205, 102)

        window._snap_on_release(desired)

        window.move.assert_called_once_with(210, 100)
        move_window.assert_not_called()
        self.assertEqual(window._snap_peer_hwnd, 2)
        self.assertEqual(window._snap_peer_offset, QtCore.QPoint(0, 0))
        find_snap_candidate.assert_called_once_with(205, 102, 110, 134, own_hwnd=1)
        get_window_rect.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
