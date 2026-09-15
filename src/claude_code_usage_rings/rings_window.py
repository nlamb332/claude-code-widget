from __future__ import annotations

import ctypes
import logging
import sys
from datetime import datetime
from typing import Optional

from PyQt6 import QtCore, QtGui, QtWidgets

from .host_window import get_claude_window_state
from .account_usage import (
    ClaudeNetworkError,
    ClaudeUsageError,
    fetch_usage_with_auth_refresh,
    parse_usage_payload,
)
from .models import UsageCardModel, build_card_models
from .window_snap import (
    find_connected_peer,
    find_snap_candidate,
    get_window_rect,
    move_window,
    windows_are_connected,
)

# Shown in the header, window title, and tray tooltip. Change this one
# constant to rebrand the widget.
APP_NAME = "Claude"

# Keep two extra Ctrl+- steps available for a compact desktop footprint.
MIN_SCALE = 0.25
MAX_SCALE = 1.8
SCALE_STEP = 0.1
FULL_CONTENT_MIN_SCALE = round(MIN_SCALE + (2 * SCALE_STEP), 2)
DEFAULT_SCALE = FULL_CONTENT_MIN_SCALE

# A failing refresh must not keep polling at the healthy cadence: hammering a
# throttled endpoint every minute is what keeps it throttled.
MAX_REFRESH_BACKOFF_SECONDS = 15 * 60
# Offline failures never reach the server, so they retry on a short fixed
# cadence instead of backing off. The rings recover soon after a reconnect.
OFFLINE_RETRY_SECONDS = 30

LOGGER = logging.getLogger("claude_code_usage_rings")


class UsageRingsWindow(QtWidgets.QWidget):
    """A compact, always-on-top pair of usage rings that follows Claude."""

    usage_changed = QtCore.pyqtSignal(object)
    glass_mode_changed = QtCore.pyqtSignal(bool)
    quit_requested = QtCore.pyqtSignal()

    def __init__(
        self,
        *,
        auth_file,
        base_url: str,
        refresh_seconds: int = 60,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._auth_file = auth_file
        self._base_url = base_url
        self._drag_position: QtCore.QPoint | None = None
        self._snap_peer_hwnd: int | None = None
        self._snap_peer_offset: QtCore.QPoint | None = None
        self._thread: QtCore.QThread | None = None
        self._worker: UsageFetchWorker | None = None
        self._scale = DEFAULT_SCALE
        self._claude_state = None
        self._position_initialized = False
        self._settings = QtCore.QSettings(APP_NAME, "UsageRings")
        self._glass_mode = bool(self._settings.value("window/glass", False, type=bool))

        self._rings = UsageRingsCanvas(parent=self)
        self._rings.installEventFilter(self)
        self._layout = QtWidgets.QHBoxLayout(self)
        # The painted canvas is the complete visible card. Keeping the
        # top-level window flush with it makes snapped card borders touch,
        # instead of leaving the former transparent layout margins between
        # the widgets.
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(12)
        self._layout.addWidget(self._rings)

        self.setWindowTitle(f"{APP_NAME} Usage")
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setObjectName("usageWidget")
        self._apply_background_style()
        self._rings.set_glass_mode(self._glass_mode)

        self._increase_shortcuts = (
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl++"), self),
            QtGui.QShortcut(QtGui.QKeySequence("Ctrl+="), self),
        )
        for shortcut in self._increase_shortcuts:
            shortcut.setContext(QtCore.Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(self._increase_scale)
        self._decrease_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+-"), self)
        self._decrease_shortcut.setContext(QtCore.Qt.ShortcutContext.ApplicationShortcut)
        self._decrease_shortcut.activated.connect(self._decrease_scale)
        self._glass_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+T"), self)
        self._glass_shortcut.setContext(QtCore.Qt.ShortcutContext.ApplicationShortcut)
        self._glass_shortcut.activated.connect(self._toggle_glass_mode)
        self._unsnap_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+S"), self)
        self._unsnap_shortcut.setContext(QtCore.Qt.ShortcutContext.ApplicationShortcut)
        self._unsnap_shortcut.activated.connect(self._unsnap)
        # Each widget runs in its own process, so an application shortcut
        # quits only the widget that currently has focus.
        self._quit_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Q"), self)
        self._quit_shortcut.setContext(QtCore.Qt.ShortcutContext.ApplicationShortcut)
        self._quit_shortcut.activated.connect(self.quit_requested.emit)

        self._base_refresh_seconds = max(15, refresh_seconds)
        self._consecutive_failures = 0
        self._usage_timer = QtCore.QTimer(self)
        self._usage_timer.setInterval(self._base_refresh_seconds * 1000)
        self._usage_timer.timeout.connect(self.refresh)

        self._lifecycle_timer = QtCore.QTimer(self)
        self._lifecycle_timer.setInterval(500)
        self._lifecycle_timer.timeout.connect(self._sync_with_claude)

        self._apply_scale()
        QtCore.QTimer.singleShot(0, self._place_initially)
        QtCore.QTimer.singleShot(0, self._sync_with_claude)
        QtCore.QTimer.singleShot(0, self.refresh)
        self._usage_timer.start()
        self._lifecycle_timer.start()

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:  # noqa: N802
        if watched is self._rings:
            if event.type() == QtCore.QEvent.Type.MouseButtonPress:
                self.mousePressEvent(event)  # type: ignore[arg-type]
                return True
            if event.type() == QtCore.QEvent.Type.MouseMove:
                self.mouseMoveEvent(event)  # type: ignore[arg-type]
                return True
            if event.type() == QtCore.QEvent.Type.MouseButtonRelease:
                self.mouseReleaseEvent(event)  # type: ignore[arg-type]
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QtGui.QMouseEvent | None) -> None:  # noqa: N802
        if event and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._refresh_snap_peer()
            self.grabMouse()
            event.accept()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent | None) -> None:  # noqa: N802
        if event and event.buttons() & QtCore.Qt.MouseButton.LeftButton and self._drag_position is not None:
            desired = event.globalPosition().toPoint() - self._drag_position
            self._move_dragged_window(desired)
            event.accept()
        super().mouseMoveEvent(event)

    def _current_window_rect(self) -> tuple[int, int, int, int]:
        rect = get_window_rect(int(self.winId()))
        if rect is not None:
            return rect
        return self.x(), self.y(), self.x() + self.width(), self.y() + self.height()

    def _clear_snap_state(self) -> None:
        self._snap_peer_hwnd = None
        self._snap_peer_offset = None

    def _refresh_snap_peer(self) -> None:
        own_hwnd = int(self.winId())
        own_rect = self._current_window_rect()
        if self._snap_peer_hwnd is not None:
            peer_rect = get_window_rect(self._snap_peer_hwnd)
            if peer_rect is None or not windows_are_connected(own_rect, peer_rect):
                self._clear_snap_state()

        if self._snap_peer_hwnd is None:
            self._snap_peer_hwnd = find_connected_peer(own_hwnd)

        if self._snap_peer_hwnd is not None:
            peer_rect = get_window_rect(self._snap_peer_hwnd)
            if peer_rect is None:
                self._clear_snap_state()
            else:
                self._snap_peer_offset = QtCore.QPoint(
                    peer_rect[0] - own_rect[0],
                    peer_rect[1] - own_rect[1],
                )

    def _move_dragged_window(self, desired: QtCore.QPoint) -> None:
        own_hwnd = int(self.winId())
        if self._snap_peer_hwnd is not None and self._snap_peer_offset is not None:
            peer_rect = get_window_rect(self._snap_peer_hwnd)
            if peer_rect is not None and windows_are_connected(self._current_window_rect(), peer_rect):
                move_window(own_hwnd, desired.x(), desired.y())
                move_window(
                    self._snap_peer_hwnd,
                    desired.x() + self._snap_peer_offset.x(),
                    desired.y() + self._snap_peer_offset.y(),
                )
                return
            self._clear_snap_state()

        # Do not snap while dragging. This lets the user pass through a
        # candidate zone and choose whether to snap by where they release.
        move_window(own_hwnd, desired.x(), desired.y())

    def _snap_on_release(self, desired: QtCore.QPoint) -> None:
        if self._snap_peer_hwnd is not None:
            return

        own_hwnd = int(self.winId())
        own_rect = self._current_window_rect()
        candidate = find_snap_candidate(
            desired.x(),
            desired.y(),
            own_rect[2] - own_rect[0],
            own_rect[3] - own_rect[1],
            own_hwnd=own_hwnd,
        )
        if candidate is None:
            return

        peer_rect = get_window_rect(candidate.peer_hwnd)
        if peer_rect is None:
            return

        move_window(own_hwnd, candidate.x, candidate.y)
        self._snap_peer_hwnd = candidate.peer_hwnd
        self._snap_peer_offset = QtCore.QPoint(
            peer_rect[0] - candidate.x,
            peer_rect[1] - candidate.y,
        )

    @QtCore.pyqtSlot()
    def _unsnap(self) -> None:
        self._refresh_snap_peer()
        peer_rect = get_window_rect(self._snap_peer_hwnd) if self._snap_peer_hwnd is not None else None
        new_x, new_y = self.x(), self.y()
        if peer_rect is not None:
            own_left, own_top, own_right, own_bottom = self._current_window_rect()
            peer_left, peer_top, peer_right, peer_bottom = peer_rect
            separation = 24
            if abs(own_right - peer_left) <= 3:
                new_x -= separation
            elif abs(own_left - peer_right) <= 3:
                new_x += separation
            elif abs(own_bottom - peer_top) <= 3:
                new_y -= separation
            elif abs(own_top - peer_bottom) <= 3:
                new_y += separation
        self._clear_snap_state()
        if (new_x, new_y) != (self.x(), self.y()):
            self.move(new_x, new_y)
        self._save_position()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent | None) -> None:  # noqa: N802
        if event and event.button() == QtCore.Qt.MouseButton.LeftButton:
            if self._drag_position is not None:
                desired = event.globalPosition().toPoint() - self._drag_position
                self._snap_on_release(desired)
            self._drag_position = None
            self.releaseMouse()
            self._save_position()
            event.accept()
        super().mouseReleaseEvent(event)

    def closeEvent(self, event: QtGui.QCloseEvent | None) -> None:  # noqa: N802
        self._save_position()
        self._stop_worker()
        super().closeEvent(event)

    @QtCore.pyqtSlot()
    def _sync_with_claude(self) -> None:
        state = get_claude_window_state()
        should_show = state.should_show_widget
        if should_show and (not self.isVisible() or self.isMinimized()):
            self.showNormal()
            self._place_initially()
            self._raise_without_focus()
        elif not should_show and self.isVisible():
            self.hide()
        self._claude_state = state

    @QtCore.pyqtSlot()
    def refresh(self) -> None:
        if self._thread is not None:
            return
        self._thread = QtCore.QThread(self)
        self._worker = UsageFetchWorker(auth_file=self._auth_file, base_url=self._base_url)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.loaded.connect(self._handle_usage_loaded)
        self._worker.failed.connect(self._handle_usage_failed)
        self._worker.finished.connect(self._stop_worker)
        self._thread.start()

    @QtCore.pyqtSlot(object)
    def _handle_usage_loaded(self, cards: tuple[UsageCardModel, UsageCardModel]) -> None:
        self._consecutive_failures = 0
        self._apply_refresh_interval(self._base_refresh_seconds)
        self._rings.set_models(cards, last_refreshed=datetime.now().astimezone())
        self.usage_changed.emit(cards)

    @QtCore.pyqtSlot(str, bool)
    def _handle_usage_failed(self, message: str, offline: bool) -> None:
        if offline:
            # Nothing reached the server, so there is nothing to back off
            # from. Retry soon so the rings recover right after a reconnect.
            LOGGER.warning("usage refresh failed (offline): %s", message)
            self._apply_refresh_interval(OFFLINE_RETRY_SECONDS)
        else:
            self._consecutive_failures += 1
            LOGGER.warning("usage refresh failed (attempt %d): %s", self._consecutive_failures, message)
            self._back_off()
        if (offline or "HTTP 429" in message) and self._rings.has_models:
            # A throttle or a dropped connection is only transient noise when
            # there is already something to show. Keep the last good rings.
            self._rings.set_syncing(message)
            return
        # Never leave the widget sitting on SYNCING with nothing behind it:
        # without usage data the status has to name the actual failure, or the
        # widget looks like it is still working when it has given up.
        self._rings.set_error(message)
        self.usage_changed.emit(None)

    def _back_off(self) -> None:
        interval = min(
            self._base_refresh_seconds * (2 ** min(self._consecutive_failures, 8)),
            MAX_REFRESH_BACKOFF_SECONDS,
        )
        self._apply_refresh_interval(interval)

    def _apply_refresh_interval(self, seconds: int) -> None:
        milliseconds = int(seconds) * 1000
        if self._usage_timer.interval() == milliseconds:
            return
        self._usage_timer.setInterval(milliseconds)
        LOGGER.info("next usage refresh in %ds", int(seconds))

    def _increase_scale(self) -> None:
        self._change_scale(SCALE_STEP)

    def _decrease_scale(self) -> None:
        self._change_scale(-SCALE_STEP)

    def _toggle_glass_mode(self) -> None:
        self.set_glass_mode(not self._glass_mode)

    def set_glass_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._glass_mode:
            return
        self._glass_mode = enabled
        self._settings.setValue("window/glass", enabled)
        self._settings.sync()
        self._rings.set_glass_mode(enabled)
        self._apply_background_style()
        self.glass_mode_changed.emit(enabled)

    @property
    def glass_mode(self) -> bool:
        return self._glass_mode

    def _apply_background_style(self) -> None:
        # The canvas paints the rounded card. Keeping the top-level window
        # transparent prevents Windows from compositing a black rectangle
        # around it, including when the widget is launched from a sandbox.
        self.setStyleSheet("QWidget#usageWidget { background: transparent; border: none; }")

    def _change_scale(self, delta: float) -> None:
        next_scale = max(MIN_SCALE, min(MAX_SCALE, round(self._scale + delta, 2)))
        if next_scale == self._scale:
            return
        self._scale = next_scale
        self._apply_scale()

    def _apply_scale(self) -> None:
        margin_h = int(round(18 * self._scale))
        margin_v = int(round(14 * self._scale))
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(int(round(12 * self._scale)))
        self._rings.apply_scale(self._scale)
        width = max(
            int(round(408 * self._scale)) - 2 * margin_h,
            (120 - 2 * margin_h) if self._scale < 0.35 else 0,
        )
        height = max(
            int(round(470 * self._scale)) - 2 * margin_v,
            (142 - 2 * margin_v) if self._scale < 0.35 else 0,
        )
        self.setMinimumSize(width, height)
        self.resize(width, height)

    def _stop_worker(self) -> None:
        worker = self._worker
        thread = self._thread
        self._worker = None
        self._thread = None
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.quit()
            thread.wait(1500)
            thread.deleteLater()

    def _place_initially(self) -> None:
        if self._position_initialized:
            return
        saved_x = self._settings.value("window/x", None)
        saved_y = self._settings.value("window/y", None)
        try:
            saved_position = QtCore.QPoint(int(saved_x), int(saved_y))
        except (TypeError, ValueError):
            saved_position = None
        if saved_position is not None:
            saved_center = saved_position + QtCore.QPoint(self.width() // 2, self.height() // 2)
            if QtWidgets.QApplication.screenAt(saved_center) is not None:
                self.move(saved_position)
                self._position_initialized = True
                return

        screen = QtWidgets.QApplication.screenAt(QtGui.QCursor.pos()) or QtWidgets.QApplication.primaryScreen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        self.move(
            geometry.x() + int((geometry.width() - self.width()) / 2),
            geometry.y() + 24,
        )
        self._position_initialized = True

    def _save_position(self) -> None:
        if not self._position_initialized or self.isMinimized():
            return
        self._settings.setValue("window/x", self.x())
        self._settings.setValue("window/y", self.y())
        self._settings.sync()

    def _raise_without_focus(self) -> None:
        """Put the frameless widget above Claude without stealing keyboard focus."""

        self.raise_()
        if sys.platform != "win32":
            return
        hwnd = int(self.winId())
        # HWND_TOPMOST plus SWP_NOACTIVATE keeps the usage card visible over a
        # normal Claude window without moving focus away from the text box.
        ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0053)


def _status_label(models: object, stale: bool, error: str | None) -> str:
    """Map the canvas state onto the header badge.

    SYNCING means "data on screen, refresh in flight". It must not double as
    the state for "no data and an error we decided not to show", which is how
    the widget used to get pinned on SYNCING indefinitely.
    """

    if error:
        return "ERROR"
    if models is None:
        return "SYNCING"
    return "SYNCING" if stale else "LIVE"


class UsageRingsCanvas(QtWidgets.QWidget):
    """Paint the 5-hour ring around the weekly ring."""

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self._scale = 1.0
        self._models: tuple[UsageCardModel, UsageCardModel] | None = None
        self._error: str | None = None
        self._stale = False
        self._glass_mode = False
        self._last_refreshed: datetime | None = None
        self.setMinimumSize(372, 412)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self.setAccessibleName(f"{APP_NAME} usage")
        self.setAccessibleDescription(f"Remaining usage for the 5-hour and weekly {APP_NAME} limits")

    def apply_scale(self, scale: float) -> None:
        self._scale = scale
        self.setMinimumSize(int(round(324 * scale)), int(round(354 * scale)))
        self.update()

    def set_glass_mode(self, enabled: bool) -> None:
        self._glass_mode = bool(enabled)
        self.update()

    def set_models(
        self,
        models: tuple[UsageCardModel, UsageCardModel],
        *,
        last_refreshed: datetime | None = None,
    ) -> None:
        self._models = models
        self._error = None
        self._stale = False
        if last_refreshed is not None:
            self._last_refreshed = last_refreshed
        self.setToolTip(
            f"5-hour: {models[0].percent_remaining}% remaining\n"
            f"{models[0].reset_text}\n"
            f"Weekly: {models[1].percent_remaining}% remaining\n"
            f"{models[1].reset_text}"
        )
        self.update()

    @property
    def has_models(self) -> bool:
        return self._models is not None

    def set_syncing(self, detail: str | None = None) -> None:
        self._stale = True
        self._error = None
        tooltip = f"{APP_NAME} usage is temporarily rate limited; retrying"
        if detail:
            tooltip = f"{tooltip}\n{detail[:160]}"
        self.setToolTip(tooltip)
        self.update()

    def set_error(self, message: str) -> None:
        self._models = None
        self._error = message[:80]
        self._stale = False
        self.setToolTip(f"{APP_NAME} usage unavailable: {self._error}")
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent | None) -> None:  # noqa: N802
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        border_color = QtGui.QColor("#7890aa") if self._glass_mode else QtGui.QColor("#3a4656")
        border_color.setAlpha(150 if self._glass_mode else 255)
        painter.setPen(QtGui.QPen(border_color, max(1, int(round(self._scale)))))
        background = QtGui.QLinearGradient(0, 0, 0, self.height())
        if self._glass_mode:
            background.setColorAt(0, QtGui.QColor(52, 67, 86, 178))
            background.setColorAt(0.48, QtGui.QColor(30, 42, 57, 148))
            background.setColorAt(1, QtGui.QColor(13, 20, 29, 184))
        else:
            background.setColorAt(0, QtGui.QColor("#222a35"))
            background.setColorAt(1, QtGui.QColor("#171c24"))
        painter.setBrush(background)
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 22 * self._scale, 22 * self._scale)

        show_full_content = self._scale >= FULL_CONTENT_MIN_SCALE
        show_header = True
        compact_footer = self._scale < 0.7
        percentage_only_footer = self._scale < FULL_CONTENT_MIN_SCALE
        row_pitch = (
            max(20, int(round(26 * self._scale)))
            if percentage_only_footer
            else max(24, int(round(28 * self._scale)))
            if compact_footer
            else max(34, int(round(40 * self._scale)))
        )
        footer_height = row_pitch * 2 + (8 if percentage_only_footer else 12)
        footer_height = max(footer_height, int(round(96 * self._scale)))
        footer_top = self.height() - footer_height
        if show_header:
            header_margin = max(4, int(round(24 * self._scale)))
            header_top = max(3, int(round(12 * self._scale)))
            header_height = max(18, int(round(24 * self._scale)))
            header_rect = QtCore.QRectF(
                header_margin,
                header_top,
                max(0, self.width() - 2 * header_margin),
                header_height,
            )

            title_font = QtGui.QFont("Segoe UI", max(8, int(round(14 * self._scale))))
            title_font.setWeight(QtGui.QFont.Weight.DemiBold)
            painter.setFont(title_font)
            painter.setPen(QtGui.QColor("#f5f7fb"))

            status = _status_label(self._models, self._stale, self._error)
            status_color = "#46d58b" if status == "LIVE" else ("#ff6b76" if status == "ERROR" else "#b9c4d3")
            status_font = QtGui.QFont("Segoe UI", max(7, int(round(9 * self._scale))))
            status_font.setWeight(QtGui.QFont.Weight.DemiBold)
            # Minor change: show when the data was last refreshed alongside
            # the live/error/syncing status instead of the status alone.
            status_text = f"●  {status}"
            if self._scale >= 0.5 and status == "LIVE" and self._last_refreshed is not None:
                status_text = f"●  {status} · {self._last_refreshed:%H:%M}"
            status_width = QtGui.QFontMetrics(status_font).horizontalAdvance(status_text)
            status_width = min(status_width, header_rect.width())
            title_width = max(0, header_rect.width() - status_width - max(5, int(round(8 * self._scale))))
            painter.drawText(
                QtCore.QRectF(header_rect.left(), header_rect.top(), title_width, header_rect.height()),
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                f"{APP_NAME} usage",
            )

            painter.setFont(status_font)
            painter.setPen(QtGui.QColor(status_color))
            painter.drawText(
                QtCore.QRectF(header_rect.right() - status_width, header_rect.top(), status_width, header_rect.height()),
                QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
                status_text,
            )

            ring_top = max(
                20,
                int(round(44 * self._scale)),
                header_top + header_height + max(4, int(round(8 * self._scale))),
            )
        else:
            ring_top = max(8, int(round(16 * self._scale)))
        footer_gap = max(5, int(round(8 * self._scale)))
        diameter = min(
            self.width() - int(round(52 * self._scale)),
            footer_top - ring_top - footer_gap,
        )
        outer_rect = QtCore.QRectF(
            (self.width() - diameter) / 2,
            ring_top,
            diameter,
            diameter,
        )
        width = max(7, int(round(12 * self._scale)))
        background_pen = QtGui.QPen(QtGui.QColor("#687689"), width)
        background_pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(background_pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawArc(outer_rect, 90 * 16, -360 * 16)

        inner_diameter = diameter * 0.67
        inner_rect = QtCore.QRectF(
            (self.width() - inner_diameter) / 2,
            outer_rect.center().y() - inner_diameter / 2,
            inner_diameter,
            inner_diameter,
        )
        inner_width = max(6, int(round(10 * self._scale)))
        inner_background_pen = QtGui.QPen(QtGui.QColor("#748198"), inner_width)
        inner_background_pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(inner_background_pen)
        painter.drawArc(inner_rect, 90 * 16, -360 * 16)

        outer_model = self._models[0] if self._models is not None else None
        inner_model = self._models[1] if self._models is not None else None
        outer_remaining = outer_model.percent_remaining if outer_model is not None else 0
        inner_remaining = inner_model.percent_remaining if inner_model is not None else 0

        if outer_model is not None:
            outer_pen = QtGui.QPen(_ring_color(outer_remaining), width)
            outer_pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            painter.setPen(outer_pen)
            painter.drawArc(outer_rect, 90 * 16, -int(outer_remaining * 3.6 * 16))
        if inner_model is not None:
            inner_pen = QtGui.QPen(_ring_color(inner_remaining), inner_width)
            inner_pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            painter.setPen(inner_pen)
            painter.drawArc(inner_rect, 90 * 16, -int(inner_remaining * 3.6 * 16))

        percent_size = (
            max(10, int(round(24 * self._scale)))
            if not show_full_content
            else max(16, int(round(30 * self._scale)))
        )
        percent_font = QtGui.QFont("Segoe UI", percent_size)
        percent_font.setWeight(QtGui.QFont.Weight.Bold)
        painter.setPen(QtGui.QColor("#ffffff"))
        if show_full_content and inner_model is not None:
            center_label_font = QtGui.QFont("Segoe UI", max(8, int(round(11 * self._scale))))
            center_label_font.setWeight(QtGui.QFont.Weight.DemiBold)
            remaining_font = QtGui.QFont("Segoe UI", max(8, int(round(9 * self._scale))))
            center_label_height = max(11, int(round(16 * self._scale)))
            percent_height = max(20, int(round(32 * self._scale)))
            remaining_height = max(11, int(round(16 * self._scale)))
            total_height = center_label_height + percent_height + remaining_height
            center_top = inner_rect.center().y() - total_height / 2

            painter.setFont(center_label_font)
            painter.setPen(QtGui.QColor("#d1d9e5"))
            painter.drawText(
                QtCore.QRectF(inner_rect.left(), center_top, inner_rect.width(), center_label_height),
                QtCore.Qt.AlignmentFlag.AlignCenter,
                "Weekly",
            )
            painter.setFont(percent_font)
            painter.setPen(QtGui.QColor("#ffffff"))
            painter.drawText(
                QtCore.QRectF(inner_rect.left(), center_top + center_label_height, inner_rect.width(), percent_height),
                QtCore.Qt.AlignmentFlag.AlignCenter,
                f"{inner_remaining}%",
            )
            painter.setFont(remaining_font)
            painter.setPen(QtGui.QColor("#b7c2d1"))
            painter.drawText(
                QtCore.QRectF(
                    inner_rect.left(),
                    center_top + center_label_height + percent_height,
                    inner_rect.width(),
                    remaining_height,
                ),
                QtCore.Qt.AlignmentFlag.AlignCenter,
                "remaining",
            )
        else:
            painter.setFont(percent_font)
            painter.drawText(
                inner_rect.adjusted(0, -1 * self._scale, 0, 1 * self._scale),
                QtCore.Qt.AlignmentFlag.AlignCenter,
                f"{inner_remaining}%" if inner_model is not None else "—",
            )

        painter.setPen(QtGui.QPen(QtGui.QColor("#344050"), max(1, int(round(self._scale)))))
        painter.drawLine(
            QtCore.QPointF(24 * self._scale, footer_top - 8 * self._scale),
            QtCore.QPointF(self.width() - 24 * self._scale, footer_top - 8 * self._scale),
        )
        if self._error:
            footer_font = QtGui.QFont("Segoe UI", max(8, int(round(10 * self._scale))))
            painter.setFont(footer_font)
            painter.setPen(QtGui.QColor("#ffb6bd"))
            painter.drawText(
                QtCore.QRectF(20 * self._scale, footer_top, self.width() - 40 * self._scale, 72 * self._scale),
                QtCore.Qt.AlignmentFlag.AlignCenter | QtCore.Qt.TextFlag.TextWordWrap,
                self._error,
            )
        else:
            self._draw_legend_row(
                painter,
                footer_top,
                "5-hour",
                outer_model,
                outer_remaining,
                compact=compact_footer,
                percentage_only=percentage_only_footer,
            )
            self._draw_legend_row(
                painter,
                footer_top + row_pitch,
                "Weekly",
                inner_model,
                inner_remaining,
                compact=compact_footer,
                percentage_only=percentage_only_footer,
            )

        # Paint the banner last so compact rings or footer text can never
        # cover it when a window is resized to a small tier.
        self._draw_persistent_header(painter)

    def _draw_persistent_header(self, painter: QtGui.QPainter) -> None:
        header_margin = max(4, int(round(24 * self._scale)))
        header_top = max(3, int(round(12 * self._scale)))
        header_height = max(18, int(round(24 * self._scale)))
        header_rect = QtCore.QRectF(
            header_margin,
            header_top,
            max(0, self.width() - 2 * header_margin),
            header_height,
        )
        title_font = QtGui.QFont("Segoe UI", max(8, int(round(14 * self._scale))))
        title_font.setWeight(QtGui.QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QtGui.QColor("#f5f7fb"))
        status = _status_label(self._models, self._stale, self._error)
        status_color = "#46d58b" if status == "LIVE" else ("#ff6b76" if status == "ERROR" else "#b9c4d3")
        status_font = QtGui.QFont("Segoe UI", max(7, int(round(9 * self._scale))))
        status_font.setWeight(QtGui.QFont.Weight.DemiBold)
        status_text = f"●  {status}"
        if self._scale >= 0.5 and status == "LIVE" and self._last_refreshed is not None:
            status_text = f"●  {status} · {self._last_refreshed:%H:%M}"
        status_width = min(QtGui.QFontMetrics(status_font).horizontalAdvance(status_text), header_rect.width())
        title_width = max(0, header_rect.width() - status_width - max(5, int(round(8 * self._scale))))
        painter.drawText(
            QtCore.QRectF(header_rect.left(), header_rect.top(), title_width, header_rect.height()),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            f"{APP_NAME} usage",
        )
        painter.setFont(status_font)
        painter.setPen(QtGui.QColor(status_color))
        painter.drawText(
            QtCore.QRectF(header_rect.right() - status_width, header_rect.top(), status_width, header_rect.height()),
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
            status_text,
        )

    def _draw_legend_row(
        self,
        painter: QtGui.QPainter,
        top: float,
        label: str,
        model: UsageCardModel | None,
        remaining: int,
        *,
        compact: bool,
        percentage_only: bool,
    ) -> None:
        color = _ring_color(remaining) if model is not None else QtGui.QColor("#687384")
        left_margin = (
            max(10, int(round(18 * self._scale)))
            if compact
            else max(14, int(round(24 * self._scale)))
        )
        right_margin = left_margin
        text_left = left_margin + (
            max(12, int(round(14 * self._scale)))
            if compact
            else max(14, int(round(16 * self._scale)))
        )
        dot_size = max(6, int(round(8 * self._scale)))
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QtCore.QRectF(left_margin, top + 8 * self._scale, dot_size, dot_size))

        label_font = QtGui.QFont(
            "Segoe UI",
            max(8, int(round(10 * self._scale))) if compact else max(9, int(round(10 * self._scale))),
        )
        label_font.setWeight(QtGui.QFont.Weight.DemiBold)
        painter.setFont(label_font)
        painter.setPen(QtGui.QColor("#f1f4f8"))
        if model is None:
            percent_text = "—"
        elif percentage_only:
            percent_text = f"{remaining}%"
        else:
            percent_text = f"{remaining}% remaining"
        available_width = max(40, self.width() - text_left - right_margin)
        label_height = max(18 if percentage_only else 22 if compact else 18, int(round(22 * self._scale)))

        if compact:
            # Give the value its own right-aligned column. A single combined
            # string can run past the edge at the two smallest widget sizes,
            # which makes the important "remaining" text appear truncated.
            value_font = QtGui.QFont(label_font)
            value_metrics = QtGui.QFontMetrics(value_font)
            value_width = value_metrics.horizontalAdvance(percent_text)
            gap = max(6, int(round(8 * self._scale)))
            label_width = max(32, available_width - value_width - gap)
            label_text = value_metrics.elidedText(label, QtCore.Qt.TextElideMode.ElideRight, label_width)
            painter.drawText(
                QtCore.QRectF(text_left, top, label_width, label_height),
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                label_text,
            )
            painter.drawText(
                QtCore.QRectF(text_left + label_width + gap, top, available_width - label_width - gap, label_height),
                QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
                percent_text,
            )
            return

        painter.drawText(
            QtCore.QRectF(text_left, top, available_width, label_height),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            f"{label}  ·  {percent_text}",
        )

        # At compact settings, the reset lines cannot remain legible
        # beside the rings. Keep them available in the tooltip instead.

        reset_font = QtGui.QFont("Segoe UI", max(8, int(round(9 * self._scale))))
        painter.setFont(reset_font)
        painter.setPen(QtGui.QColor("#b7c2d1"))
        reset_text = model.reset_text if model is not None else "Reset unavailable"
        reset_text = QtGui.QFontMetrics(reset_font).elidedText(
            reset_text,
            QtCore.Qt.TextElideMode.ElideRight,
            available_width,
        )
        painter.drawText(
            QtCore.QRectF(text_left, top + label_height - 1, available_width, max(16, int(round(18 * self._scale)))),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            reset_text,
        )


def _ring_color(remaining: int) -> QtGui.QColor:
    if remaining >= 50:
        return QtGui.QColor("#46d58b")
    if remaining >= 20:
        return QtGui.QColor("#ffcc66")
    return QtGui.QColor("#ff6b76")


class UsageFetchWorker(QtCore.QObject):
    loaded = QtCore.pyqtSignal(object)
    # The flag marks failures where no connection was made (offline).
    failed = QtCore.pyqtSignal(str, bool)
    finished = QtCore.pyqtSignal()

    def __init__(self, *, auth_file, base_url: str) -> None:
        super().__init__()
        self._auth_file = auth_file
        self._base_url = base_url

    @QtCore.pyqtSlot()
    def run(self) -> None:
        try:
            payload = fetch_usage_with_auth_refresh(self._auth_file, self._base_url, timeout=20.0)
            usage = parse_usage_payload(payload)
            self.loaded.emit(build_card_models(five_hour=usage.five_hour, weekly=usage.weekly))
        except ClaudeNetworkError as exc:
            self.failed.emit(str(exc), True)
        except ClaudeUsageError as exc:
            self.failed.emit(str(exc), False)
        except Exception as exc:  # noqa: BLE001 - a silent worker is the worse failure
            # Anything unexpected here previously escaped the slot, so no
            # result signal was ever emitted and the window stayed on its
            # initial SYNCING state forever. Report it instead.
            LOGGER.exception("unexpected error while refreshing usage")
            self.failed.emit(f"{type(exc).__name__}: {exc}", False)
        finally:
            self.finished.emit()
