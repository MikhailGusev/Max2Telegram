"""Точка входа.

    python -m maxbridge            запустить мост
    python -m maxbridge login      разовый вход в MAX по SMS-коду
    python -m maxbridge check      проверить конфигурацию и каналы
"""

from __future__ import annotations

import asyncio
import sys


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "run"

    if command in {"-h", "--help", "help"}:
        print(__doc__)
        return 0

    if command == "login":
        from .cli.login import login_flow

        return asyncio.run(login_flow())

    if command == "check":
        from .cli.check import check_flow

        return asyncio.run(check_flow())

    from .app import main as run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
