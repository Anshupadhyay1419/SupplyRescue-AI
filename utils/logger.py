"""
Logging setup.

Every module gets its logger with `get_logger(__name__)`. Logs go to the
console and to `logs/supplyrescue.log` so problems can be investigated after
a demo.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from config import settings

_configured = False


def _configure_root_logger() -> None:
    """Attach console + rotating file handlers once per process."""
    global _configured
    if _configured:
        return

    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

    root = logging.getLogger("supplyrescue")
    root.setLevel(settings.LOG_LEVEL)

    # Windows consoles often use cp1252, which cannot print "₹". Replace instead of crashing.
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        settings.LOG_DIR / "supplyrescue.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger that lives under the 'supplyrescue' namespace."""
    _configure_root_logger()
    return logging.getLogger(f"supplyrescue.{name}")
