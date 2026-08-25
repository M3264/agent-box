"""Structured JSON logging.

One line of JSON per record so journalctl output is machine-readable. Falls back
to plain text when AGENT_HUB_LOG_FORMAT=text, which is friendlier for local runs.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from app.config import settings

# Attributes present on every LogRecord; anything else was passed via `extra`
# and belongs in the JSON payload.
_RESERVED = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName relativeCreated
    stack_info stacklevel thread threadName taskName""".split()
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = record.stack_info
        return json.dumps(payload, default=str)


def configure() -> None:
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s  %(message)s"))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    # uvicorn installs its own colourised handlers; route them through ours so
    # the whole process emits one consistent format.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # The access logger needs care. `--no-access-log` (which start.sh passes) is
    # implemented by clearing this logger's handlers and setting propagate=False,
    # and configure() runs *after* that — so adopting it unconditionally silently
    # resurrected a line per request, which is the double logging start.sh claims
    # to avoid. Take it over only when uvicorn left it enabled.
    access = logging.getLogger("uvicorn.access")
    if access.handlers or access.propagate:
        access.handlers = []
        access.propagate = True


def get_logger(name: str) -> logging.LoggerAdapter | logging.Logger:
    return logging.getLogger(name)
