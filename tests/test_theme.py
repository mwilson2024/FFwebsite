from pathlib import Path
from bs4 import BeautifulSoup


def test_all_pages_load_their_requested_theme_last():
    templates = Path(__file__).resolve().parents[1] / 'src/weekly_projections/web/templates'
    pages = [p for p in templates.glob('*.html') if '<!doctype html>' in p.read_text(encoding='utf-8').lower()]
    assert len(pages) == 20
    for page in pages:
        source = page.read_text(encoding='utf-8')
        assert "{% include '_theme_assets.html' %}" in source, page.name
        source = source.replace("{% include '_theme_assets.html' %}", (templates / '_theme_assets.html').read_text(encoding='utf-8'))
        soup = BeautifulSoup(source, 'html.parser')
        assert soup.select('link[rel="stylesheet"]')[-1]['href'] == '/static/themes.css?v=2', page.name
        assert len(soup.select('meta[name="theme-color"]')) == 1
        assert soup.select_one('script[src="/static/theme.js?v=1"]')


def test_theme_primary_text_pairs_have_accessible_contrast():
    def luminance(color):
        channels = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
        channels = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(channels, (.2126, .7152, .0722)))
    for foreground, background in [('#ffcb05','#00274c'),('#ffffff','#0076b6'),('#005c90','#e5f2fa'),('#152b3b','#ffffff'),('#4b6373','#edf3f7'),('#005c90','#ffffff')]:
        values = sorted([luminance(foreground), luminance(background)])
        assert (values[1] + .05) / (values[0] + .05) >= 4.5


def test_lions_uses_white_surfaces_and_light_controls():
    css = (Path(__file__).resolve().parents[1] / 'src/weekly_projections/web/static/themes.css').read_text()
    palette = css.split('html[data-theme="lions"] {',1)[1].split('}',1)[0]
    assert '--site-bg:#fff;' in palette and '--site-panel:#fff;' in palette
    assert 'color-scheme:light;' in css


def test_league_switcher_uses_one_control_height_on_every_page():
    root = Path(__file__).resolve().parents[1] / 'src/weekly_projections'
    css = (root / 'web/static/interface.css').read_text(encoding='utf-8')
    assets = (root / 'web/templates/_theme_assets.html').read_text(encoding='utf-8')
    header = (root / 'web/templates/_league_header.html').read_text(encoding='utf-8')
    assert '.sr-only {' in css
    assert '.header-league-picker .league-switch-button' in css
    assert 'height:44px; min-height:44px;' in css
    assert '.league-topbar .brand-mark { display:grid; width:44px; height:44px; }' in css
    assert '/static/interface.css?v=20260921-rankings' in assets
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
