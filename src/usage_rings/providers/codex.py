"""Codex provider: auth.json tokens, wham usage endpoint, app-server renewal."""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ..usage import (
    NO_WINDOW,
    ProviderUsage,
    RenewalCooldown,
    UsageError,
    fetch_with_renewal,
    http_get_json,
    parse_rate_limit_payload,
)
from .base import HostWindowSpec, Prerequisites, Provider

LOGGER = logging.getLogger("usage_rings.codex")

DEFAULT_AUTH_FILE = Path.home() / ".codex" / "auth.json"
DEFAULT_BASE_URL = "https://chatgpt.com/backend-api"
CLI_RENEWAL_TIMEOUT_SECONDS = 60

_RENEWAL = RenewalCooldown()


def load_auth(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise UsageError(f"Auth file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise UsageError(f"Invalid auth file: {path}") from exc
    if not isinstance(data, dict):
        raise UsageError("Auth file does not contain a JSON object")
    return data


def extract_access_token(auth: dict[str, Any]) -> str:
    tokens = auth.get("tokens")
    if isinstance(tokens, dict):
        for key in ("access_token", "accessToken"):
            value = tokens.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    raise UsageError("Could not find tokens.access_token in auth.json")


def extract_account_id(auth: dict[str, Any]) -> str:
    tokens = auth.get("tokens")
    if isinstance(tokens, dict):
        for key in ("account_id", "accountId"):
            value = tokens.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        id_token = tokens.get("id_token") or tokens.get("idToken")
        if isinstance(id_token, str):
            account_id = _account_id_from_id_token(id_token)
            if account_id:
                return account_id

    raise UsageError("Could not find chatgpt-account-id in auth.json")


def fetch_usage(access_token: str, account_id: str, base_url: str, timeout: float) -> dict[str, Any]:
    return http_get_json(
        f"{base_url.rstrip('/')}/wham/usage",
        {
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
            "Accept": "application/json",
        },
        timeout,
    )


def fetch_usage_with_auth_refresh(auth_file: Path, base_url: str, timeout: float) -> dict[str, Any]:
    return fetch_with_renewal(
        lambda: _fetch_with_stored_auth(auth_file, base_url, timeout),
        _renew_with_codex_guarded,
        "Codex still rejects the sign-in after Codex tried to renew it. Run `codex login`.",
    )


def _fetch_with_stored_auth(auth_file: Path, base_url: str, timeout: float) -> dict[str, Any]:
    auth = load_auth(auth_file)
    return fetch_usage(extract_access_token(auth), extract_account_id(auth), base_url, timeout)


def _renew_with_codex_guarded() -> None:
    """Have Codex renew its own sign-in, at most once per cooldown window.

    The widget never renews or writes auth.json itself. The Codex app keeps the
    same refresh token in memory and OpenAI rotates it on every renewal, so a
    renewal by the widget could leave Codex holding a spent token and sign it
    out. Instead the widget asks Codex's app server for the account with
    refreshToken=true, which runs Codex's normal refresh flow without a model
    call, and the caller then reads auth.json again.
    """

    blocked = _RENEWAL.blocked()
    if blocked is not None:
        since, remaining = blocked
        raise UsageError(
            f"Codex rejected the access token; Codex was asked to renew it {since}s ago. "
            f"Trying again in {remaining}s; run `codex login` if this persists."
        )
    cli = find_codex_cli()
    if cli is None:
        raise UsageError(
            "The sign-in needs renewing but the Codex CLI was not found. "
            "Run `codex login`, or set CODEX_USAGE_CLI to codex.exe."
        )
    _RENEWAL.start()
    reply = _request_account_refresh(_codex_app_server_command(cli))
    if "error" in reply:
        error = reply.get("error")
        message = error.get("message") if isinstance(error, dict) else error
        raise UsageError(f"Codex could not renew the sign-in: {str(message or 'unknown error')[:200]}")
    result = reply.get("result")
    if not isinstance(result, dict) or not result.get("account"):
        raise UsageError("Codex is signed out. Run `codex login`.")
    LOGGER.info("asked Codex to renew the sign-in")


def _codex_app_server_command(cli: str) -> list[str]:
    return [cli, "app-server"]


def _request_account_refresh(command: list[str]) -> dict[str, Any]:
    """Speak just enough of the app-server protocol to request account/read."""

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(Path.home()),
            creationflags=NO_WINDOW,
        )
    except OSError as exc:
        raise UsageError(f"Could not start Codex to renew the sign-in ({type(exc).__name__})") from exc

    messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
    threading.Thread(target=_pump_messages, args=(process.stdout, messages), daemon=True).start()
    deadline = time.monotonic() + CLI_RENEWAL_TIMEOUT_SECONDS
    try:
        _send_message(
            process,
            {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex-usage-rings", "version": "1"}}},
        )
        _await_reply(messages, 1, deadline)
        _send_message(process, {"method": "initialized"})
        _send_message(process, {"id": 2, "method": "account/read", "params": {"refreshToken": True}})
        return _await_reply(messages, 2, deadline)
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _send_message(process: subprocess.Popen, message: dict[str, Any]) -> None:
    try:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
    except OSError as exc:
        raise UsageError("Codex exited before the sign-in could be renewed") from exc


def _pump_messages(stream, messages: queue.Queue) -> None:
    for line in stream:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            messages.put(message)
    messages.put(None)


def _await_reply(messages: queue.Queue, request_id: int, deadline: float) -> dict[str, Any]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UsageError("Timed out waiting for Codex to renew the sign-in")
        try:
            message = messages.get(timeout=remaining)
        except queue.Empty:
            continue
        if message is None:
            raise UsageError("Codex exited before the sign-in could be renewed")
        if message.get("id") == request_id and ("result" in message or "error" in message):
            return message


def find_codex_cli() -> str | None:
    """Locate the Codex CLI: an override, PATH, then the Codex app's copy."""

    configured = os.environ.get("CODEX_USAGE_CLI")
    if configured and Path(configured).is_file():
        return configured
    on_path = shutil.which("codex")
    if on_path:
        return on_path
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    bundled = Path(local_app_data, "OpenAI", "Codex", "bin")
    if (bundled / "codex.exe").is_file():
        return str(bundled / "codex.exe")
    versions = sorted(bundled.glob("*/codex.exe"), key=lambda path: path.stat().st_mtime)
    return str(versions[-1]) if versions else None


def parse_usage_payload(payload: dict[str, Any], now: int | None = None) -> ProviderUsage:
    now_epoch = int(time.time()) if now is None else now
    return parse_rate_limit_payload(payload, now_epoch)


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

    auth_claims = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claims, dict):
        value = auth_claims.get("chatgpt_account_id")
        if isinstance(value, str) and value.strip():
            return value.strip()

    value = claims.get("chatgpt_account_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def check_prerequisites(auth_file: Path | None = None) -> Prerequisites:
    """Is Codex signed in? Uses the same lookup the widget uses at runtime."""

    path = Path(auth_file) if auth_file is not None else Path(os.environ.get("CODEX_AUTH_FILE", DEFAULT_AUTH_FILE))
    details: list[str] = []
    cli = find_codex_cli()
    details.append(f"Codex CLI: {cli}" if cli else "Codex CLI: not found (needed only to renew an expired sign-in)")
    try:
        auth = load_auth(path)
        extract_access_token(auth)
        extract_account_id(auth)
    except UsageError as exc:
        return Prerequisites(
            ready=False,
            details=[str(exc), *details],
            hint="Install the ChatGPT desktop app (it includes Codex), open Codex and sign in, or run `codex login`.",
        )
    details.insert(0, f"Credentials found: {path}")
    return Prerequisites(ready=True, details=details)


PROVIDER = Provider(
    key="codex",
    app_name="Codex",
    log_dir_name="CodexUsageRings",
    instance_mutex="Local\\CodexUsageRings",
    supervisor_mutex="Local\\CodexUsageRingsSupervisor",
    host=HostWindowSpec(
        # The current Windows Codex desktop shell is packaged as ChatGPT.exe.
        # Older builds and the command host use codex.exe, so both names are
        # intentional.
        executable_names=frozenset({"codex.exe", "chatgpt.exe"}),
        title_tokens=("codex", "chatgpt"),
        require_window=True,
    ),
    default_auth_file=DEFAULT_AUTH_FILE,
    auth_file_env="CODEX_AUTH_FILE",
    default_base_url=DEFAULT_BASE_URL,
    base_url_env="CODEX_USAGE_BASE_URL",
    fetch_usage=fetch_usage_with_auth_refresh,
    parse_usage=parse_usage_payload,
    check_prerequisites=check_prerequisites,
)
