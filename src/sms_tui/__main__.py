"""CLI entrypoint for gammu-sms-tui."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

from sms_tui.backend import GammuBackend
from sms_tui.store import Store
from sms_tui.tui import App


def resolve_db_path() -> Path:
    env_path = os.environ.get("SMS_TUI_DB", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".local" / "share" / "gammu-sms-tui" / "messages.db"


def resolve_log_path() -> Path:
    env_path = os.environ.get("SMS_TUI_LOG", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".local" / "share" / "gammu-sms-tui" / "sms-tui.log"


def _configure_logging() -> Path:
    log_path = resolve_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=512_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger = logging.getLogger("sms_tui")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(handler)
    return log_path


def main() -> int:
    _configure_logging()
    db_path = resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)
    cfg_env = os.environ.get("GAMMU_CONFIG", "").strip()
    backend = GammuBackend(config=Path(cfg_env).expanduser() if cfg_env else None)
    try:
        App(store=store, backend=backend, staging_dir=db_path.parent).run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"gammu-sms-tui error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
