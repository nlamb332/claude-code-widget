"""The interface every usage provider fills in.

A provider is plain data plus three callables. The window, tray, watchdog and
setup code only ever see a ``Provider``, so adding a third service means
writing one module and registering it in ``providers/__init__.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..usage import ProviderUsage


@dataclass(frozen=True)
class HostWindowSpec:
    """How to recognise the desktop app the widget follows."""

    executable_names: frozenset[str]
    title_tokens: tuple[str, ...]
    # Codex helpers can outlive the desktop shell, so Codex requires a matching
    # top-level window to count as open. Claude Code can run as a console
    # process with no window at all, so a missing window is treated as unknown.
    require_window: bool


@dataclass(frozen=True)
class Prerequisites:
    """Result of checking that a provider can plausibly work on this machine."""

    ready: bool
    details: list[str] = field(default_factory=list)
    # What to do when not ready, phrased as a command or an instruction.
    hint: str = ""


@dataclass(frozen=True)
class Provider:
    # Stable id used by --provider, config.json, and as the registry key.
    key: str
    # Shown in the header, window title, and tray tooltip. It is also the
    # QSettings organisation and half of the cross-process snapping title
    # ("<app_name> Usage"), so changing it moves saved positions and breaks
    # snapping against the other widget.
    app_name: str
    # Folder under %LOCALAPPDATA% for this provider's usage.log/watchdog.log.
    log_dir_name: str
    instance_mutex: str
    supervisor_mutex: str
    host: HostWindowSpec
    default_auth_file: Path
    auth_file_env: str
    default_base_url: str
    base_url_env: str
    # (auth_file, base_url, timeout) -> raw usage payload, renewing via the CLI if needed.
    fetch_usage: Callable[[Path, str, float], dict[str, Any]]
    parse_usage: Callable[[dict[str, Any]], ProviderUsage]
    # (auth_file or None for the default) -> Prerequisites.
    check_prerequisites: Callable[[Path | None], Prerequisites]

    @property
    def window_title(self) -> str:
        return f"{self.app_name} Usage"
