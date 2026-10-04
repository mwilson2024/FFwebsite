from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_push_migration_keeps_capability_endpoint_encrypted_and_private():
    sql = (ROOT / "supabase" / "migrations" / "009_web_push_subscriptions.sql").read_text(encoding="utf-8")
    assert "encrypted_subscription bytea" in sql
    assert "endpoint_hash bytea" in sql
    assert "enable row level security" in sql
    assert "revoke all" in sql and "anon, authenticated" in sql
    assert " endpoint text" not in sql


def test_emergency_lineup_migration_requires_opt_in_and_guards_duplicate_writes():
    sql = (ROOT / "supabase" / "migrations" / "010_emergency_lineup_guard.sql").read_text(encoding="utf-8")
    assert "emergency_lineup_preference" in sql
    assert "enabled boolean not null default false" in sql
    assert "primary key (user_id, season, league_id)" in sql
    assert "primary key (user_id, action_key)" in sql
    assert "enable row level security" in sql
    assert sql.count("revoke all") == 2
    assert "values (10, 'Emergency lineup opt-in and idempotency guard')" in sql


def test_emergency_choice_migration_is_private_and_preserves_order():
    sql = (ROOT / "supabase" / "migrations" / "011_emergency_lineup_choices.sql").read_text(encoding="utf-8")
    assert "prompt_answered boolean not null default false" in sql
    assert "replacement_mode text not null default 'projection'" in sql
    assert "primary key (user_id, season, league_id, player_id)" in sql
    assert "unique (user_id, season, league_id, priority)" in sql
    assert "enable row level security" in sql
    assert "revoke all" in sql
    assert "values (11, 'Emergency lineup onboarding and player priority')" in sql


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


def test_multi_scores_loads_leagues_sequentially_and_pauses_hidden_tabs():
    script = (ROOT / "src" / "weekly_projections" / "web" / "static" / "multi-scores.js").read_text(
        encoding="utf-8"
    )
    template = (ROOT / "src" / "weekly_projections" / "web" / "templates" / "multi_scores.html").read_text(
        encoding="utf-8"
    )
    assert "for (const league of leagues)" in script
    assert "Promise.all" not in script
    assert "document.hidden" in script and "visibilitychange" in script
    assert "window.setTimeout(loadAll, 60000)" in script
    assert "data-multi-scope=\"mine\"" in template
    assert "data-multi-scope=\"following\"" in template
    assert "data-multi-scope=\"all\"" in template
    assert "localStorage.setItem(storageKey" in script
    assert "data.followKey" not in script
    assert "card.dataset.followKey" in script
    assert "renderWinProbability" in script
