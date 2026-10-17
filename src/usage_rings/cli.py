"""Entry point for the ``usage-rings`` command."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "setup":
        from .setup import main as setup_main

        return setup_main(args[1:])
    # Imported lazily so `usage-rings setup` works before PyQt6 is installed.
    from .app import main as app_main

    return app_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
