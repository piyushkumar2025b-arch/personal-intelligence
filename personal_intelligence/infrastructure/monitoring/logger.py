"""Structured logging wrapper with fallback to stdlib logging."""

from __future__ import annotations
import logging
from typing import Any

try:
    import structlog
    HAS_STRUCTLOG = True
except ImportError:
    HAS_STRUCTLOG = False


class _FallbackLogger:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _fmt(self, event: str, kwargs: dict[str, Any]) -> str:
        if not kwargs:
            return event
        extras = " ".join(f"{k}={v}" for k, v in kwargs.items())
        return f"{event} | {extras}"

    def info(self, event: str, **kwargs: Any) -> None:
        self._logger.info(self._fmt(event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self._logger.warning(self._fmt(event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self._logger.error(self._fmt(event, kwargs))

    def debug(self, event: str, **kwargs: Any) -> None:
        self._logger.debug(self._fmt(event, kwargs))


def get_logger(name: str) -> Any:
    if HAS_STRUCTLOG:
        return structlog.get_logger(name)
    std = logging.getLogger(name)
    if not std.handlers:
        std.addHandler(logging.NullHandler())
    return _FallbackLogger(std)
