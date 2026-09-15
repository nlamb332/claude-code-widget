from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_FALLBACK_DELAY_SECONDS = 2.0
RATE_LIMIT_MAX_DELAY_SECONDS = 10.0
# Claude Code renews the sign-in, not the widget (see
# _renew_with_claude_code_guarded). Ask it at most once per cooldown window so
# a persistently rejected token cannot start the CLI on every refresh tick.
RENEWAL_COOLDOWN_SECONDS = 600
CLI_RENEWAL_TIMEOUT_SECONDS = 90
# Keep the CLI's console window hidden when the widget runs under pythonw.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
# The Claude desktop app sets these for its own sessions; passing them on would
# make the CLI act as part of that session instead of renewing the standalone
# sign-in. The config-dir overrides must survive so it renews the file we read.
_KEPT_CLAUDE_VARIABLES = frozenset({"CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR"})

LOGGER = logging.getLogger("claude_code_usage_rings")
_LAST_RENEWAL_ATTEMPT = 0.0


class ClaudeUsageError(RuntimeError):
    pass


class ClaudeRateLimitError(ClaudeUsageError):
    """A temporary service throttle, not a credential or parsing failure."""


class ClaudeNetworkError(ClaudeUsageError):
    """No connection was made (for example, Wi-Fi is down), so nothing changed server-side."""


@dataclass(frozen=True)
class UsageWindow:
    used_percent: float | None
    reset_at: int | None
    window_seconds: int | None

    @property
    def remaining_percent(self) -> float | None:
        if self.used_percent is None:
            return None
        return max(0.0, 100.0 - self.used_percent)


@dataclass(frozen=True)
class ClaudeUsage:
    five_hour: UsageWindow | None
    weekly: UsageWindow | None
    plan_type: str | None


def load_auth(path: Path) -> dict[str, Any]:
    requested = Path(path).expanduser()
    candidates = _auth_file_candidates(requested)
    existing = next((candidate for candidate in candidates if candidate.is_file()), None)
    if existing is None:
        searched = ", ".join(str(candidate) for candidate in candidates)
        raise ClaudeUsageError(
            "Claude Code credentials were not found. Sign in with `claude auth login` "
            f"or set CLAUDE_AUTH_FILE. Searched: {searched}"
        )
    try:
        with existing.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ClaudeUsageError(f"Cannot read Claude Code credentials: {existing}") from exc
    except json.JSONDecodeError as exc:
        raise ClaudeUsageError(f"Invalid Claude Code credentials file: {existing}") from exc
    if not isinstance(data, dict):
        raise ClaudeUsageError("Auth file does not contain a JSON object")
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
    raise ClaudeUsageError("Claude Code credentials do not contain claudeAiOauth.accessToken")


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

    raise ClaudeUsageError("Claude Code credentials do not contain an account id")


def fetch_usage(access_token: str, base_url: str, timeout: float) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/oauth/usage"
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": OAUTH_USAGE_BETA,
            "Accept": "application/json",
            "User-Agent": "claude-code/2.1.270",
        },
        method="GET",
    )
    rate_limit_attempts = 0
    while True:
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
            break
        except HTTPError as exc:
            if exc.code == 429 and rate_limit_attempts < RATE_LIMIT_RETRIES:
                rate_limit_attempts += 1
                _pause_for_rate_limit(exc)
                continue
            detail = _safe_error_body(exc)
            error_type = ClaudeRateLimitError if exc.code == 429 else ClaudeUsageError
            raise error_type(f"HTTP {exc.code} while fetching usage: {detail}") from exc
        except URLError as exc:
            raise ClaudeNetworkError(f"Network error while fetching usage: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ClaudeNetworkError("Timed out while fetching usage") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ClaudeUsageError("Usage response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ClaudeUsageError("Usage response does not contain a JSON object")
    return data


def fetch_usage_with_auth_refresh(auth_file: Path, base_url: str, timeout: float) -> dict[str, Any]:
    # Always try the stored token first, even when it looks expired. A rejected
    # request proves the network is up; an offline one raises ClaudeNetworkError
    # and never reaches the renewal below.
    try:
        return fetch_usage(extract_access_token(load_auth(auth_file)), base_url, timeout)
    except ClaudeUsageError as exc:
        if not _is_auth_failure(exc):
            raise
        LOGGER.warning("usage request rejected (%s); asking Claude Code to renew the sign-in", exc)

    _renew_with_claude_code_guarded()
    try:
        return fetch_usage(extract_access_token(load_auth(auth_file)), base_url, timeout)
    except ClaudeUsageError as exc:
        if not _is_auth_failure(exc):
            raise
        raise ClaudeUsageError(
            "Claude still rejects the sign-in after Claude Code tried to renew it. "
            "Run `claude auth login --claudeai`."
        ) from exc


def _renew_with_claude_code_guarded() -> None:
    """Have Claude Code renew its own sign-in, at most once per cooldown window.

    The token endpoint answers the widget's own renewal requests with HTTP 429,
    even on the first use of a fresh refresh token, while it accepts Claude
    Code's. So the widget never renews or writes the credentials file itself:
    it runs Claude Code's local /usage command, which renews an expired sign-in
    as part of reading usage, and the caller then reads the file again.
    """

    global _LAST_RENEWAL_ATTEMPT

    now = time.monotonic()
    since = now - _LAST_RENEWAL_ATTEMPT
    if _LAST_RENEWAL_ATTEMPT and since < RENEWAL_COOLDOWN_SECONDS:
        raise ClaudeUsageError(
            f"Claude rejected the access token; Claude Code was asked to renew it {int(since)}s ago. "
            f"Trying again in {int(RENEWAL_COOLDOWN_SECONDS - since)}s; "
            "run `claude auth login --claudeai` if this persists."
        )
    cli = find_claude_cli()
    if cli is None:
        raise ClaudeUsageError(
            "The sign-in needs renewing but the Claude CLI was not found. "
            "Run `claude auth login --claudeai`, or set CLAUDE_USAGE_CLI to claude.exe."
        )
    _LAST_RENEWAL_ATTEMPT = now
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
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeUsageError(f"Could not run Claude Code to renew the sign-in ({type(exc).__name__})") from exc
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

    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        # The MSIX-packaged desktop app keeps its bundled CLI in the package's
        # virtualized roaming directory, one folder per version.
        candidates.extend(
            Path(local_app_data, "Packages").glob("Claude_*/LocalCache/Roaming/Claude/claude-code/*/claude.exe")
        )
    app_data = os.environ.get("APPDATA")
    if app_data:
        candidates.extend(Path(app_data, "Claude", "claude-code").glob("*/claude.exe"))
    candidates.append(Path.home() / ".local" / "bin" / "claude.exe")
    existing = [candidate for candidate in candidates if candidate.is_file()]
    if not existing:
        return None
    return str(max(existing, key=_cli_version_key))


def _cli_version_key(path: Path) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in path.parent.name.split("."))
    except ValueError:
        return (0,)


def parse_usage_payload(payload: dict[str, Any], now: int | None = None) -> ClaudeUsage:
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
        return ClaudeUsage(five_hour=five_hour, weekly=weekly, plan_type=_plan_type(payload))

    # Keep accepting the older internal rate_limit shape for installations
    # running a Claude Code proxy or an older client.
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return ClaudeUsage(five_hour=None, weekly=None, plan_type=_plan_type(payload))

    primary = _parse_window(rate_limit.get("primary_window"), now_epoch)
    secondary = _parse_window(rate_limit.get("secondary_window"), now_epoch)

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

    return ClaudeUsage(five_hour=five_hour, weekly=weekly, plan_type=_plan_type(payload))


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


def _safe_error_body(exc: HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return exc.reason or "error without details"
    if not raw:
        return exc.reason or "error without details"
    return raw[:500]


def _pause_for_rate_limit(exc: HTTPError) -> None:
    """Honor Retry-After without allowing a background refresh to hang."""

    delay = RATE_LIMIT_FALLBACK_DELAY_SECONDS
    retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = RATE_LIMIT_MAX_DELAY_SECONDS
    try:
        exc.close()
    except Exception:
        pass
    time.sleep(max(0.5, min(delay, RATE_LIMIT_MAX_DELAY_SECONDS)))


def _is_auth_failure(exc: ClaudeUsageError) -> bool:
    return str(exc).startswith("HTTP 401 ") or str(exc).startswith("HTTP 403 ")


def _parse_window(raw: Any, now: int) -> UsageWindow | None:
    if not isinstance(raw, dict):
        return None

    used_percent = _as_float(raw.get("used_percent"))
    reset_at = _as_int(raw.get("reset_at"))
    reset_after = _as_int(raw.get("reset_after_seconds"))
    if reset_at is None and reset_after is not None:
        reset_at = now + reset_after

    return UsageWindow(
        used_percent=used_percent,
        reset_at=reset_at,
        window_seconds=_as_int(raw.get("limit_window_seconds")),
    )


def _parse_usage_window(raw: Any, now: int) -> UsageWindow | None:
    if not isinstance(raw, dict):
        return None

    used_percent = None
    for key in ("utilization", "percentage", "percent", "usage", "used_percent"):
        if key not in raw:
            continue
        used_percent = _as_float(raw.get(key))
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

    reset_after = _as_int(raw.get("reset_after_seconds"))
    return now + reset_after if reset_after is not None else None


def _plan_type(payload: dict[str, Any]) -> str | None:
    value = payload.get("plan_type")
    return value if isinstance(value, str) and value else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
