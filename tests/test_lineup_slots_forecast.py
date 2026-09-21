from dataclasses import replace
from weekly_projections.lineup import assign_lineup_slots, lineup_slots
from weekly_projections.mfl.client import MFLClient, MFLConfig, MFLPlayer, MFLLineupRule, MFLLineupSettings, MFLLiveScoring, MFLLiveMatchup, MFLLiveFranchise, MFLLivePlayer
from weekly_projections.web.app import HeadToHeadView, LivePlayerView, LiveTeamView


def test_bench_pairs_exact_positions_then_packs_leftovers():
    def team(number, positions):
        players = tuple(LivePlayerView(MFLPlayer(f'{number}-{i}', f'Player {i}', pos), 0, 'nonstarter', 3600) for i, pos in enumerate(positions))
        return LiveTeamView(str(number), str(number), 0, False, 0, 0, players)

    left = team(1, ['QB', 'RB', 'RB', 'WR', 'TE', 'K'])
    right = team(2, ['QB', 'WR', 'WR', 'PK', 'Def'])
    starter = LivePlayerView(MFLPlayer('starter', 'Starter', 'TE'), 10, 'starter', 0)
    left = replace(left, players=left.players + (starter,))
    rows = HeadToHeadView((left, right)).rows(False)
    assert len(rows) == 6  # Only the real bench-size difference leaves a gap.
    assert [label for a, b, label in rows[:3]] == ['QB', 'WR', 'PK']
    assert all(a and b for a, b, _ in rows[:5])
    assert rows[-1][1] is None
    assert all(label == 'BN' for a, b, label in rows[3:])
    assert {a.player.id for a, _, _ in rows if a} == {p.player.id for p in left.players if not p.is_starter}
    assert {b.player.id for _, b, _ in rows if b} == {p.player.id for p in right.players}
    assert len(HeadToHeadView((right, left)).rows(False)) == 6
    assert len(HeadToHeadView((left,)).rows(False)) == 6
    assert HeadToHeadView(()).rows(False) == []


def test_variable_position_limits_become_shared_flex_slots():
    settings = MFLLineupSettings(5, (MFLLineupRule('QB',1,1), MFLLineupRule('RB',1,3), MFLLineupRule('WR',1,3), MFLLineupRule('TE',0,2)))
    assert [s['label'] for s in lineup_slots(settings)] == ['QB','RB','WR','FLEX','FLEX']
    first = [MFLPlayer(str(i),str(i),pos) for i,pos in enumerate(['QB','RB','WR','TE','TE'])]
    second = [MFLPlayer(str(i+10),str(i),pos) for i,pos in enumerate(['QB','RB','RB','WR','WR'])]
    left, right = assign_lineup_slots(first,settings), assign_lineup_slots(second,settings)
    assert all(player for _,player in left+right)
    assert {player.position for label,player in left if label == 'FLEX'} == {'TE'}
    assert {player.position for label,player in right if label == 'FLEX'} == {'RB','WR'}
    teams = tuple(LiveTeamView(str(i),str(i),0,False,5,0,tuple(LivePlayerView(p,0,'starter',3600,10) for p in roster)) for i,roster in enumerate([first,second]))
    view = HeadToHeadView(teams,1,1,settings)
    assert len(view.rows()) == 5
    assert all(a and b for a,b,_ in view.rows())


def test_estimate_symmetric_missing_and_final_states():
    p = MFLPlayer('p','Player','QB')
    left = LiveTeamView('1','One',0,False,1,0,(LivePlayerView(p,0,'starter',3600,20),))
    right = replace(left,franchise_id='2',name='Two')
    view = HeadToHeadView((left,right),1,1)
    assert view.forecast['percentages'] == (50,50)
    asymmetric = replace(
        right,
        players=(replace(right.players[0], projection=12),),
    )
    percentages = replace(view, teams=(left, asymmetric)).forecast['percentages']
    assert percentages[0] != round(percentages[0])
    assert all(value == round(value, 2) for value in percentages)
    assert sum(percentages) == 100
    missing = replace(right,players=(replace(right.players[0],projection=None),))
    assert replace(view,teams=(left,missing)).forecast is None
    final_left = replace(left,score=27,players_yet_to_play=0,players=(replace(left.players[0],game_seconds_remaining=0),))
    final_right = replace(right,score=20,players_yet_to_play=0,players=(replace(right.players[0],game_seconds_remaining=0),))
    assert replace(view,teams=(final_left,final_right)).forecast['percentages'] == (100,0)
    assert replace(view,teams=(final_left,replace(final_right,score=27))).forecast['percentages'] == (None,None)


def test_roster_status_accepts_franchise_shape_and_live_fallback(monkeypatch):
    client = MFLClient(MFLConfig(2026,'league','0001'))
    monkeypatch.setattr(client,'export',lambda *a,**k: {'playerRosterStatus':{'player':[{'id':'a','franchise':{'id':'1','status':'S'}},{'id':'b','roster_franchise':{'id':'0001','status':'R'}}]}})
    live = MFLLiveScoring(1,(MFLLiveMatchup((MFLLiveFranchise('0001',0,False,1,0,3600,(MFLLivePlayer('b',0,'nonstarter',3600),)),)),))
    monkeypatch.setattr(client,'live_scoring',lambda **k: live)
    assert client.player_roster_statuses(['a','b'],week=1) == {'a':'S','b':'NS'}


def test_league_points_are_not_blended_or_filled_by_generic_ml(monkeypatch):
    from weekly_projections import projection_sources as sources
    monkeypatch.setattr(sources,'stathead_weekly_scores',lambda *a,**k: ({'a':100,'b':200},None))
    result = sources.projection_blend([MFLPlayer('a','A','QB'),MFLPlayer('b','B','RB')],year=2026,week=1,mfl_scores={'a':28.5})
    assert result.scores == {'a':28.5}
    assert result.ml_scores == {'a':100,'b':200}
