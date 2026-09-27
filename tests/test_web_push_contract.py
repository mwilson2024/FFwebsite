from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_push_migration_keeps_capability_endpoint_encrypted_and_private():
    sql = (ROOT / "supabase" / "migrations" / "009_web_push_subscriptions.sql").read_text(encoding="utf-8")
    assert "encrypted_subscription bytea" in sql
    assert "endpoint_hash bytea" in sql
    assert "enable row level security" in sql
    assert "revoke all" in sql and "anon, authenticated" in sql
    assert " endpoint text" not in sql


def test_service_worker_handles_background_push_and_safe_internal_navigation():
    script = (ROOT / "src" / "weekly_projections" / "web" / "static" / "service-worker.js").read_text(encoding="utf-8")
    assert "addEventListener('push'" in script
    assert "payload.url.startsWith('/')" in script
    assert "showNotification" in script


def test_command_center_loads_leagues_sequentially():
    script = (ROOT / "src" / "weekly_projections" / "web" / "static" / "command-center.js").read_text(encoding="utf-8")
    template = (ROOT / "src" / "weekly_projections" / "web" / "templates" / "command_center.html").read_text(encoding="utf-8")
    assert "for (const card of cards)" in script
    assert "Promise.all" not in script
    assert script.index("for (const card of cards)") < script.index("/queue")
    assert "data-command-filter" in template
    assert "command-action-count" in template
    assert "Core data loads first; transaction queues follow." in template
