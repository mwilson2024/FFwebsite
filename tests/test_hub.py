import copy
import time

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from weekly_projections.mfl.client import MFLApiError, MFLClient, MFLConfig, MFLLeague, MFLPlayer, MFLLineupRule, MFLLineupSettings, MFLFranchise, MFLLeagueDetails
from weekly_projections.trade_engine import suggest_trades, analyze_target_trade
from weekly_projections.web import app as web


@pytest.fixture
def hub(monkeypatch):
    mfl = MFLClient(MFLConfig(2026, '12345', '0001'))
    mfl._players = {'101': MFLPlayer('101', 'Your receiver', 'WR', 'DET'), '102': MFLPlayer('102', 'Your runner', 'RB', 'SEA')}
    block = {'0001': {'assets': ('102', 'FP_0001_2027_1', 'BB_10'), 'wanted': 'Future picks', 'timestamp': '100'},
             '0002': {'assets': ('201',), 'wanted': '<script>bad</script>', 'timestamp': '200'}}
    rosters = {'0001': {'101', '102'}, '0002': {'201'}}
    monkeypatch.setattr(mfl, 'trade_rosters', lambda: rosters)
    monkeypatch.setattr(mfl, 'trade_block', lambda: copy.deepcopy(block))
    monkeypatch.setattr(mfl, 'franchise_names', lambda: {'0001': 'Your team', '0002': 'Other team'})
    monkeypatch.setattr(mfl, 'league_details', lambda: MFLLeagueDetails(
        (("00", "Blue Division"), ("01", "Silver Division")),
        {"0001": MFLFranchise("0001", "Your team", "00", "https://www42.myfantasyleague.com/one.png"),
         "0002": MFLFranchise("0002", "Other team", "01", "")},
    ))
    writes = []
    def write(kind, **params):
        writes.append((kind, params))
        block['0001'] = {'assets': tuple(params['WILL_GIVE_UP'].split(',')), 'wanted': params['IN_EXCHANGE_FOR'], 'timestamp': '300'}
        return {'status': {'$t': 'OK'}}
    monkeypatch.setattr(mfl, 'import_request', write)
    monkeypatch.setattr(web, '_client', lambda *args: mfl)
    session = web.BrowserSession('fake', 2026, [MFLLeague('12345', '0001', 'First league'), MFLLeague('22222', '0002', 'Second league')], 'csrf')
    monkeypatch.setattr(web, 'sessions', {'test': session})
    client = TestClient(web.app)
    client.cookies.set('wp_session', 'test')
    return client, mfl, session, block, rosters, writes


def block_preview(client, **changes):
    form = {'league': '12345', 'player_ids': ['101'], 'wanted': 'Future picks', 'csrf_token': 'csrf'}
    form.update(changes)
    return client.post('/trade-block/preview', data=form, follow_redirects=False)


def test_home_remembers_only_authorized_league_and_not_widget_navigation(hub, monkeypatch):
    client, mfl, session, block, rosters, writes = hub
    home = client.get('/home?league=22222')
    assert home.status_code == 200 and 'Second league' in home.text
    assert 'wp_last_league=2026:22222' in home.headers['set-cookie']
    soup = BeautifulSoup(home.text, 'html.parser')
    assert len(soup.select('select[name="league"]')) == 1
    assert len(soup.select('[data-hub-url]')) == 3
    assert '/home?league=22222' == client.get('/dashboard', follow_redirects=False).headers['location']
    assert client.get('/home?league=99999').status_code == 404
    assert client.get('/hub/block?league=12345').status_code == 200
    assert '/home?league=22222' == client.get('/dashboard', follow_redirects=False).headers['location']
    assert not writes
    assert 'wp_last_league' not in client.post('/logout', data={'csrf_token': 'csrf'}, follow_redirects=False).headers['set-cookie']
    assert client.get('/hub/block?league=12345').status_code == 401


@pytest.mark.parametrize('preference,expected', [('2026:22222','22222'), ('2025:22222','12345'), ('2026:99999','12345')])
def test_login_returns_to_valid_last_league(monkeypatch, preference, expected):
    monkeypatch.setenv('WP_SECURE_COOKIES', '1')
    class LoginClient:
        def __init__(self, config): pass
        def login(self): pass
        def user_cookie(self): return 'fake'
        def account_leagues(self): return [MFLLeague('12345','0001','One'), MFLLeague('22222','0002','Two')]
    monkeypatch.setattr(web, 'MFLClient', LoginClient)
    monkeypatch.setattr(web, 'sessions', {})
    client = TestClient(web.app)
    client.cookies.set('wp_last_league', preference)
    result = client.post('/login', data={'username':'fake', 'password':'fake', 'year':2026}, follow_redirects=False)
    assert result.headers['location'] == '/home?league=' + expected
    assert '; Secure' in result.headers['set-cookie']


def test_api_key_login_is_memory_only_and_never_uses_cookie_auth(monkeypatch):
    captured = []
    class KeyClient:
        def __init__(self, config): captured.append(config)
        def login(self): pass
        def user_cookie(self): raise AssertionError('API-key login must not request a user cookie')
        def account_leagues(self): return [MFLLeague('12345','0001','One')]
    monkeypatch.setattr(web, 'MFLClient', KeyClient)
    monkeypatch.setattr(web, 'sessions', {})
    client = TestClient(web.app)
    result = client.post('/login', data={'api_key':'session-secret','year':2026}, follow_redirects=False)
    assert result.status_code == 303
    assert captured[0].api_key == 'session-secret'
    session = next(iter(web.sessions.values()))
    assert session.mfl_api_key == 'session-secret' and session.mfl_cookie == ''
    assert 'session-secret' not in str(result.headers) and 'session-secret' not in result.text


def test_settings_validates_api_key_before_storing_it(hub, monkeypatch):
    client,mfl,session,block,rosters,writes = hub
    seen = []
    class Probe:
        def __init__(self, config): seen.append(config)
        def league_standings(self): return []
    monkeypatch.setattr(web, 'MFLClient', Probe)
    result = client.post('/session/api-key', data={'league':'12345','api_key':'fresh-secret','csrf_token':'csrf'}, follow_redirects=False)
    assert result.status_code == 303 and result.headers['location'].endswith('connection=api-key-added')
    assert seen[0].api_key == 'fresh-secret' and session.mfl_api_key == 'fresh-secret'
    assert 'fresh-secret' not in str(result.headers)
    session.mfl_api_key = ''
    result = client.post('/session/api-key', data={'league':'12345','api_key':'bad key','csrf_token':'csrf'}, follow_redirects=False)
    assert result.headers['location'].endswith('connection=invalid-key') and session.mfl_api_key == ''
    assert client.post('/session/api-key', data={'league':'12345','api_key':'x','csrf_token':'wrong'}).status_code == 403


def test_trade_block_preview_preserves_assets_and_confirms_once(hub):
    client, mfl, session, block, rosters, writes = hub
    response = block_preview(client)
    assert response.status_code == 303 and not writes
    url = response.headers['location']
    review = client.get(url)
    assert 'FP_0001_2027_1' in review.text and 'BB_10' in review.text and 'Your receiver' in review.text
    send_url = url.replace('/review/', '/send/')
    result = client.post(send_url, data={'csrf_token':'csrf'})
    assert 'Trade block updated' in result.text
    assert writes == [('tradeBait', {'WILL_GIVE_UP':'101,102,BB_10,FP_0001_2027_1', 'IN_EXCHANGE_FOR':'Future picks'})]
    client.post(send_url, data={'csrf_token':'csrf'})
    assert len(writes) == 1


@pytest.mark.parametrize('failure', ['stale', 'ownership', 'expired', 'csrf', 'other_session'])
def test_block_revalidation_and_authorization(hub, failure):
    client, mfl, session, block, rosters, writes = hub
    result = block_preview(client)
    url = result.headers['location'].replace('/review/', '/send/')
    draft = session.blocks[url.rsplit('/',1)[1]]
    if failure == 'stale': block['0001']['assets'] += ('DP_02_05',)
    if failure == 'ownership': rosters['0001'].remove('101')
    if failure == 'expired': draft.created_at = time.monotonic() - 1201
    if failure == 'other_session':
        web.sessions['other'] = web.BrowserSession('fake2', 2026, session.leagues, 'csrf')
        client.cookies.set('wp_session', 'other')
    result = client.post(url, data={'csrf_token':'bad' if failure == 'csrf' else 'csrf'})
    assert not writes
    assert result.status_code == (403 if failure == 'csrf' else 404 if failure == 'other_session' else 200)


def test_block_uncertain_write_is_never_repeated(hub, monkeypatch):
    client, mfl, session, block, rosters, writes = hub
    def uncertain(*args, **kwargs):
        writes.append('write-attempt')
        raise MFLApiError('timeout')
    monkeypatch.setattr(mfl, 'import_request', uncertain)
    url = block_preview(client).headers['location'].replace('/review/', '/send/')
    result = client.post(url, data={'csrf_token':'csrf'})
    assert 'MFL did not confirm' in result.text
    client.post(url, data={'csrf_token':'csrf'})
    assert writes == ['write-attempt']


def test_feed_parsers_preserve_trade_assets_and_missing_standings(monkeypatch):
    client = MFLClient(MFLConfig(2026, '1', '0001'))
    payloads = {'tradeBait': {'tradeBaits': {'tradeBait': {'franchise_id':'1', 'willGiveUp':{'$t':'101,DP_02_05,FP_0001_2027_1,BB_10'}, 'inExchangeFor':'WR'}}},
                'leagueStandings': {'leagueStandings': {'franchise': {'id':'1','h2hw':'0','h2hl':{'$t':'1'},'pf':'27.2'}}}}
    calls = []
    def export(kind, **params):
        calls.append((kind, params))
        return payloads[kind]
    monkeypatch.setattr(client, 'export', export)
    assert set(client.trade_block()['0001']['assets']) == {'101','DP_02_05','FP_0001_2027_1','BB_10'}
    assert calls[0] == ('tradeBait', {'INCLUDE_DRAFT_PICKS':1})
    assert client.league_standings() == [{'id':'0001','h2hw':'0','h2hl':'1','h2ht':'—','pf':'27.2','pa':'—','vp':'—'}]
    payloads['tradeBait'] = {'unexpected': {}}
    with pytest.raises(MFLApiError): client.trade_block()


def test_hub_sections_escape_feed_text_and_fail_independently(hub, monkeypatch):
    client, mfl, session, block, rosters, writes = hub
    result = client.get('/hub/block?league=12345')
    assert result.status_code == 200 and '&lt;script&gt;bad&lt;/script&gt;' in result.text
    monkeypatch.setattr(mfl, 'league_standings', lambda: [{'id':'0001','h2hw':'1','h2hl':'0','h2ht':'0','pf':'88.5','pa':'40','vp':'—'}])
    standings = client.get('/hub/standings?league=12345').text
    assert '88.5' in standings and 'Blue Division' in standings
    assert 'https://www42.myfantasyleague.com/one.png' in standings
    def fail(): raise MFLApiError('private upstream error')
    monkeypatch.setattr(mfl, 'league_standings', fail)
    result = client.get('/hub/standings?league=12345')
    assert 'data-hub-retry' in result.text and 'private upstream error' not in result.text
    assert client.get('/home?league=12345').status_code == 200
    assert not writes


def test_trade_engine_finds_mutual_legal_starter_upgrades_and_skips_missing():
    settings = MFLLineupSettings(2, (MFLLineupRule('RB',1,1), MFLLineupRule('WR',1,1)))
    catalog = {str(i):MFLPlayer(str(i), f'Player {i}', pos) for i,pos in enumerate(['RB','RB','WR','WR','WR','RB'],1)}
    scores = dict(zip(catalog, [20,15,5,20,15,5]))
    rosters = {'0001':{'1','2','3'}, '0002':{'4','5','6'}}
    ideas = suggest_trades('0001',rosters,catalog,scores,settings)
    assert len(ideas) == 1
    assert (ideas[0].give.id, ideas[0].receive.id, ideas[0].own_gain, ideas[0].other_gain) == ('2','5',10,10)
    scores.pop('2')
    assert suggest_trades('0001',rosters,catalog,scores,settings) == []


@pytest.fixture
def target_data():
    settings = MFLLineupSettings(2, (MFLLineupRule('RB',1,1), MFLLineupRule('WR',1,1)))
    catalog = {str(i):MFLPlayer(str(i), f'Player {i}', pos) for i,pos in enumerate(['RB','RB','WR','WR','WR','RB'],1)}
    return {'0001':{'1','2','3'}, '0002':{'4','5','6'}}, catalog, dict(zip(catalog,[20,15,5,20,15,5])), settings


def test_target_engine_ranks_mutual_gain_and_two_player_packages(target_data):
    rosters, catalog, scores, settings = target_data
    original = copy.deepcopy(rosters)
    result = analyze_target_trade('0001','0002','5',rosters,catalog,scores,settings,limit=20)
    best = result['offers'][0]
    assert (best.give_ids,best.receive.id,best.own_gain,best.other_gain) == ('2','5',10,10)
    assert (best.own_before,best.own_after,best.other_before,best.other_after) == (25,35,25,35)
    assert any(len(offer.give) == 2 for offer in result['offers'])
    assert rosters == original
    singles = analyze_target_trade('0001','0002','5',rosters,catalog,scores,settings,package_size=1)
    assert all(len(offer.give) == 1 for offer in singles['offers'])


@pytest.mark.parametrize('own,target,player,size', [('0001','0001','1',2),('0001','0002','1',2),('0001','0002','999',2),('0001','0002','5',3)])
def test_target_engine_rejects_invalid_targets(target_data, own, target, player, size):
    rosters,catalog,scores,settings = target_data
    with pytest.raises(ValueError):
        analyze_target_trade(own,target,player,rosters,catalog,scores,settings,package_size=size)


def test_target_engine_missing_projections_and_time_limit(target_data):
    rosters,catalog,scores,settings = target_data
    scores['5'] = float('nan')
    result = analyze_target_trade('0001','0002','5',rosters,catalog,scores,settings)
    assert not result['offers'] and 'no weekly projection' in result['warning']
    scores['5'] = 15
    scores['1'] = 'unavailable'
    result = analyze_target_trade('0001','0002','5',rosters,catalog,scores,settings)
    assert result['missing'] == 1 and 'excluded' in result['warning']
    assert all('1' not in offer.give_ids.split(',') for offer in result['offers'])
    result = analyze_target_trade('0001','0002','5',rosters,catalog,scores,settings,seconds=0)
    assert result['truncated'] and not result['offers']


def test_target_engine_routes_select_only_and_never_send(hub, target_data, monkeypatch):
    client,mfl,session,block,rosters,writes = hub
    target_rosters,catalog,scores,settings = target_data
    mfl._players = catalog
    monkeypatch.setattr(mfl,'franchise_roster',lambda fid: target_rosters[fid])
    monkeypatch.setattr(mfl,'current_week',lambda: 1)
    monkeypatch.setattr(mfl,'projected_scores',lambda **kw: scores)
    monkeypatch.setattr(mfl,'lineup_settings',lambda: settings)
    response = client.get('/hub/ideas?league=12345&target=0002&wanted=5')
    assert response.status_code == 200 and 'Offers for Player 5' in response.text
    assert '+10.0' in response.text and 'nothing is sent to MFL' in response.text
    page = client.get('/trades?league=12345&target=0002&give=1,2&receive=5&wanted=5')
    soup = BeautifulSoup(page.text,'html.parser')
    assert {p['value'] for p in soup.select('input[name="give"]:checked')} == {'1','2'}
    assert {p['value'] for p in soup.select('input[name="receive"]:checked')} == {'5'}
    assert soup.select_one('select[name="wanted"] option[selected]')['value'] == '5'
    assert not writes and not session.trades


def test_expired_session_cannot_access_mfl(hub):
    client,mfl,session,block,rosters,writes = hub
    session.expires_at = time.monotonic()-1
    assert client.get('/hub/block?league=12345').status_code == 401
    assert 'test' not in web.sessions and not writes


def test_hosting_setting_marks_preference_cookie_secure(hub, monkeypatch):
    client,mfl,session,block,rosters,writes = hub
    monkeypatch.setenv('WP_SECURE_COOKIES','1')
    response = client.get('/home?league=12345')
    assert '; Secure' in response.headers['set-cookie']
