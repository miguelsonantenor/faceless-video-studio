"""Structured JSON logs, rotated daily, with secrets removed (NFR-O1, NFR-S2).

Secrets (the ElevenLabs key, the session token) are registered with ``register_secret``.
A processor replaces them anywhere in a log record, and fields whose names look secret
(``api_key``, ``token``, ``authorization`` …) are masked whatever their value.
"""

from __future__ import annotations

import logging
import logging.handlers
import threading
from pathlib import Path
from typing import Any

import structlog

MASK = "***"
_SECRET_FIELD_HINTS = ("api_key", "apikey", "token", "authorization", "secret", "password",
                       "xi-api-key", "cookie")
_secrets: set[str] = set()
_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 4:
        with _lock:
            _secrets.add(value)


def scrub(text: str) -> str:
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for s in secrets:
        if s in text:
            text = text.replace(s, MASK)
    return text


def _scrub_value(key: str, value: Any) -> Any:
    if any(h in key.lower() for h in _SECRET_FIELD_HINTS):
        return MASK
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: _scrub_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub_value("", v) for v in value]
    return value


def redact_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return {k: _scrub_value(k, v) for k, v in event_dict.items()}


class _ScrubFilter(logging.Filter):
    """Last line of defence for records from other libraries (uvicorn, httpx)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = None
        return True


def configure_logging(logs_dir: Path | None, *, level: int = logging.INFO) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.TimedRotatingFileHandler(
                logs_dir / "fvs.log", when="midnight", backupCount=30, encoding="utf-8"
            )
        )
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in handlers:
        h.setFormatter(logging.Formatter("%(message)s"))
        h.addFilter(_ScrubFilter())
        root.addHandler(h)
    root.setLevel(level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str = "fvs") -> Any:
    return structlog.get_logger(name)
