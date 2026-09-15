from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
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
# Claude Code's OAuth token endpoint is hosted on the platform domain. The
# console host used to accept this request, but now commonly returns 403.
DEFAULT_AUTH_BASE_URL = "https://platform.claude.com"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_SCOPE = "org:create_api_key user:profile user:inference"
OAUTH_USAGE_BETA = "oauth-2025-04-20"
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_FALLBACK_DELAY_SECONDS = 2.0
RATE_LIMIT_MAX_DELAY_SECONDS = 10.0
# Claude Code rotates the refresh token on every renewal. Renewing once a
# minute - which is what an unconditional refresh-on-403 does - gets the
# token endpoint to answer HTTP 429 and can invalidate the refresh token
# Claude Code itself is holding. Renew only when the stored token is
# actually expiring, and never more than once per cooldown window.
AUTH_EXPIRY_SKEW_SECONDS = 120
AUTH_REFRESH_COOLDOWN_SECONDS = 600

LOGGER = logging.getLogger("claude_code_usage_rings")
_LAST_REFRESH_ATTEMPT = 0.0


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
    auth = load_auth(auth_file)
    if _access_token_expired(auth):
        LOGGER.info("stored access token is expired; renewing before fetching usage")
        auth = _refresh_auth_guarded(auth, auth_file=auth_file, timeout=timeout, reason="expired")

    try:
        return fetch_usage(
            extract_access_token(auth),
            base_url,
            timeout,
        )
    except ClaudeUsageError as exc:
        if not _is_auth_failure(exc):
            raise
        LOGGER.warning("usage request rejected (%s); attempting a token renewal", exc)

    refreshed_auth = _refresh_auth_guarded(auth, auth_file=auth_file, timeout=timeout, reason="rejected")
    return fetch_usage(
        extract_access_token(refreshed_auth),
        base_url,
        timeout,
    )


def _refresh_auth_guarded(
    auth: dict[str, Any],
    *,
    auth_file: Path,
    timeout: float,
    reason: str,
) -> dict[str, Any]:
    """Renew the token at most once per cooldown window.

    Without this guard a persistently rejected usage request renews the token
    on every refresh tick. Claude Code rotates the refresh token on each
    renewal, so that loop both invites HTTP 429 from the token endpoint and
    races Claude Code for the credentials file, which is how the widget ends
    up unable to recover on its own.

    Only an attempt that reached the token endpoint starts the cooldown. An
    attempt made while offline never left the machine, and counting it used
    to lock renewal out for ten minutes after every Wi-Fi reconnect.
    """

    global _LAST_REFRESH_ATTEMPT

    now = time.monotonic()
    since = now - _LAST_REFRESH_ATTEMPT
    if _LAST_REFRESH_ATTEMPT and since < AUTH_REFRESH_COOLDOWN_SECONDS:
        wait = int(AUTH_REFRESH_COOLDOWN_SECONDS - since)
        if reason == "expired":
            raise ClaudeUsageError(
                f"The access token expired and a renewal was attempted {int(since)}s ago. "
                f"Retrying the renewal in {wait}s."
            )
        raise ClaudeUsageError(
            f"Claude rejected the access token; a renewal was attempted {int(since)}s ago. "
            f"Retrying the renewal in {wait}s; run `claude auth login --claudeai` if this persists."
        )
    try:
        refreshed = refresh_auth(auth, auth_file=auth_file, timeout=timeout)
    except ClaudeNetworkError:
        raise
    except ClaudeUsageError:
        _LAST_REFRESH_ATTEMPT = now
        raise
    _LAST_REFRESH_ATTEMPT = now
    return refreshed


def _access_token_expired(auth: dict[str, Any], now: float | None = None) -> bool:
    """Report whether the stored access token is at or past its expiry."""

    expires_at = None
    for tokens in _credential_containers(auth):
        for key in ("expiresAt", "expires_at"):
            expires_at = _as_int(tokens.get(key))
            if expires_at is not None:
                break
        if expires_at is not None:
            break
    if expires_at is None:
        return False
    # Credentials store milliseconds; tolerate a seconds-based value too.
    seconds = expires_at / 1000 if expires_at > 10_000_000_000 else expires_at
    current = time.time() if now is None else now
    return seconds - AUTH_EXPIRY_SKEW_SECONDS <= current


def refresh_auth(
    auth: dict[str, Any],
    *,
    auth_file: Path,
    timeout: float,
    auth_base_url: str = DEFAULT_AUTH_BASE_URL,
) -> dict[str, Any]:
    tokens = next(iter(_credential_containers(auth)), auth)
    refresh_token = _token_value(tokens, "refreshToken", "refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise ClaudeUsageError("Claude Code credentials have no refreshToken for renewal")

    # Claude Code sends an OAuth form post. In particular, the endpoint does
    # not treat the equivalent JSON body as a token refresh request, and the
    # scope is required for subscription OAuth credentials.
    payload = urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": OAUTH_CLIENT_ID,
            "refresh_token": refresh_token.strip(),
            "scope": OAUTH_SCOPE,
        }
    ).encode("utf-8")
    request = Request(
        f"{auth_base_url.rstrip('/')}/v1/oauth/token",
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "claude-code/2.1.270",
        },
        method="POST",
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
            raise error_type(f"Auth refresh failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise ClaudeNetworkError(f"Network error while refreshing auth: {exc.reason}") from exc
        except TimeoutError as exc:
            # Unlike a failed connection, a read timeout may mean the endpoint
            # did rotate the token, so it is not reported as a network error.
            raise ClaudeUsageError("Timed out while refreshing auth") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ClaudeUsageError("Auth refresh response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ClaudeUsageError("Auth refresh response does not contain a JSON object")

    access_token = _token_value(data, "access_token", "accessToken")
    next_refresh_token = _token_value(data, "refresh_token", "refreshToken") or refresh_token.strip()
    id_token = _token_value(data, "id_token", "idToken")
    if not access_token:
        raise ClaudeUsageError("Auth refresh response did not include an access token")

    next_auth = dict(auth)
    next_tokens = dict(tokens)
    uses_camel_case = "claudeAiOauth" in auth or "accessToken" in next_tokens
    access_key = "accessToken" if uses_camel_case else "access_token"
    refresh_key = "refreshToken" if uses_camel_case else "refresh_token"
    next_tokens[access_key] = access_token
    next_tokens[refresh_key] = next_refresh_token
    expires_in = _as_int(data.get("expires_in") or data.get("expiresIn"))
    if expires_in is not None:
        next_tokens["expiresAt" if uses_camel_case else "expires_at"] = int(time.time() * 1000) + expires_in * 1000
    if id_token:
        next_tokens["idToken" if uses_camel_case else "id_token"] = id_token
        account_id = _account_id_from_id_token(id_token)
        if account_id:
            next_tokens["accountId" if uses_camel_case else "account_id"] = account_id
    if "claudeAiOauth" in auth:
        next_auth["claudeAiOauth"] = next_tokens
    elif "tokens" in auth:
        next_auth["tokens"] = next_tokens
    else:
        next_auth.update(next_tokens)
    next_auth["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_auth(auth_file, next_auth)
    LOGGER.info("renewed Claude Code OAuth credentials")
    return next_auth


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


def _token_value(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _write_auth(path: Path, auth: dict[str, Any]) -> None:
    # Claude Code holds this file open on Windows, so a failed replace is an
    # expected outcome rather than a crash. Raise the project's own error type
    # so the caller reports it instead of killing the worker thread.
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(auth, indent=2), encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ClaudeUsageError(f"Cannot update Claude Code credentials at {path}: {exc}") from exc


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
