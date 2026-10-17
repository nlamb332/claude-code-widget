"""First-run setup: choose widgets, check prerequisites, write config, add shortcuts.

Run it as ``python scripts/setup.py`` or ``usage-rings setup``. Every prompt
has a flag, so it can also be scripted:

    usage-rings setup --providers claude,codex --no-shortcuts
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
from pathlib import Path

from .config import config_path, load_providers, save_providers
from .providers import PROVIDERS
from .usage import NO_WINDOW

# Setup runs from a source checkout (scripts/ and assets/ sit beside src/).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = PROJECT_ROOT / "scripts" / "start_with_update.py"
ICON_FILE = PROJECT_ROOT / "assets" / "usage-widgets.ico"
SHORTCUT_NAME = "Usage Rings.lnk"
SHORTCUT_DESCRIPTION = "Start the Claude Code and Codex usage rings that are enabled in setup."

_DISPLAY_NAMES = {"claude": "Claude Code", "codex": "Codex"}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    interactive = _stdin_is_console() and not args.non_interactive

    try:
        return _run(args, interactive)
    finally:
        if args.pause:
            # Launched in a fresh console by the startup script: keep the
            # window open so its output can be read.
            _ask("Press Enter to close this window.", "")


def _stdin_is_console() -> bool:
    """Whether a person can answer prompts.

    ``isatty()`` is not enough on Windows: it reports the NUL device (what a
    scheduled task or ``< NUL`` redirect provides) as a terminal.
    """

    if sys.stdin is None:
        return False
    if sys.platform != "win32":
        return sys.stdin.isatty()
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
    return bool(handle) and bool(kernel32.GetConsoleMode(handle, ctypes.byref(ctypes.c_ulong())))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="usage-rings setup",
        description="Choose which usage widgets to enable and set up shortcuts.",
    )
    parser.add_argument(
        "--providers",
        help=f"Comma-separated widgets to enable: {', '.join(PROVIDERS)} (or 'all'). Asked when omitted.",
    )
    parser.add_argument("--no-shortcuts", action="store_true", help="Create neither shortcut.")
    parser.add_argument("--desktop-shortcut", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--startup-shortcut", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--start", action=argparse.BooleanOptionalAction, default=None, help="Start the widgets when setup finishes.")
    parser.add_argument("--desktop-dir", type=Path, help="Create the Desktop shortcut here instead of the real Desktop.")
    parser.add_argument("--startup-dir", type=Path, help="Create the Startup shortcut here instead of the real Startup folder.")
    parser.add_argument("--non-interactive", action="store_true", help="Never prompt; unanswered questions use their safe default.")
    parser.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    return parser


def _run(args: argparse.Namespace, interactive: bool) -> int:
    print("Usage Rings setup")
    print()

    selected = _select_providers(args, interactive)
    if selected is None:
        return 2

    enabled: list[str] = []
    for key in selected:
        provider = PROVIDERS[key]
        result = provider.check_prerequisites(None)
        status = "ready" if result.ready else "NOT READY"
        print(f"{_DISPLAY_NAMES[key]}: {status}")
        for line in result.details:
            print(f"  {line}")
        if not result.ready:
            print(f"  To fix: {result.hint}")
            if interactive and not _confirm(f"Enable {_DISPLAY_NAMES[key]} anyway (it will show an error until you sign in)?", True):
                print(f"  Skipping {_DISPLAY_NAMES[key]}.")
                continue
        enabled.append(key)
        print()

    if not enabled:
        print("No widgets were enabled; nothing was written.")
        return 1

    path = save_providers(enabled)
    print(f"Saved {', '.join(_DISPLAY_NAMES[key] for key in enabled)} to {path}")
    print()

    _create_shortcuts(args, interactive)

    if _decide(args.start, "Start the widgets now?", True, interactive):
        _start_widgets()
    return 0


def _select_providers(args: argparse.Namespace, interactive: bool) -> list[str] | None:
    if args.providers:
        wanted = [part.strip().lower() for part in args.providers.split(",") if part.strip()]
        if wanted == ["all"]:
            wanted = list(PROVIDERS)
        unknown = [key for key in wanted if key not in PROVIDERS]
        if unknown or not wanted:
            print(f"Unknown provider(s): {', '.join(unknown) or '(none given)'}. Choose from: {', '.join(PROVIDERS)}.", file=sys.stderr)
            return None
        return [key for key in PROVIDERS if key in wanted]

    if not interactive:
        print("--providers is required when setup cannot prompt (for example --providers claude,codex).", file=sys.stderr)
        return None

    current = load_providers()
    if current:
        print(f"Currently enabled: {', '.join(_DISPLAY_NAMES[key] for key in current)} ({config_path()})")
    keys = list(PROVIDERS)
    print("Which widgets do you want to enable?")
    for index, key in enumerate(keys, start=1):
        print(f"  {index}) {_DISPLAY_NAMES[key]}")
    print(f"  {len(keys) + 1}) Both")
    default = str(len(keys) + 1) if not current or len(current) > 1 else str(keys.index(current[0]) + 1)
    while True:
        answer = _ask("Choice", default)
        if answer.isdigit() and 1 <= int(answer) <= len(keys) + 1:
            index = int(answer)
            print()
            return keys if index == len(keys) + 1 else [keys[index - 1]]
        print("  Please enter one of the numbers above.")


def _create_shortcuts(args: argparse.Namespace, interactive: bool) -> None:
    desktop = False if args.no_shortcuts else args.desktop_shortcut
    startup = False if args.no_shortcuts else args.startup_shortcut
    want_desktop = _decide(desktop, "Create a Desktop shortcut that starts the widgets?", True, interactive)
    want_startup = _decide(startup, "Also start the widgets automatically when you sign in to Windows?", True, interactive)
    if not (want_desktop or want_startup):
        return
    if sys.platform != "win32":
        print("Shortcuts are only supported on Windows; skipping.")
        return
    if not START_SCRIPT.is_file():
        print(f"Cannot create shortcuts: {START_SCRIPT} is missing (run setup from a source checkout).")
        return

    if want_desktop:
        folder = args.desktop_dir or _shell_folder("Desktop")
        _report_shortcut("Desktop", folder)
    if want_startup:
        folder = args.startup_dir or _shell_folder("Startup")
        _report_shortcut("Startup", folder)
    print()


def _report_shortcut(label: str, folder: Path | None) -> None:
    if folder is None:
        print(f"{label} shortcut: could not locate the {label} folder.")
        return
    try:
        target = create_shortcut(folder / SHORTCUT_NAME)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"{label} shortcut: failed ({type(exc).__name__}: {exc})")
        return
    print(f"{label} shortcut: {target}")


def create_shortcut(link_path: Path) -> Path:
    """Write a .lnk that runs the startup script with the checkout's pythonw."""

    link_path.parent.mkdir(parents=True, exist_ok=True)
    # Values travel through the environment so paths with quotes or spaces
    # never have to be escaped into PowerShell source.
    env = dict(
        os.environ,
        UR_LINK=str(link_path),
        UR_TARGET=str(_pythonw()),
        UR_ARGUMENTS=f'"{START_SCRIPT}"',
        UR_WORKDIR=str(PROJECT_ROOT),
        UR_ICON=f"{ICON_FILE},0",
        UR_DESCRIPTION=SHORTCUT_DESCRIPTION,
    )
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:UR_LINK);"
        "$s.TargetPath=$env:UR_TARGET;$s.Arguments=$env:UR_ARGUMENTS;"
        "$s.WorkingDirectory=$env:UR_WORKDIR;$s.IconLocation=$env:UR_ICON;"
        "$s.Description=$env:UR_DESCRIPTION;$s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        check=True,
        capture_output=True,
        creationflags=NO_WINDOW,
    )
    return link_path


def _shell_folder(name: str) -> Path | None:
    """Ask Windows for a special folder so OneDrive-redirected Desktops work."""

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"[Environment]::GetFolderPath('{name}')"],
            check=True,
            capture_output=True,
            text=True,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return Path(value) if value else None


def _pythonw() -> Path:
    """Prefer the checkout's venv so the shortcut sees the installed PyQt6."""

    venv = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if venv.is_file():
        return venv
    sibling = Path(sys.executable).with_name("pythonw.exe")
    return sibling if sibling.is_file() else Path(sys.executable)


def _start_widgets() -> None:
    """Hand off to the same startup script the shortcuts run."""

    if not START_SCRIPT.is_file():
        print("Cannot start the widgets: scripts/start_with_update.py is missing.")
        return
    flags = NO_WINDOW
    if sys.platform == "win32":
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [str(_pythonw()), str(START_SCRIPT)],
        cwd=str(PROJECT_ROOT),
        close_fds=True,
        creationflags=flags,
    )
    print("Starting the widgets.")


def _decide(flag: bool | None, question: str, default: bool, interactive: bool) -> bool:
    """A flag wins; otherwise ask when interactive; otherwise stay off."""

    if flag is not None:
        return flag
    if not interactive:
        return False
    return _confirm(question, default)


def _confirm(question: str, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = _ask(f"{question} {suffix}", "").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def _ask(prompt: str, default: str) -> str:
    text = f"{prompt} [{default}]: " if default else f"{prompt} "
    try:
        answer = input(text).strip()
    except EOFError:
        return default
    return answer or default
