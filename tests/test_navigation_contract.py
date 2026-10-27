from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "src/weekly_projections/web"


def test_navigation_groups_decision_tools_by_user_job():
    home = (ROOT / "templates/_home_tabs.html").read_text(encoding="utf-8")
    lineup = (ROOT / "templates/_lineup_tabs.html").read_text(encoding="utf-8")
    players = (ROOT / "templates/_roster_tabs.html").read_text(encoding="utf-8")
    header = (ROOT / "templates/_league_header.html").read_text(encoding="utf-8")

    assert "Overview" in home and "Inbox &amp; alerts" in home
    assert "Multi-week planner" in lineup and "Decision lab" in lineup
    assert "Market &amp; research" in players
    assert "Watch &amp; compare" not in players
    assert 'action="/moves" role="search"' in header
    assert 'placeholder="Search any player' in header


def test_assistant_exposes_optional_page_context_preview():
    widget = (ROOT / "templates/_assistant_widget.html").read_text(encoding="utf-8")
    script = (ROOT / "static/assistant-widget.js").read_text(encoding="utf-8")
    assert "Preview what page context will be sent" in widget
    assert "data-assistant-include-context" in widget
    assert "contextChoice?.checked === false" in script
    assert "No page context will be sent." in script


def test_home_fragments_fail_fast_and_show_display_freshness():
    script = (ROOT / "static/hub.js").read_text(encoding="utf-8")
    assert "AbortSignal.timeout(20000)" in script
    assert "hub-freshness" in script
    assert "Displayed" in script
