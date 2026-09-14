from __future__ import annotations

import argparse
import ctypes
import os
import sys
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

from .account_usage import DEFAULT_AUTH_FILE, DEFAULT_BASE_URL
from .models import UsageCardModel

from .rings_window import APP_NAME, UsageRingsWindow


class UsageRingsTray(QtWidgets.QSystemTrayIcon):
    """Status icon and small control menu for the borderless rings window."""

    def __init__(self, window: UsageRingsWindow, app: QtWidgets.QApplication) -> None:
        super().__init__(app)
        self._window = window
        self.setToolTip(f"{APP_NAME} Usage Rings")
        self._window.setWindowIcon(self._make_icon(None, None))
        self.setContextMenu(self._build_menu())
        self.activated.connect(self._handle_activation)
        window.usage_changed.connect(self._update_icon)
        window.glass_mode_changed.connect(self._sync_glass_action)
        self._sync_glass_action(window.glass_mode)
        self._update_icon(None)

    def _build_menu(self) -> QtWidgets.QMenu:
        menu = QtWidgets.QMenu()
        show_action = menu.addAction("Show usage rings")
        show_action.triggered.connect(self._show_window)
        hide_action = menu.addAction("Hide usage rings")
        hide_action.triggered.connect(self._window.hide)
        menu.addSeparator()
        self._glass_action = menu.addAction("Glass background")
        self._glass_action.setCheckable(True)
        self._glass_action.triggered.connect(self._window.set_glass_mode)
        return menu

    @QtCore.pyqtSlot(bool)
    def _sync_glass_action(self, enabled: bool) -> None:
        self._glass_action.setChecked(enabled)

    def _show_window(self) -> None:
        self._window.showNormal()
        self._window._place_initially()
        self._window._raise_without_focus()

    def _handle_activation(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QtWidgets.QSystemTrayIcon.ActivationReason.Trigger,
            QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show_window()

    @QtCore.pyqtSlot(object)
    def _update_icon(self, models: object) -> None:
        cards = models if isinstance(models, tuple) and len(models) == 2 else None
        outer = cards[0] if cards is not None else None
        inner = cards[1] if cards is not None else None
        icon = self._make_icon(outer, inner)
        self.setIcon(icon)
        self._window.setWindowIcon(icon)
        if outer is not None and inner is not None:
            self.setToolTip(
                f"{APP_NAME} Usage Rings | 5-hour {outer.percent_remaining}% remaining; "
                f"Weekly {inner.percent_remaining}% remaining"
            )
        else:
            self.setToolTip(f"{APP_NAME} Usage Rings | waiting for usage data")

    @staticmethod
    def _make_icon(outer: UsageCardModel | None, inner: UsageCardModel | None) -> QtGui.QIcon:
        size = 64
        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor("#1c2430"))
        painter.drawRoundedRect(QtCore.QRectF(2, 2, 60, 60), 14, 14)

        outer_rect = QtCore.QRectF(9, 9, 46, 46)
        inner_rect = QtCore.QRectF(19, 19, 26, 26)
        UsageRingsTray._draw_ring(painter, outer_rect, 6, outer.percent_remaining if outer else None)
        UsageRingsTray._draw_ring(painter, inner_rect, 5, inner.percent_remaining if inner else None)
        painter.end()
        return QtGui.QIcon(pixmap)

    @staticmethod
    def _draw_ring(
        painter: QtGui.QPainter,
        rect: QtCore.QRectF,
        width: int,
        remaining: int | None,
    ) -> None:
        background = QtGui.QPen(QtGui.QColor("#536174"), width)
        background.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(background)
        painter.drawArc(rect, 90 * 16, -360 * 16)
        if remaining is None:
            return
        accent = QtGui.QPen(_remaining_color(remaining), width)
        accent.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(accent)
        painter.drawArc(rect, 90 * 16, -int(max(0, min(100, remaining)) * 3.6 * 16))


def _remaining_color(percent: int) -> QtGui.QColor:
    if percent >= 50:
        return QtGui.QColor("#46d58b")
    if percent >= 20:
        return QtGui.QColor("#ffbd69")
    return QtGui.QColor("#ff6b76")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Standalone Windows app for {APP_NAME} usage rings.")
    parser.add_argument("--auth-file", type=Path, default=Path(os.environ.get("CLAUDE_AUTH_FILE", DEFAULT_AUTH_FILE)))
    parser.add_argument("--base-url", default=os.environ.get("CLAUDE_USAGE_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--refresh-seconds", type=int, default=60)
    parser.add_argument(
        "--start-visible",
        action="store_true",
        help="Show the widget immediately before lifecycle synchronization takes over.",
    )
    args = parser.parse_args(argv)
    instance_mutex = _acquire_single_instance()
    if instance_mutex == 0:
        return 0

    app = QtWidgets.QApplication(sys.argv[:1])
    # Hiding the rings while Claude is closed must not terminate the listener.
    app.setQuitOnLastWindowClosed(False)
    widget = UsageRingsWindow(
        auth_file=args.auth_file,
        base_url=args.base_url,
        refresh_seconds=args.refresh_seconds,
    )
    tray = UsageRingsTray(widget, app)
    tray.show()
    app.aboutToQuit.connect(tray.hide)
    if args.start_visible:
        widget.show()
        widget._raise_without_focus()
    else:
        # Resolve the initial state before entering the event loop. The timer
        # continues polling, but startup no longer depends on its first tick
        # to make the widget visible beside an already-open Claude window.
        widget._sync_with_claude()
    app.aboutToQuit.connect(lambda: _release_single_instance(instance_mutex))
    return app.exec()


def _acquire_single_instance() -> int | None:
    """Keep startup and manual launches from creating duplicate widgets."""

    if sys.platform != "win32":
        return None
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, True, "Local\\ClaudeCodeUsageRings")
    if not mutex:
        return None
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(mutex)
        return 0
    return mutex


def _release_single_instance(mutex: int | None) -> None:
    if mutex:
        ctypes.windll.kernel32.ReleaseMutex(mutex)
        ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
