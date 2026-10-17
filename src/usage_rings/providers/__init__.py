"""Registry of the usage providers the widget can follow."""

from __future__ import annotations

from .base import HostWindowSpec, Prerequisites, Provider
from .claude import PROVIDER as CLAUDE
from .codex import PROVIDER as CODEX

# Order is the order setup lists them in and the order they are started in.
PROVIDERS: dict[str, Provider] = {provider.key: provider for provider in (CLAUDE, CODEX)}


def get_provider(key: str) -> Provider:
    try:
        return PROVIDERS[key]
    except KeyError:
        raise ValueError(f"Unknown provider {key!r}; choose from: {', '.join(PROVIDERS)}") from None


__all__ = ["CLAUDE", "CODEX", "PROVIDERS", "HostWindowSpec", "Prerequisites", "Provider", "get_provider"]
