"""Detailed diagnostics for local files and hosted log streams.

Messages and tracebacks are useful for diagnosing provider failures, but secrets,
cookies, authorization values, and request bodies must never be recorded.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import traceback
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock

from weekly_projections.config import PROJECT_ROOT

LOG_PATH = PROJECT_ROOT / "logs" / "errors.log"
request_context: ContextVar[tuple[str, object] | None] = ContextVar("diagnostics", default=None)
_lock = Lock()
_logger = logging.getLogger("weekly_projections.errors")
_logger.setLevel(logging.INFO)
_logger.propagate = False

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(?:api[_-]?key|password|passwd|username|mfl_user_id|authorization|cookie|token|secret)"
    r"\s*[:=]\s*[^\s,;&\"']+"
)
_SENSITIVE_WORD = re.compile(
    r"(?i)api[_ -]?key|password|passwd|username|mfl_user_id|authorization|cookie|bearer|secret"
)


def _safe_text(value: object) -> str:
    """Keep operational detail while refusing to serialize likely credentials."""
    text = _SENSITIVE_ASSIGNMENT.sub("[REDACTED]", str(value))
    if _SENSITIVE_WORD.search(text):
        return "[REDACTED: message contained sensitive material]"
    return text[:8_000]


def _exception_details(error: BaseException) -> list[dict]:
    """Return the complete Python exception chain without locals or request data."""
    details: list[dict] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        item: dict = {
            "exception": type(current).__name__,
            "message": _safe_text(current),
            "traceback": [
                {
                    "file": frame.filename,
                    "line": frame.lineno,
                    "function": frame.name,
                    **({"source": _safe_text(frame.line)} if frame.line else {}),
                }
                for frame in traceback.extract_tb(current.__traceback__)
            ],
        }
        request = getattr(current, "request", None)
        response = getattr(current, "response", None)
        if request is not None:
            item["http_request"] = {
                "method": _safe_text(getattr(request, "method", "")),
                "url": _safe_text(getattr(request, "url", "")),
            }
        if response is not None:
            headers = getattr(response, "headers", {}) or {}
            item["http_response"] = {
                "status": getattr(response, "status_code", None),
                "retry_after": _safe_text(headers.get("Retry-After", "")),
                "rate_limit_remaining": _safe_text(headers.get("X-RateLimit-Remaining", "")),
                "rate_limit_reset": _safe_text(headers.get("X-RateLimit-Reset", "")),
            }
        details.append(item)
        current = current.__cause__ or (
            current.__context__ if not current.__suppress_context__ else None
        )
    return details


def initialize_log() -> None:
    """Create the local log at startup without inserting a fabricated error."""
    try:
        with _lock:
            if not _logger.handlers:
                LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
                file_handler = RotatingFileHandler(
                    LOG_PATH, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
                )
                file_handler.setFormatter(formatter)
                _logger.addHandler(file_handler)
                # Railway captures stdout, so hosted failures appear in Deploy Logs.
                stream_handler = logging.StreamHandler(sys.stdout)
                stream_handler.setFormatter(formatter)
                _logger.addHandler(stream_handler)
    except OSError:
        pass


def log_error(event: str, error: BaseException | None = None, *, status: int | None = None) -> None:
    context = request_context.get()
    record: dict = {"event": event}
    if context:
        request_id, request = context
        route = request.scope.get("route")
        record.update(
            reference=request_id,
            method=request.method,
            route=getattr(route, "path", "unmatched"),
        )
    if status is not None:
        record["status"] = status
    if error is not None:
        record["exception"] = type(error).__name__
        record["message"] = _safe_text(error)
        record["chain"] = _exception_details(error)
    try:
        initialize_log()
        with _lock:
            if _logger.handlers:
                _logger.error(json.dumps(record))
    except OSError:
        # A logging failure must not break a lineup request.
        pass
