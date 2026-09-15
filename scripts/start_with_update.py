#!/usr/bin/env python3
"""Update the checkout from origin, then hand off to the widget watchdog.

Launching through this script keeps the installed widget in step with GitHub
without any manual pulls. An update that cannot be applied cleanly is logged and
skipped so a failed refresh never stops the widget from starting.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FETCH_TIMEOUT = 60
UPDATE_TIMEOUT = 120
# Hide the console windows git would otherwise flash when launched by pythonw.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def main() -> int:
    try:
        _update_checkout()
    except Exception as exc:  # noqa: BLE001 - a broken update must not block launch
        _log(f"update skipped after {type(exc).__name__}")

    watchdog = _watchdog_script()
    if watchdog is None:
        _log("no watchdog script found; nothing to launch")
        return 1

    os.chdir(PROJECT_ROOT)
    os.execv(sys.executable, [sys.executable, str(watchdog)])


def _update_checkout() -> None:
    git = _git_executable()
    if git is None:
        _log("git not found; running the installed version")
        return
    if not (PROJECT_ROOT / ".git").exists():
        _log("not a git checkout; running the installed version")
        return

    branch = _git_output(git, "rev-parse", "--abbrev-ref", "HEAD")
    if branch in (None, "HEAD"):
        _log("detached HEAD; running the installed version")
        return
    if _git_output(git, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}") is None:
        _log(f"branch {branch} tracks no upstream; running the installed version")
        return

    before = _git_output(git, "rev-parse", "--short", "HEAD")
    fetch = _run_git(git, ["fetch", "--quiet", "origin"], FETCH_TIMEOUT)
    if fetch.returncode != 0:
        _log(f"fetch failed ({_first_line(fetch.stderr)}); running the installed version")
        return

    # --autostash keeps uncommitted edits, --rebase replays local commits on top.
    update = _run_git(git, ["pull", "--rebase", "--autostash", "--quiet"], UPDATE_TIMEOUT)
    if update.returncode != 0:
        _log(f"update failed ({_first_line(update.stderr)}); running the previous version")
        _abort_incomplete_rebase(git)
        return

    after = _git_output(git, "rev-parse", "--short", "HEAD")
    if before == after:
        _log(f"already current at {after} on {branch}")
    else:
        _log(f"updated {before} -> {after} on {branch}")


def _abort_incomplete_rebase(git: str) -> None:
    """Leave the checkout usable when a rebase stops on a conflict."""

    git_dir = PROJECT_ROOT / ".git"
    if not any((git_dir / name).exists() for name in ("rebase-merge", "rebase-apply")):
        return
    if _run_git(git, ["rebase", "--abort"], UPDATE_TIMEOUT).returncode == 0:
        _log("conflicting update rolled back; resolve it manually to get the latest")
    else:
        _log("conflicting update could not be rolled back; the checkout needs attention")


def _watchdog_script() -> Path | None:
    scripts = sorted((PROJECT_ROOT / "scripts").glob("*_watchdog.py"))
    return scripts[0] if scripts else None


def _git_executable() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    for candidate in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd" / "git.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _run_git(git: str, args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [git, "-C", str(PROJECT_ROOT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )


def _git_output(git: str, *args: str) -> str | None:
    result = _run_git(git, list(args), FETCH_TIMEOUT)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "no details"


def _log(message: str) -> None:
    """Record update outcomes only; command output may name private paths."""

    try:
        log_file = _log_directory() / "update.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
    except OSError:
        # Logging must never prevent the widget from starting.
        pass


def _log_directory() -> Path:
    local_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    package = next(
        (p.name for p in (PROJECT_ROOT / "src").iterdir() if p.is_dir() and p.name.endswith("usage_rings")),
        None,
    )
    if package is None:
        return local_appdata / PROJECT_ROOT.name
    return local_appdata / "".join(part.title() for part in package.split("_"))


if __name__ == "__main__":
    raise SystemExit(main())
