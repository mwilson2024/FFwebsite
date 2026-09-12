"""Local, size-limited diagnostics. Never record request content or error messages."""
from __future__ import annotations

import json
import logging
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


def initialize_log() -> None:
    """Create the local log at startup without inserting a fabricated error."""
    try:
        with _lock:
            if not _logger.handlers:
                LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
                _logger.addHandler(handler)
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
        # No exception text, source lines, locals, query strings, headers or bodies.
        record["frames"] = [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(error.__traceback__)[-12:]
        ]
    try:
        initialize_log()
        with _lock:
            if _logger.handlers:
                _logger.error(json.dumps(record))
    except OSError:
        # A logging failure must not break a lineup request.
        pass
