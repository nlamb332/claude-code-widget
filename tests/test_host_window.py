import sys

import pytest

from usage_rings.host_window import HostWindowState, _DWMAPI, _is_cloaked


def state(**overrides) -> HostWindowState:
    values = dict(supported=True, running=True, window_found=True, minimized=False)
    values.update(overrides)
    return HostWindowState(**values)


def test_visible_on_current_desktop_shows():
    assert state().should_show_widget


def test_host_on_other_desktop_hides_for_both_providers():
    assert not state(on_other_desktop=True).should_show_widget
    assert not state(on_other_desktop=True, window_required=True).should_show_widget


def test_unsupported_platform_always_shows():
    assert state(supported=False, running=False, on_other_desktop=True).should_show_widget


def test_claude_process_without_window_still_shows():
    assert state(window_found=False).should_show_widget


def test_codex_requires_a_window():
    assert not state(window_found=False, window_required=True).should_show_widget


def test_minimized_hides():
    assert not state(minimized=True).should_show_widget


@pytest.mark.skipif(sys.platform != "win32", reason="DWM is Windows only")
def test_dwm_query_succeeds_on_a_real_window():
    import ctypes

    hwnd = ctypes.windll.user32.GetDesktopWindow()
    value = ctypes.c_uint()
    # HRESULT 0 proves the ctypes signature works; the desktop is never cloaked.
    assert _DWMAPI.DwmGetWindowAttribute(
        ctypes.c_void_p(hwnd), 14, ctypes.byref(value), ctypes.sizeof(value)
    ) == 0
    assert not _is_cloaked(hwnd)
