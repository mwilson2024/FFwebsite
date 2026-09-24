from pathlib import Path
from bs4 import BeautifulSoup
import pytest


def test_all_pages_load_their_requested_theme_last():
    templates = Path(__file__).resolve().parents[1] / 'src/weekly_projections/web/templates'
    pages = [p for p in templates.glob('*.html') if '<!doctype html>' in p.read_text(encoding='utf-8').lower()]
    assert len(pages) == 22
    for page in pages:
        source = page.read_text(encoding='utf-8')
        assert "{% include '_theme_assets.html' %}" in source, page.name
        source = source.replace("{% include '_theme_assets.html' %}", (templates / '_theme_assets.html').read_text(encoding='utf-8'))
        soup = BeautifulSoup(source, 'html.parser')
        assert soup.select('link[rel="stylesheet"]')[-1]['href'] == '/static/themes.css?v=6', page.name
        assert len(soup.select('meta[name="theme-color"]')) == 1
        assert soup.select_one('script[src="/static/theme.js?v=20260924-league-themes"]')
        assert soup.select_one('meta[name="wp-theme-scope"]')


def test_theme_primary_text_pairs_have_accessible_contrast():
    def luminance(color):
        channels = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
        channels = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(channels, (.2126, .7152, .0722)))
    for foreground, background in [('#ffcb05','#00274c'),('#ffffff','#0076b6'),('#005c90','#e5f2fa'),('#152b3b','#ffffff'),('#4b6373','#edf3f7'),('#005c90','#ffffff'),('#f7f9ff','#07111f'),('#59e1ff','#0d1b2f'),('#ffffff','#7157e8'),('#f8f7f2','#0c2340'),('#ff9b73','#0c2340'),('#061525','#fa4616'),('#ffffff','#ce1126'),('#a50e1e','#ffffff'),('#f7f9fc','#0b2242'),('#82b5ff','#0b2242'),('#ffffff','#c8102e')]:
        values = sorted([luminance(foreground), luminance(background)])
        assert (values[1] + .05) / (values[0] + .05) >= 4.5


def test_lions_uses_white_surfaces_and_light_controls():
    css = (Path(__file__).resolve().parents[1] / 'src/weekly_projections/web/static/themes.css').read_text()
    palette = css.split('html[data-theme="lions"] {',1)[1].split('}',1)[0]
    assert '--site-bg:#fff;' in palette and '--site-panel:#fff;' in palette
    assert 'color-scheme:light;' in css


def test_midnight_aurora_theme_is_complete_and_selectable():
    root = Path(__file__).resolve().parents[1] / 'src/weekly_projections/web/static'
    css = (root / 'themes.css').read_text(encoding='utf-8')
    script = (root / 'theme.js').read_text(encoding='utf-8')
    palette = css.split('html[data-theme="aurora"] {', 1)[1].split('}', 1)[0]
    for token in ('--site-bg:', '--site-panel:', '--site-raised:', '--site-text:', '--site-muted:',
                  '--site-accent:', '--site-on-accent:', '--site-link:', '--site-header:', '--site-input:'):
        assert token in palette
    assert 'radial-gradient' in css and 'Midnight Aurora' in script
    assert "['michigan','lions','aurora','tigers','redwings','pistons']" in script


def test_detroit_tigers_theme_is_complete_and_selectable():
    root = Path(__file__).resolve().parents[1] / 'src/weekly_projections/web/static'
    css = (root / 'themes.css').read_text(encoding='utf-8')
    script = (root / 'theme.js').read_text(encoding='utf-8')
    palette = css.split('html[data-theme="tigers"] {', 1)[1].split('}', 1)[0]
    for token in ('--site-bg:', '--site-panel:', '--site-raised:', '--site-text:', '--site-muted:',
                  '--site-accent:', '--site-on-accent:', '--site-link:', '--site-header:', '--site-input:'):
        assert token in palette
    assert '--site-accent:#fa4616;' in palette
    assert '--site-header:#0c2340;' in palette
    assert 'Detroit Tigers' in script


@pytest.mark.parametrize(
    ("theme", "accent", "header", "title"),
    (
        ("redwings", "#ce1126", "#ce1126", "Detroit Red Wings"),
        ("pistons", "#c8102e", "#1d428a", "Detroit Pistons"),
    ),
)
def test_additional_detroit_themes_are_complete_and_selectable(theme, accent, header, title):
    root = Path(__file__).resolve().parents[1] / 'src/weekly_projections/web/static'
    css = (root / 'themes.css').read_text(encoding='utf-8')
    script = (root / 'theme.js').read_text(encoding='utf-8')
    palette = css.split(f'html[data-theme="{theme}"] {{', 1)[1].split('}', 1)[0]
    for token in ('--site-bg:', '--site-panel:', '--site-raised:', '--site-text:', '--site-muted:',
                  '--site-accent:', '--site-on-accent:', '--site-link:', '--site-header:', '--site-input:'):
        assert token in palette
    assert f'--site-accent:{accent};' in palette
    assert f'--site-header:{header};' in palette
    assert title in script


def test_roster_tool_tabs_keep_six_columns_and_readable_active_text():
    root = Path(__file__).resolve().parents[1] / 'src/weekly_projections'
    css = (root / 'web/static/rosters.css').read_text(encoding='utf-8')
    tabs = (root / 'web/templates/_roster_tabs.html').read_text(encoding='utf-8')
    assert 'grid-template-columns:repeat(6,minmax(0,1fr));' in css
    assert '.roster-tools a.active :is(strong,small)' in css
    assert 'color:var(--site-on-accent) !important;' in css
    assert tabs.count('<a ') == 6
    assert tabs.rfind('League leaders') > tabs.rfind('Compare')


def test_league_switcher_uses_one_control_height_on_every_page():
    root = Path(__file__).resolve().parents[1] / 'src/weekly_projections'
    css = (root / 'web/static/interface.css').read_text(encoding='utf-8')
    assets = (root / 'web/templates/_theme_assets.html').read_text(encoding='utf-8')
    header = (root / 'web/templates/_league_header.html').read_text(encoding='utf-8')
    assert '.sr-only {' in css
    assert '.header-league-picker .league-switch-button' in css
    assert 'height:44px; min-height:44px;' in css
    assert '.league-topbar .brand-mark { display:grid; width:44px; height:44px; }' in css
    assert '/static/interface.css?v=20260924-league-themes' in assets
    assert '<span class="sr-only">Switch league</span>' in header


def test_mobile_navigation_fits_the_five_primary_tabs_on_one_row():
    css = (Path(__file__).resolve().parents[1] / 'src/weekly_projections/web/static/themes.css').read_text(
        encoding='utf-8'
    )
    mobile = css.split('@media(max-width:480px)', 1)[1]
    assert 'grid-template-columns:repeat(5,minmax(0,1fr));' in mobile


def test_activity_wall_only_scrolls_after_reaching_waiver_height():
    css = (Path(__file__).resolve().parents[1] / 'src/weekly_projections/web/static/league.css').read_text(
        encoding='utf-8'
    )
    synced_rule = css.split('[data-scroll-height-target].is-height-synced {', 1)[1].split('}', 1)[0]
    properties = {declaration.split(':', 1)[0].strip() for declaration in synced_rule.split(';') if ':' in declaration}
    assert 'max-height:var(--activity-panel-height);' in synced_rule
    assert 'height' not in properties
    assert '.activity-feed { flex:1 1 auto; min-height:0; overflow-y:auto;' in css
