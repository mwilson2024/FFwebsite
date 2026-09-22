import logging
import io
from fastapi.testclient import TestClient

from weekly_projections.web import app as web
from weekly_projections.web import diagnostics


def test_access_log_is_stdout_only_and_contains_safe_ip(monkeypatch):
    stream = io.StringIO()
    logger = logging.Logger("isolated-access")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    monkeypatch.setattr(diagnostics, "_access_logger", logger)
    request = type("Request", (), {
        "headers": {},
        "client": type("Client", (), {"host": "203.0.113.44"})(),
        "method": "GET",
        "scope": {"route": type("Route", (), {"path": "/league"})()},
    })()
    diagnostics.log_access("reference1", request, 200)
    text = stream.getvalue()
    assert '"event": "access_request"' in text
    assert '"client_ip": "203.0.113.44"' in text
    assert '"route": "/league"' in text and '"status": 200' in text
    assert "query" not in text and "cookie" not in text


def test_error_log_does_not_capture_credentials_or_query_values(monkeypatch, tmp_path):
    logger = logging.Logger("isolated-test")
    monkeypatch.setattr(diagnostics, "_logger", logger)
    path = tmp_path / "errors.log"
    monkeypatch.setattr(diagnostics, "LOG_PATH", path)
    client = TestClient(web.app, client=("203.0.113.10", 50000))
    response = client.get("/missing?password=do-not-log-this")
    assert response.status_code == 404
    assert response.headers["x-error-reference"]
    # Unmatched browser/bot probes are expected internet noise, not app errors.
    assert not path.exists()
    monkeypatch.setitem(web.sessions, "diagnostic-route", web.BrowserSession("unused", 2026, [], "csrf"))
    client.cookies.set("wp_session", "diagnostic-route")
    response = client.get("/api/live-window?league=999&week=1")
    assert response.status_code == 404
    for handler in logger.handlers:
        handler.flush()
        handler.close()
    text = path.read_text()
    assert "http_error" in text
    assert "404" in text
    assert '"client_ip": "203.0.113.10"' in text
    assert "do-not-log-this" not in text
    assert "password" not in text


def test_railway_ip_header_is_trusted_only_inside_railway(monkeypatch):
    request = type("Request", (), {
        "headers": {"x-real-ip": "198.51.100.25"},
        "client": type("Client", (), {"host": "203.0.113.10"})(),
    })()
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    assert diagnostics.client_ip(request) == "203.0.113.10"
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "project")
    assert diagnostics.client_ip(request) == "198.51.100.25"
    request.headers["x-real-ip"] = "spoofed-or-invalid"
    assert diagnostics.client_ip(request) == "203.0.113.10"


def test_client_ip_canonicalizes_ipv6_and_rejects_non_addresses():
    request = type("Request", (), {
        "headers": {},
        "client": type("Client", (), {"host": "2001:0db8:0:0:0:0:0:1"})(),
    })()
    assert diagnostics.client_ip(request) == "2001:db8::1"
    request.client.host = "not-an-address"
    assert diagnostics.client_ip(request) == "unknown"


def test_exception_details_log_full_chain_but_redacts_sensitive_messages(monkeypatch, tmp_path):
    logger = logging.Logger("isolated-exception")
    monkeypatch.setattr(diagnostics, "_logger", logger)
    path = tmp_path / "errors.log"
    monkeypatch.setattr(diagnostics, "LOG_PATH", path)
    try:
        raise ValueError("secret-cookie-value")
    except ValueError as error:
        diagnostics.log_error("lineup_review_failed", error)
    for handler in logger.handlers:
        handler.flush()
        handler.close()
    text = path.read_text()
    assert "ValueError" in text and "test_diagnostics.py" in text
    assert "secret-cookie-value" not in text


def test_exception_details_keep_useful_provider_failure(monkeypatch, tmp_path):
    logger = logging.Logger("isolated-provider-error")
    monkeypatch.setattr(diagnostics, "_logger", logger)
    path = tmp_path / "errors.log"
    monkeypatch.setattr(diagnostics, "LOG_PATH", path)
    try:
        try:
            raise TimeoutError("MFL returned HTTP 429 after retries")
        except TimeoutError as cause:
            raise RuntimeError("Could not load lineup") from cause
    except RuntimeError as error:
        diagnostics.log_error("lineup_load_failed", error)
    for handler in logger.handlers:
        handler.flush()
        handler.close()
    text = path.read_text()
    assert "Could not load lineup" in text
    assert "MFL returned HTTP 429 after retries" in text
    assert '"chain"' in text
    assert "RuntimeError" in text and "TimeoutError" in text


def test_exception_details_include_safe_postgres_sqlstate(monkeypatch, tmp_path):
    logger = logging.Logger("isolated-database-error")
    monkeypatch.setattr(diagnostics, "_logger", logger)
    path = tmp_path / "errors.log"
    monkeypatch.setattr(diagnostics, "LOG_PATH", path)

    error = RuntimeError("Database schema lookup failed")
    error.sqlstate = "3F000"
    diagnostics.log_error("database_status_unavailable", error)

    for handler in logger.handlers:
        handler.flush()
        handler.close()
    text = path.read_text()
    assert '"sqlstate": "3F000"' in text


def test_browser_error_reports_require_session_and_csrf(monkeypatch):
    client = TestClient(web.app)
    payload = {"kind": "script", "page": "/lineup", "line": 42}
    assert client.post("/api/client-error", json=payload).status_code == 401
    monkeypatch.setitem(web.sessions, "diagnostic-test", web.BrowserSession("unused", 2026, [], "test-csrf"))
    client.cookies.set("wp_session", "diagnostic-test")
    assert client.post("/api/client-error", json=payload).status_code == 403
    events = []
    monkeypatch.setattr(web, "log_error", lambda event, *args, **kwargs: events.append(event))
    response = client.post("/api/client-error", json=payload, headers={"X-CSRF-Token": "test-csrf"})
    assert response.status_code == 200
    assert response.json()["reference"] == response.headers["x-error-reference"]
    assert events == ["browser_lineup_script_line_42"]
    assert client.post("/api/client-error", json={**payload, "page": "/lineup?secret=value"}, headers={"X-CSRF-Token": "test-csrf"}).status_code == 422
