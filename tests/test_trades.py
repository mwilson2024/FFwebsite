import time

import pytest
from fastapi.testclient import TestClient

from weekly_projections.mfl.client import MFLApiError, MFLClient, MFLConfig, MFLLeague, MFLPlayer, MFLTradeAsset
from weekly_projections.web import app as web


@pytest.fixture
def trade_app(monkeypatch):
    mfl = MFLClient(MFLConfig(2026, '12345', '0001'))
    mfl._players = {'101': MFLPlayer('101', 'Your Receiver', 'WR', 'DET'),
                    '102': MFLPlayer('102', 'Your Runner', 'RB', 'SEA'),
                    '103': MFLPlayer('103', 'Your Defense', 'Def', 'DET'),
                    '201': MFLPlayer('201', 'Their Quarterback', 'QB', 'BUF'),
                    '202': MFLPlayer('202', 'Their Defense', 'Def', 'BUF')}
    rosters = {'0001': {'101', '102', '103'}, '0002': {'201', '202'}}
    monkeypatch.setattr(mfl, 'trade_rosters', lambda: rosters)
    monkeypatch.setattr(mfl, 'franchise_roster', lambda fid: rosters[fid])
    monkeypatch.setattr(mfl, 'franchise_names', lambda: {'0001': 'Your Team', '0002': 'Other Team'})
    pick_assets = {
        '0001': (MFLTradeAsset('FP_0001_2027_1', '0001', '2027 Round 1 pick'),),
        '0002': (MFLTradeAsset('FP_0002_2027_2', '0002', '2027 Round 2 pick'),),
    }
    monkeypatch.setattr(mfl, 'trade_pick_assets', lambda: pick_assets)
    calls = []
    def write(kind, **params):
        calls.append((kind, params))
        return {'status': {'$t': 'OK'}}
    monkeypatch.setattr(mfl, 'import_request', write)
    monkeypatch.setattr(web, '_client', lambda *a: mfl)
    session = web.BrowserSession('fake-cookie', 2026, [MFLLeague('12345', '0001', 'Test League')], 'csrf')
    monkeypatch.setattr(web, 'sessions', {'test': session})
    client = TestClient(web.app)
    client.cookies.set('wp_session', 'test')
    return client, mfl, session, rosters, calls


def preview(client, **changes):
    form = {'league': '12345', 'target': '0002', 'give': ['101', '102'], 'receive': ['201'], 'comments': 'A fair offer', 'csrf_token': 'csrf'}
    form.update(changes)
    return client.post('/trades/preview', data=form, follow_redirects=False)


def test_build_review_then_send_exact_offer_once(trade_app):
    client, mfl, session, rosters, calls = trade_app
    page = client.get('/trades?league=12345&target=0002')
    assert page.status_code == 200
    assert 'Your Receiver' in page.text and 'Their Quarterback' in page.text
    assert 'Your Defense' in page.text and 'Their Defense' in page.text
    assert page.text.index('Your Receiver') < page.text.index('Your Defense')
    assert page.text.index('Their Quarterback') < page.text.index('Their Defense')
    assert '3 players · complete roster' in page.text
    assert '2 players · complete roster' in page.text
    assert 'href="/trades?league=12345"' in page.text
    response = preview(client, comments='<script>not executable</script>')
    assert response.status_code == 303 and not calls
    url = response.headers['location']
    review = client.get(url)
    assert 'Confirm &amp; send offer to MFL' in review.text
    assert '&lt;script&gt;not executable&lt;/script&gt;' in review.text
    draft_id = url.rsplit('/', 1)[1]
    result = client.post(f'/trades/send/{draft_id}', data={'csrf_token': 'csrf'})
    assert result.status_code == 200 and 'Offer sent to Other Team' in result.text
    assert calls == [('tradeProposal', {'OFFEREDTO': '0002', 'WILL_GIVE_UP': '101,102', 'WILL_RECEIVE': '201',
                                      'COMMENTS': '<script>not executable</script>', 'FRANCHISE_ID': '0001'})]
    client.post(f'/trades/send/{draft_id}', data={'csrf_token': 'csrf'})
    client.get(url)
    assert len(calls) == 1


def test_trade_builder_reviews_and_sends_draft_picks(trade_app):
    client, mfl, session, rosters, calls = trade_app
    page = client.get('/trades?league=12345&target=0002')
    assert '2027 Round 1 pick' in page.text and '2027 Round 2 pick' in page.text
    response = preview(
        client, give=['101'], receive=[], give_asset=['FP_0001_2027_1'],
        receive_asset=['FP_0002_2027_2'],
    )
    assert response.status_code == 303
    review_url = response.headers['location']
    review = client.get(review_url)
    assert '2027 Round 1 pick' in review.text and '2027 Round 2 pick' in review.text
    result = client.post(review_url.replace('/review/', '/send/'), data={'csrf_token': 'csrf'})
    assert result.status_code == 200
    assert calls[0][1]['WILL_GIVE_UP'] == '101,FP_0001_2027_1'
    assert calls[0][1]['WILL_RECEIVE'] == 'FP_0002_2027_2'


@pytest.mark.parametrize('change', [
    {'target': '0001'}, {'target': '0000'}, {'target': '9999'},
    {'give': []}, {'receive': []}, {'give': ['201']}, {'receive': ['101']},
    {'give': ['101,102']}, {'give': ['FP_0001_2027_1']},
])
def test_invalid_offer_is_blocked_before_any_write(trade_app, change):
    client, mfl, session, rosters, calls = trade_app
    response = preview(client, **change)
    assert response.status_code == 400
    assert 'Offer not ready' in response.text
    assert not calls and not session.trades


def test_revalidates_ownership_after_review(trade_app):
    client, mfl, session, rosters, calls = trade_app
    response = preview(client)
    draft_id = response.headers['location'].rsplit('/', 1)[1]
    rosters['0002'].clear()
    result = client.post(f'/trades/send/{draft_id}', data={'csrf_token': 'csrf'})
    assert 'no longer on that team' in result.text
    assert not calls
    assert session.trades[draft_id].status == 'failed'


def test_auth_csrf_expiry_and_cross_session(trade_app):
    client, mfl, session, rosters, calls = trade_app
    assert preview(client, csrf_token='wrong').status_code == 403
    assert preview(client, league='99999').status_code == 404
    response = preview(client)
    draft_id = response.headers['location'].rsplit('/', 1)[1]
    assert client.post(f'/trades/send/{draft_id}', data={'csrf_token': 'wrong'}).status_code == 403
    assert session.trades[draft_id].status == 'draft'
    session.trades[draft_id].created_at = time.monotonic() - 1201
    assert 'preview expired' in client.post(f'/trades/send/{draft_id}', data={'csrf_token': 'csrf'}).text
    web.sessions['other'] = web.BrowserSession('other-cookie', 2026, session.leagues, 'other-csrf')
    client.cookies.set('wp_session', 'other')
    assert client.get(f'/trades/review/{draft_id}').status_code == 404
    assert client.post(f'/trades/send/{draft_id}', data={'csrf_token': 'other-csrf'}).status_code == 404
    client.cookies.clear()
    assert client.get('/trades?league=12345', follow_redirects=False).status_code == 303
    assert preview(client).status_code == 401
    assert not calls


@pytest.mark.parametrize('outcome', ['timeout', 'unknown'])
def test_uncertain_send_is_not_retried(trade_app, monkeypatch, outcome):
    client, mfl, session, rosters, calls = trade_app
    response = preview(client)
    draft_id = response.headers['location'].rsplit('/', 1)[1]
    def unconfirmed(kind, **params):
        calls.append((kind, params))
        if outcome == 'timeout':
            raise MFLApiError('network failure')
        return {'unexpected': 'reply'}
    monkeypatch.setattr(mfl, 'import_request', unconfirmed)
    result = client.post(f'/trades/send/{draft_id}', data={'csrf_token': 'csrf'})
    assert 'Offer not confirmed' in result.text and 'Pending Trades' in result.text
    assert session.trades[draft_id].status == 'uncertain'
    client.post(f'/trades/send/{draft_id}', data={'csrf_token': 'csrf'})
    assert len(calls) == 1


def test_trade_roster_parser_keeps_teams_separate(monkeypatch):
    mfl = MFLClient(MFLConfig(2026, '12345', '0001'))
    monkeypatch.setattr(mfl, 'export', lambda *a, **k: {'rosters': {'franchise': [
        {'id': '1', 'player': [{'id': '101'}, {'id': '102', 'status': 'IR'}]},
        {'id': '0002', 'player': {'id': '201'}},
    ]}})
    assert mfl.trade_rosters() == {'0001': {'101', '102'}, '0002': {'201'}}


def test_trade_pick_assets_are_parsed_and_sent_with_players(monkeypatch):
    mfl = MFLClient(MFLConfig(2026, '12345', '0001'))
    rosters = {'0001': {'101'}, '0002': {'201'}}
    payload = {'assets': {'franchise': [
        {'id': '1', 'asset': [{'id': '101'}, {'id': 'FP_0003_2027_1'}]},
        {'id': '2', 'asset': [{'id': '201'}, {'id': 'DP_2_05'}]},
    ]}}
    monkeypatch.setattr(mfl, 'export', lambda kind, **kwargs: payload)
    monkeypatch.setattr(mfl, 'trade_rosters', lambda: rosters)
    calls = []
    monkeypatch.setattr(mfl, 'import_request', lambda kind, **kwargs: calls.append((kind, kwargs)) or {'status': {'$t': 'OK'}})
    assets = mfl.trade_pick_assets()
    assert assets['0001'][0].code == 'FP_0003_2027_1'
    assert assets['0002'][0].code == 'DP_2_05'
    mfl.propose_player_trade(
        target='0002', give=['101'], receive=['201'],
        give_assets=['FP_0003_2027_1'], receive_assets=['DP_2_05'],
    )
    assert calls[0][1]['WILL_GIVE_UP'] == '101,FP_0003_2027_1'
    assert calls[0][1]['WILL_RECEIVE'] == '201,DP_2_05'
