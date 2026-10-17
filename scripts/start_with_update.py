"""Update the checkout from origin, then start a watchdog per enabled widget.

Launching through this script keeps the installed widgets in step with GitHub
without any manual pulls. An update that cannot be applied cleanly is logged and
skipped so a failed refresh never stops the widgets from starting.

Which widgets run comes from the config file written by ``scripts/setup.py``.
Without one, setup is launched instead of guessing.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
FETCH_TIMEOUT = 60
UPDATE_TIMEOUT = 120
# Hide the console windows git would otherwise flash when launched by pythonw.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

for _path in (PROJECT_ROOT, SRC_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from usage_rings import config, single_instance  # noqa: E402
from usage_rings.providers import PROVIDERS  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update, then start the enabled usage widgets.")
    parser.add_argument("--no-update", action="store_true", help="Skip the git update.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would start without starting anything.")
    args = parser.parse_args(argv)

    if not args.no_update:
        try:
            _update_checkout()
        except Exception as exc:  # noqa: BLE001 - a broken update must not block launch
            _log(f"update skipped after {type(exc).__name__}")

    enabled = config.load_providers()
    if enabled is None:
        if args.dry_run:
            print("No config found; setup would be launched.")
            return 0
        _log("no usable config; launching setup")
        _launch_setup()
        return 0

    exit_code = 0
    for key in enabled:
        provider = PROVIDERS[key]
        if single_instance.is_held(provider.supervisor_mutex):
            _report(f"{key}: already running; leaving it alone.", args.dry_run)
            continue
        if args.dry_run:
            print(f"{key}: would start.")
            continue
        try:
            _start_watchdog(key)
        except OSError as exc:
            _log(f"{key}: watchdog could not start ({type(exc).__name__})")
            exit_code = 1
        else:
            _log(f"{key}: watchdog started")
    return exit_code


def _report(message: str, dry_run: bool) -> None:
    if dry_run:
        print(message)
    _log(message)


def _start_watchdog(provider_key: str) -> None:
    """Start one detached watchdog; it owns that widget from here on."""

    watchdog = SCRIPTS_DIR / "usage_rings_watchdog.py"
    creationflags = _NO_WINDOW
    if sys.platform == "win32":
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [sys.executable, str(watchdog), "--provider", provider_key],
        cwd=str(PROJECT_ROOT),
        close_fds=True,
        creationflags=creationflags,
    )


def _launch_setup() -> None:
    """Run setup in its own console; this script normally runs under pythonw."""

    python = Path(sys.executable)
    console_python = python.with_name("python.exe")
    executable = console_python if console_python.is_file() else python
    creationflags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
    subprocess.Popen(
        [str(executable), str(SCRIPTS_DIR / "setup.py"), "--pause"],
        cwd=str(PROJECT_ROOT),
        close_fds=True,
        creationflags=creationflags,
    )


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
    return config.config_dir()


if __name__ == "__main__":
    raise SystemExit(main())
