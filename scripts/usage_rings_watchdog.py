#!/usr/bin/env python3
"""Keep one provider's usage rings app available across restarts.

Run as ``usage_rings_watchdog.py --provider claude|codex``. One watchdog
supervises one widget process, so the two providers stay independent.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
WIDGET_SCRIPT = PROJECT_ROOT / "scripts" / "launch_usage_rings.py"

for _path in (PROJECT_ROOT, SRC_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from usage_rings import USER_QUIT_EXIT_CODE, single_instance  # noqa: E402
from usage_rings.providers import PROVIDERS, Provider, get_provider  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervise one usage rings widget.")
    parser.add_argument("--provider", required=True, choices=list(PROVIDERS))
    provider = get_provider(parser.parse_args(argv).provider)

    mutex = single_instance.acquire(provider.supervisor_mutex)
    if mutex == 0:
        _log(provider, "supervisor already running")
        return 0

    try:
        while True:
            try:
                creationflags = 0
                if sys.platform == "win32":
                    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                child = subprocess.Popen(
                    [sys.executable, str(WIDGET_SCRIPT), "--provider", provider.key],
                    cwd=str(PROJECT_ROOT),
                    close_fds=True,
                    creationflags=creationflags,
                )
                _log(provider, f"widget started pid={child.pid}")
                return_code = child.wait()
                _log(provider, f"widget exited code={return_code}")
            except Exception as exc:
                # Keep supervising if a transient process or filesystem error
                # prevents one child from starting. Only exception metadata is
                # logged; auth tokens and request contents are never written.
                _log(provider, f"supervisor error {type(exc).__name__}")
                time.sleep(5)
                continue
            if return_code == USER_QUIT_EXIT_CODE:
                # The user quit on purpose; restarting would undo that.
                _log(provider, "widget quit by user; supervisor stopping")
                return 0
            # A normal close or a transient startup failure should not leave
            # the widget unavailable, but avoid a tight respawn loop.
            time.sleep(3)
    finally:
        single_instance.release(mutex)


def _log(provider: Provider, message: str) -> None:
    """Write minimal lifecycle diagnostics without credentials or payloads."""

    try:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        log_file = base / provider.log_dir_name / "watchdog.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
    except OSError:
        # Logging must never prevent supervision.
        pass


if __name__ == "__main__":
    raise SystemExit(main())
