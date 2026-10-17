"""Provider-neutral usage types and the fetch plumbing every provider shares.

Each provider (see ``providers/``) only supplies what really differs: where
its credentials live, which endpoint and headers to use, and how to ask its
own CLI to renew an expired sign-in. Everything that was identical in the two
original widgets lives here so a fix lands in both at once.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("usage_rings.usage")

RATE_LIMIT_RETRIES = 2
RATE_LIMIT_FALLBACK_DELAY_SECONDS = 2.0
RATE_LIMIT_MAX_DELAY_SECONDS = 10.0
# Each CLI renews the sign-in, not the widget. Ask it at most once per
# cooldown window so a persistently rejected token cannot start the CLI on
# every refresh tick.
RENEWAL_COOLDOWN_SECONDS = 600
# Keep a CLI's console window hidden when the widget runs under pythonw.
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class UsageError(RuntimeError):
    pass


class RateLimitError(UsageError):
    """A temporary service throttle, not a credential or parsing failure."""


class NetworkError(UsageError):
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
class ProviderUsage:
    five_hour: UsageWindow | None
    weekly: UsageWindow | None
    plan_type: str | None


class RenewalCooldown:
    """Remembers when a CLI was last asked to renew the sign-in."""

    def __init__(self, seconds: float = RENEWAL_COOLDOWN_SECONDS) -> None:
        self._seconds = seconds
        self._last_attempt = 0.0

    def blocked(self) -> tuple[int, int] | None:
        """Return (seconds since the last ask, seconds left) while cooling down."""

        since = time.monotonic() - self._last_attempt
        if self._last_attempt and since < self._seconds:
            return int(since), int(self._seconds - since)
        return None

    def start(self) -> None:
        self._last_attempt = time.monotonic()


def http_get_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    """GET a JSON object, retrying a throttle (HTTP 429) a couple of times."""

    request = Request(url, headers=headers, method="GET")
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
            detail = safe_error_body(exc)
            error_type = RateLimitError if exc.code == 429 else UsageError
            raise error_type(f"HTTP {exc.code} while fetching usage: {detail}") from exc
        except URLError as exc:
            raise NetworkError(f"Network error while fetching usage: {exc.reason}") from exc
        except TimeoutError as exc:
            raise NetworkError("Timed out while fetching usage") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise UsageError("Usage response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise UsageError("Usage response does not contain a JSON object")
    return data


def fetch_with_renewal(
    fetch_once: Callable[[], dict[str, Any]],
    renew: Callable[[], None],
    still_rejected_message: str,
) -> dict[str, Any]:
    """Fetch with the stored token, asking the provider's CLI to renew on rejection.

    The stored token is always tried first, even when it looks expired: a
    rejected request proves the network is up, while an offline one raises
    ``NetworkError`` and never reaches the renewal.
    """

    try:
        return fetch_once()
    except UsageError as exc:
        if not is_auth_failure(exc):
            raise
        LOGGER.warning("usage request rejected (%s); asking the CLI to renew the sign-in", exc)

    renew()
    try:
        return fetch_once()
    except UsageError as exc:
        if not is_auth_failure(exc):
            raise
        raise UsageError(still_rejected_message) from exc


def is_auth_failure(exc: UsageError) -> bool:
    return str(exc).startswith("HTTP 401 ") or str(exc).startswith("HTTP 403 ")


def safe_error_body(exc: HTTPError) -> str:
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


def parse_rate_limit_payload(payload: dict[str, Any], now: int) -> ProviderUsage:
    """Parse the ``rate_limit`` shape: a primary and a secondary window."""

    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return ProviderUsage(five_hour=None, weekly=None, plan_type=plan_type(payload))

    primary = parse_window(rate_limit.get("primary_window"), now)
    secondary = parse_window(rate_limit.get("secondary_window"), now)

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


def parse_window(raw: Any, now: int) -> UsageWindow | None:
    if not isinstance(raw, dict):
        return None

    used_percent = as_float(raw.get("used_percent"))
    reset_at = as_int(raw.get("reset_at"))
    reset_after = as_int(raw.get("reset_after_seconds"))
    if reset_at is None and reset_after is not None:
        reset_at = now + reset_after

    return UsageWindow(
        used_percent=used_percent,
        reset_at=reset_at,
        window_seconds=as_int(raw.get("limit_window_seconds")),
    )


def plan_type(payload: dict[str, Any]) -> str | None:
    value = payload.get("plan_type")
    return value if isinstance(value, str) and value else None


def as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
