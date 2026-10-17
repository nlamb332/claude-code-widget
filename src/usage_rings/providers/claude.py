"""Claude Code provider: OAuth credentials file, usage endpoint, CLI renewal."""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..usage import (
    NO_WINDOW,
    ProviderUsage,
    RenewalCooldown,
    UsageError,
    UsageWindow,
    as_float,
    as_int,
    fetch_with_renewal,
    http_get_json,
    parse_window,
    plan_type,
)
from .base import HostWindowSpec, Prerequisites, Provider

LOGGER = logging.getLogger("usage_rings.claude")


# Claude Code stores subscription OAuth credentials in this file on Windows
# and Linux. Respect profile overrides used by Claude Code before falling back
# to the normal per-user location.
def _default_auth_file() -> Path:
    for variable in ("CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_CONFIG_DIR"):
        configured = os.environ.get(variable)
        if configured:
            return Path(configured).expanduser() / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


DEFAULT_AUTH_FILE = _default_auth_file()
DEFAULT_BASE_URL = "https://api.anthropic.com"
OAUTH_USAGE_BETA = "oauth-2025-04-20"
CLI_RENEWAL_TIMEOUT_SECONDS = 90
# The Claude desktop app sets these for its own sessions; passing them on would
# make the CLI act as part of that session instead of renewing the standalone
# sign-in. The config-dir overrides must survive so it renews the file we read.
_KEPT_CLAUDE_VARIABLES = frozenset({"CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR"})

_RENEWAL = RenewalCooldown()


def load_auth(path: Path) -> dict[str, Any]:
    requested = Path(path).expanduser()
    candidates = _auth_file_candidates(requested)
    existing = next((candidate for candidate in candidates if candidate.is_file()), None)
    if existing is None:
        searched = ", ".join(str(candidate) for candidate in candidates)
        raise UsageError(
            "Claude Code credentials were not found. Sign in with `claude auth login` "
            f"or set CLAUDE_AUTH_FILE. Searched: {searched}"
        )
    try:
        with existing.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise UsageError(f"Cannot read Claude Code credentials: {existing}") from exc
    except json.JSONDecodeError as exc:
        raise UsageError(f"Invalid Claude Code credentials file: {existing}") from exc
    if not isinstance(data, dict):
        raise UsageError("Auth file does not contain a JSON object")
    return data


def _auth_file_candidates(requested: Path) -> list[Path]:
    candidates = [requested]
    if requested == DEFAULT_AUTH_FILE:
        for variable in ("CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_CONFIG_DIR"):
            configured = os.environ.get(variable)
            if configured:
                candidates.append(Path(configured).expanduser() / ".credentials.json")

        # Claude's MSIX package virtualizes its roaming directory. Include a
        # plain credentials file there when Claude Code is configured to use
        # that profile, but never treat the encrypted desktop config.json as a
        # credentials file.
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            packages = Path(local_app_data) / "Packages"
            if packages.is_dir():
                candidates.extend(
                    package / "LocalCache" / "Roaming" / "Claude" / ".credentials.json"
                    for package in packages.glob("Claude_*")
                    if package.is_dir()
                )

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def _credential_containers(auth: dict[str, Any]) -> list[dict[str, Any]]:
    """Return Claude Code credential objects in priority order.

    In particular, do not accidentally select an access token belonging to an
    MCP server or another nested integration before claudeAiOauth.
    """

    containers: list[dict[str, Any]] = []
    for key in ("claudeAiOauth", "claude_ai_oauth", "tokens"):
        value = auth.get(key)
        if isinstance(value, dict):
            containers.append(value)
    containers.append(auth)
    return containers


def extract_access_token(auth: dict[str, Any]) -> str:
    for tokens in _credential_containers(auth):
        for key in ("accessToken", "access_token"):
            value = tokens.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    raise UsageError("Claude Code credentials do not contain claudeAiOauth.accessToken")


def extract_account_id(auth: dict[str, Any]) -> str:
    for tokens in _credential_containers(auth):
        for key in ("account_id", "accountId"):
            value = tokens.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        id_token = tokens.get("id_token") or tokens.get("idToken")
        if isinstance(id_token, str):
            account_id = _account_id_from_id_token(id_token)
            if account_id:
                return account_id

    raise UsageError("Claude Code credentials do not contain an account id")


def fetch_usage(access_token: str, base_url: str, timeout: float) -> dict[str, Any]:
    return http_get_json(
        f"{base_url.rstrip('/')}/api/oauth/usage",
        {
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": OAUTH_USAGE_BETA,
            "Accept": "application/json",
            "User-Agent": "claude-code/2.1.270",
        },
        timeout,
    )


def fetch_usage_with_auth_refresh(auth_file: Path, base_url: str, timeout: float) -> dict[str, Any]:
    return fetch_with_renewal(
        lambda: fetch_usage(extract_access_token(load_auth(auth_file)), base_url, timeout),
        _renew_with_claude_code_guarded,
        "Claude still rejects the sign-in after Claude Code tried to renew it. "
        "Run `claude auth login --claudeai`.",
    )


def _renew_with_claude_code_guarded() -> None:
    """Have Claude Code renew its own sign-in, at most once per cooldown window.

    The token endpoint answers the widget's own renewal requests with HTTP 429,
    even on the first use of a fresh refresh token, while it accepts Claude
    Code's. So the widget never renews or writes the credentials file itself:
    it runs Claude Code's local /usage command, which renews an expired sign-in
    as part of reading usage, and the caller then reads the file again.
    """

    blocked = _RENEWAL.blocked()
    if blocked is not None:
        since, remaining = blocked
        raise UsageError(
            f"Claude rejected the access token; Claude Code was asked to renew it {since}s ago. "
            f"Trying again in {remaining}s; "
            "run `claude auth login --claudeai` if this persists."
        )
    cli = find_claude_cli()
    if cli is None:
        raise UsageError(
            "The sign-in needs renewing but the Claude CLI was not found. "
            "Run `claude auth login --claudeai`, or set CLAUDE_USAGE_CLI to claude.exe."
        )
    _RENEWAL.start()
    _run_claude_usage_command(cli)


def _run_claude_usage_command(cli: str) -> None:
    """Run `claude -p /usage`: a local command, so it makes no model call."""

    env = {name: value for name, value in os.environ.items() if not _is_host_session_variable(name)}
    try:
        result = subprocess.run(
            [cli, "-p", "/usage"],
            env=env,
            cwd=str(Path.home()),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLI_RENEWAL_TIMEOUT_SECONDS,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UsageError(f"Could not run Claude Code to renew the sign-in ({type(exc).__name__})") from exc
    if result.returncode == 0:
        LOGGER.info("asked Claude Code to renew the sign-in")
        return
    detail = next((line.strip() for line in (result.stderr or result.stdout or "").splitlines() if line.strip()), "")
    LOGGER.warning("Claude Code exited %d while renewing the sign-in: %s", result.returncode, detail[:200])


def _is_host_session_variable(name: str) -> bool:
    upper = name.upper()
    return upper.startswith(("CLAUDE", "ANTHROPIC")) and upper not in _KEPT_CLAUDE_VARIABLES


def find_claude_cli() -> str | None:
    """Locate a Claude Code CLI: an override, PATH, then the desktop app's copy."""

    configured = os.environ.get("CLAUDE_USAGE_CLI")
    if configured and Path(configured).is_file():
        return configured
    on_path = shutil.which("claude")
    if on_path:
        return on_path

    # The desktop app keeps its bundled CLI one folder per version; newer
    # releases nest it one level deeper, under a build-hash folder.
    bundled_patterns = ("*/claude.exe", "*/*/claude.exe")
    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        # The MSIX-packaged app uses the package's virtualized roaming directory.
        packaged = Path(local_app_data, "Packages")
        for pattern in bundled_patterns:
            candidates.extend(packaged.glob(f"Claude_*/LocalCache/Roaming/Claude/claude-code/{pattern}"))
    app_data = os.environ.get("APPDATA")
    if app_data:
        for pattern in bundled_patterns:
            candidates.extend(Path(app_data, "Claude", "claude-code").glob(pattern))
    candidates.append(Path.home() / ".local" / "bin" / "claude.exe")
    existing = [candidate for candidate in candidates if candidate.is_file()]
    if not existing:
        return None
    return str(max(existing, key=_cli_version_key))


def _cli_version_key(path: Path) -> tuple[int, ...]:
    for folder in (path.parent, path.parent.parent):
        try:
            return tuple(int(part) for part in folder.name.split("."))
        except ValueError:
            continue
    return (0,)


def parse_usage_payload(payload: dict[str, Any], now: int | None = None) -> ProviderUsage:
    now_epoch = int(time.time()) if now is None else now

    five_hour = _parse_usage_window(payload.get("five_hour"), now_epoch)
    weekly = _parse_usage_window(payload.get("seven_day"), now_epoch)
    limits = payload.get("limits")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            if kind == "session" and five_hour is None:
                five_hour = _parse_usage_window(item, now_epoch)
            elif kind == "weekly_all" and weekly is None:
                weekly = _parse_usage_window(item, now_epoch)

    if five_hour is not None or weekly is not None:
        return ProviderUsage(five_hour=five_hour, weekly=weekly, plan_type=plan_type(payload))

    # Keep accepting the older internal rate_limit shape for installations
    # running a Claude Code proxy or an older client.
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return ProviderUsage(five_hour=None, weekly=None, plan_type=plan_type(payload))

    primary = parse_window(rate_limit.get("primary_window"), now_epoch)
    secondary = parse_window(rate_limit.get("secondary_window"), now_epoch)

    five_hour = None
    weekly = None
    for window in (primary, secondary):
        if window is None:
            continue
        if window.window_seconds == 604800:
            weekly = window
        elif window.window_seconds == 18000:
            five_hour = window

    if five_hour is None and primary is not None and primary.window_seconds != 604800:
        five_hour = primary
    if weekly is None and secondary is not None:
        weekly = secondary

    return ProviderUsage(five_hour=five_hour, weekly=weekly, plan_type=plan_type(payload))


def _account_id_from_id_token(id_token: str) -> str | None:
    parts = id_token.split(".")
    if len(parts) < 2:
        return None
    try:
        payload = parts[1] + ("=" * (-len(parts[1]) % 4))
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(claims, dict):
        return None

    value = claims.get("account_id") or claims.get("anthropic_account_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_usage_window(raw: Any, now: int) -> UsageWindow | None:
    if not isinstance(raw, dict):
        return None

    used_percent = None
    for key in ("utilization", "percentage", "percent", "usage", "used_percent"):
        if key not in raw:
            continue
        used_percent = as_float(raw.get(key))
        if used_percent is not None:
            # Some clients expose utilization as a fraction while the OAuth
            # usage body normally returns a percentage.
            if key == "utilization" and 0 <= used_percent <= 1:
                used_percent *= 100
            break

    reset_at = _reset_epoch(raw, now)
    return UsageWindow(
        used_percent=used_percent,
        reset_at=reset_at,
        window_seconds=None,
    )


def _reset_epoch(raw: dict[str, Any], now: int) -> int | None:
    for key in ("resets_at", "resetsAt", "reset_at", "expires_at"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # Epoch milliseconds are used by credentials; usage payloads use
            # seconds when they return a numeric reset.
            return int(value / 1000) if value > 10_000_000_000 else int(value)
        if isinstance(value, str) and value.strip():
            try:
                normalized = value.strip().replace("Z", "+00:00")
                return int(datetime.fromisoformat(normalized).timestamp())
            except ValueError:
                continue

    reset_after = as_int(raw.get("reset_after_seconds"))
    return now + reset_after if reset_after is not None else None


def check_prerequisites(auth_file: Path | None = None) -> Prerequisites:
    """Is Claude Code signed in? Uses the same lookup the widget uses at runtime."""

    path = Path(auth_file) if auth_file is not None else Path(os.environ.get("CLAUDE_AUTH_FILE", DEFAULT_AUTH_FILE))
    details: list[str] = []
    cli = find_claude_cli()
    details.append(f"Claude CLI: {cli}" if cli else "Claude CLI: not found (needed only to renew an expired sign-in)")
    try:
        extract_access_token(load_auth(path))
    except UsageError as exc:
        return Prerequisites(
            ready=False,
            details=[str(exc), *details],
            hint="Install Claude Code, then run `claude auth login --claudeai`.",
        )
    details.insert(0, f"Credentials found: {path}")
    return Prerequisites(ready=True, details=details)


PROVIDER = Provider(
    key="claude",
    app_name="Claude",
    log_dir_name="ClaudeCodeUsageRings",
    instance_mutex="Local\\ClaudeCodeUsageRings",
    supervisor_mutex="Local\\ClaudeCodeUsageRingsSupervisor",
    host=HostWindowSpec(
        # The Windows Claude desktop shell ships as Claude.exe. "claude.exe"
        # also covers the Claude Code command-line host running as a console
        # process.
        executable_names=frozenset({"claude.exe"}),
        title_tokens=("claude",),
        require_window=False,
    ),
    default_auth_file=DEFAULT_AUTH_FILE,
    auth_file_env="CLAUDE_AUTH_FILE",
    default_base_url=DEFAULT_BASE_URL,
    base_url_env="CLAUDE_USAGE_BASE_URL",
    fetch_usage=fetch_usage_with_auth_refresh,
    parse_usage=parse_usage_payload,
    check_prerequisites=check_prerequisites,
)
