"""The per-user config file that records which widgets are enabled."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .providers import PROVIDERS

# Lets tests and scripted runs keep the config away from the real profile.
CONFIG_DIR_ENV = "USAGE_RINGS_CONFIG_DIR"


def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override)
    local_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local_appdata / "UsageRings"


def config_path() -> Path:
    return config_dir() / "config.json"


def load_providers() -> list[str] | None:
    """Return the enabled provider keys, or None when setup has not run.

    A config that exists but is unreadable or names nothing valid is treated
    like a missing one, so the startup script re-runs setup instead of
    silently starting nothing.
    """

    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("providers"), list):
        return None
    # Keep registry order and ignore unknown names (for example from a newer version).
    enabled = [key for key in PROVIDERS if key in data["providers"]]
    return enabled or None


def save_providers(keys: list[str]) -> Path:
    unknown = [key for key in keys if key not in PROVIDERS]
    if unknown:
        raise ValueError(f"Unknown provider(s): {', '.join(unknown)}")
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = [key for key in PROVIDERS if key in keys]
    path.write_text(json.dumps({"providers": ordered}, indent=2) + "\n", encoding="utf-8")
    return path
