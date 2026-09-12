from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from weekly_projections.mfl.client import MFLClient, MFLConfig, MFLLeague, MFLPlayer, MFLApiError
from weekly_projections.mfl.client import MFLLiveFranchise, MFLLivePlayer, MFLLiveMatchup, MFLLiveScoring
from weekly_projections.projection_sources import ProjectionBlend
from weekly_projections.web import app as web


@pytest.mark.parametrize('clock,kickoff,active,next_time', [
    ('3600',2000,False,2000), ('3600',900,False,None), ('3500',900,True,None),
    ('1800',900,True,None), ('0',900,False,None), ('bad',900,False,None),
])
def test_refresh_requires_live_nfl_clock(monkeypatch, clock, kickoff, active, next_time):
    client = MFLClient(MFLConfig(2026,'1','0001'))
    monkeypatch.setattr(client,'export',lambda *a,**k:{'nflSchedule':{'matchup':{'kickoff':kickoff,'gameSecondsRemaining':clock}}})
    assert client.nfl_refresh_state(week=1,now=1000) == {'active':active,'next_kickoff':next_time}


def test_other_matchup_selection_loads_its_players_and_keeps_week(monkeypatch):
    def team(fid, pid):
        return MFLLiveFranchise(fid,10,False,1,0,3600,(MFLLivePlayer(pid,10,'starter',3600),))
    live = MFLLiveScoring(1,(MFLLiveMatchup((team('0001','1'),team('0002','2'))),
                              MFLLiveMatchup((team('0003','3'),team('0004','4')))))
    mfl = MFLClient(MFLConfig(2026,'1','0001'))
    mfl._players = {str(i):MFLPlayer(str(i),f'Player {i}','QB') for i in range(1,5)}
    monkeypatch.setattr(mfl,'current_week',lambda:1)
    monkeypatch.setattr(mfl,'live_scoring',lambda **k:live)
    monkeypatch.setattr(mfl,'franchise_names',lambda:{f'{i:04d}':f'Team {i}' for i in range(1,5)})
    monkeypatch.setattr(mfl,'projected_scores',lambda **k:{pid:20 for pid in k['player_ids']})
    monkeypatch.setattr(mfl,'lineup_settings',lambda:None)
    monkeypatch.setattr(mfl,'nfl_refresh_state',lambda **k:{'active':False,'next_kickoff':2000000000})
    monkeypatch.setattr(web,'projection_blend',lambda *a,**k:ProjectionBlend(k['mfl_scores'],k['mfl_scores'],{},0))
    monkeypatch.setattr(web,'_client',lambda *a:mfl)
    monkeypatch.setattr(web,'sessions',{'test':web.BrowserSession('fake',2026,[MFLLeague('1','0001','League')],'csrf')})
    client = TestClient(web.app); client.cookies.set('wp_session','test')
    page = client.get('/scores?league=1&week=1&matchup=1')
    assert page.status_code == 200 and 'Player 3' in page.text and 'Player 1' not in page.text
    assert 'data-live-refresh="0"' in page.text
    assert 'matchup=1' in page.text
    assert 'Player 1' in client.get('/scores?league=1&week=1&matchup=').text
    assert 'unavailable for this week' in client.get('/scores?league=1&week=1&matchup=9').text
    # Gate reads only NFL status; it never fetches fantasy scoring.
    monkeypatch.setattr(mfl,'live_scoring',lambda **k:pytest.fail('Gate fetched scores'))
    assert client.get('/api/live-window?league=1&week=1').json()['active'] is False
    assert client.get('/api/live-window?league=1&week=2').json() == {'active':False,'next_kickoff':None}
    assert client.get('/api/live-window?league=999&week=1').status_code == 404


def test_selected_team_uses_public_read_without_credentials_then_authenticated_fallback(monkeypatch):
    client = MFLClient(MFLConfig(2026,'1','0001',user_cookie='synthetic-secret'))
    calls = []
    def public(url, **kwargs):
        calls.append((url,kwargs))
        return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'rosters':{'franchise':{'id':'0002','player':{'id':'201'}}}})
    monkeypatch.setattr('weekly_projections.mfl.client.requests.get',public)
    monkeypatch.setattr(client,'export',lambda *a,**k:pytest.fail('Public roster should not need auth'))
    assert client.franchise_roster('2') == {'201'}
    assert calls[0][1]['params'] == {'TYPE':'rosters','L':'1','JSON':'1','FRANCHISE':'0002'}
    assert 'synthetic-secret' not in str(calls) and 'cookies' not in calls[0][1]
    def private(url,**kwargs):
        return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'error':{'$t':'Private'}})
    monkeypatch.setattr('weekly_projections.mfl.client.requests.get',private)
    monkeypatch.setattr(client,'export',lambda *a,**k:{'rosters':{'franchise':{'id':'0002','player':{'id':'202'}}}})
    assert client.franchise_roster('0002') == {'202'}
