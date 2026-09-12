from __future__ import annotations

from typing import Any

import pytest
import requests

from weekly_projections.mfl.client import MFLAvailability, MFLClient, MFLConfig, MFLPlayer


def _config(**overrides: Any) -> MFLConfig:
    values = {
        "year": 2026,
        "league_id": "12345",
        "franchise_id": "0007",
        "username": "owner",
        "password": "top-secret",
    }
    values.update(overrides)
    return MFLConfig(**values)


class LoginResponse:
    text = '<status MFL_USER_ID="cookie-value" />'

    def raise_for_status(self) -> None:
        return None


class LoginSession:
    def __init__(self) -> None:
        self.cookies = requests.cookies.RequestsCookieJar()
        self.calls: list[tuple[str, dict[str, str]]] = []

    def post(self, url: str, *, data: dict[str, str], **kwargs: Any) -> LoginResponse:
        self.calls.append((url, data))
        return LoginResponse()


class JsonResponse:
    text = ""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class LeagueSession(LoginSession):
    def get(self, url: str, **kwargs: Any) -> JsonResponse:
        return JsonResponse(
            {
                "leagues": {
                    "league": [
                        {
                            "league_id": "11111",
                            "franchise_id": "0001",
                            "name": "First League",
                            "url": "https://www.example/11111",
                        },
                        {
                            "league_id": "22222",
                            "franchise_id": "0007",
                            "name": "Second League",
                            "url": "https://www.example/22222",
                        },
                    ]
                }
            }
        )


@pytest.mark.parametrize("feed", ["nflSchedule", "players", "injuries"])
def test_global_feeds_use_api_host_without_league_or_login(feed):
    class PublicSession:
        def get(self, url, **kwargs):
            self.url, self.params = url, kwargs["params"]
            return JsonResponse({})
    session = PublicSession()
    client = MFLClient(_config(base_url="https://www42.myfantasyleague.com", username=None, password=None), session=session)
    client.export(feed, W=1)
    assert session.url == "https://api.myfantasyleague.com/2026/export"
    assert "L" not in session.params
    assert session.params["W"] == 1


def test_default_week_is_current_scoring_week_not_next_lineup_week():
    class StatusSession:
        def get(self, url, **kwargs):
            return JsonResponse({"mfl_status": {"weeks": {"LineupWeek": "2", "UpcomingWeek": "2", "CurrentWeek": "1", "LiveScoringWeek": "1"}}})
    client = MFLClient(_config(), session=StatusSession())
    assert client.current_week() == 1


def test_player_card_uses_api_metadata_and_valid_photo_id():
    player = MFLPlayer.from_dict({"id":"13116", "name":"Mahomes, Patrick", "espn_id":"3139477", "jersey":"15", "college":"Texas Tech"})
    assert player.photo_url == "https://a.espncdn.com/i/headshots/nfl/players/full/3139477.png"
    assert player.college == "Texas Tech"
    assert not MFLPlayer.from_dict({"espn_id":"../unsafe"}).photo_url


def test_league_details_parse_divisions_logos_and_reject_external_artwork(monkeypatch):
    client = MFLClient(_config(user_cookie="cookie"))
    payload = {"league": {
        "divisions": {"division": [{"id":"00","name":"East"},{"id":"01","name":"West"}]},
        "franchises": {"franchise": [
            {"id":"1","name":"One","division":"00","logo":"http://www42.myfantasyleague.com/team.png"},
            {"id":"2","name":"Two","division":"01","logo":"https://tracker.example/team.png","icon":"https://www42.myfantasyleague.com/icon.gif"},
            {"id":"3","name":"Three","division":"01","logo":"https://user:secret@www42.myfantasyleague.com/no.png"},
        ]},
    }}
    calls = []
    monkeypatch.setattr(client, "export", lambda kind, **params: calls.append(kind) or payload)
    details = client.league_details()
    assert details.divisions == (("00","East"),("01","West"))
    assert details.franchises["0001"].logo_url == "https://www42.myfantasyleague.com/team.png"
    assert details.franchises["0002"].logo_url == "https://www42.myfantasyleague.com/icon.gif"
    assert details.franchises["0003"].logo_url == ""
    assert client.franchise_names() == {"0001":"One","0002":"Two","0003":"Three"}
    assert calls == ["league"]


def test_login_posts_credentials_and_stores_cookie() -> None:
    session = LoginSession()
    client = MFLClient(_config(), session=session)  # type: ignore[arg-type]
    client.login()

    url, data = session.calls[0]
    assert url == "https://api.myfantasyleague.com/2026/login"
    assert "top-secret" not in url
    assert data["PASSWORD"] == "top-secret"
    assert session.cookies.get("MFL_USER_ID") == "cookie-value"


def test_authenticated_account_discovers_multiple_leagues() -> None:
    session = LeagueSession()
    client = MFLClient(_config(), session=session)  # type: ignore[arg-type]
    client.login()
    leagues = client.account_leagues()
    assert [(league.id, league.franchise_id) for league in leagues] == [
        ("11111", "0001"),
        ("22222", "0007"),
    ]


def test_api_key_authenticates_exports_imports_and_league_discovery_without_login():
    class KeySession:
        def __init__(self):
            self.cookies = requests.cookies.RequestsCookieJar()
            self.gets = []
            self.posts = []
        def get(self, url, **kwargs):
            self.gets.append((url, kwargs.get("params", {})))
            if kwargs.get("params", {}).get("TYPE") == "myleagues":
                return JsonResponse({"leagues":{"league":{"league_id":"11111","franchise_id":"0001","name":"One"}}})
            return JsonResponse({"league":{"franchises":{}}})
        def post(self, url, **kwargs):
            self.posts.append((url, kwargs.get("data", {})))
            return JsonResponse({"status":{"$t":"OK"}})
    transport = KeySession()
    client = MFLClient(_config(username=None, password=None, api_key="temporary-secret"), session=transport)
    client.export("league")
    assert client.account_leagues()[0].id == "11111"
    client.import_request("testWrite", VALUE="one")
    assert not any("temporary-secret" in url for url, _ in transport.gets + transport.posts)
    assert all(params["APIKEY"] == "temporary-secret" for _, params in transport.gets)
    assert transport.posts[0][1]["APIKEY"] == "temporary-secret"
    assert not any(url.endswith("/login") for url, _ in transport.posts)


def test_preview_validates_free_agent_and_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    players = {
        "100": MFLPlayer("100", "Add Me", "WR", "BUF"),
        "200": MFLPlayer("200", "Drop Me", "WR", "NYJ"),
    }
    monkeypatch.setattr(client, "players", lambda refresh=False: players)
    monkeypatch.setattr(
        client, "free_agents", lambda: {"100": MFLAvailability("100")}
    )
    monkeypatch.setattr(client, "roster_ids", lambda: {"200"})

    preview = client.preview_add_drop(add="Add Me", drop="Drop Me")
    assert preview.add.id == "100"
    assert preview.drop.id == "200"
    assert preview.mode == "fcfs"


def test_preview_rejects_player_not_on_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    players = {
        "100": MFLPlayer("100", "Add Me"),
        "200": MFLPlayer("200", "Not Mine"),
    }
    monkeypatch.setattr(client, "players", lambda refresh=False: players)
    monkeypatch.setattr(
        client, "free_agents", lambda: {"100": MFLAvailability("100")}
    )
    monkeypatch.setattr(client, "roster_ids", lambda: set())

    with pytest.raises(ValueError, match="not on franchise"):
        client.preview_add_drop(add="100", drop="200")


def test_submit_fcfs_uses_official_mfl_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    players = {
        "100": MFLPlayer("100", "Add Me"),
        "200": MFLPlayer("200", "Drop Me"),
    }
    monkeypatch.setattr(client, "players", lambda refresh=False: players)
    monkeypatch.setattr(
        client, "free_agents", lambda: {"100": MFLAvailability("100")}
    )
    monkeypatch.setattr(client, "roster_ids", lambda: {"200"})
    preview = client.preview_add_drop(add="100", drop="200")
    recorded: dict[str, Any] = {}

    def record(request_type: str, **parameters: Any) -> dict[str, str]:
        recorded.update({"request_type": request_type, **parameters})
        return {"status": "ok"}

    monkeypatch.setattr(client, "import_request", record)
    assert client.submit_add_drop(preview) == {"status": "ok"}
    assert recorded == {
        "request_type": "fcfsWaiver",
        "ADD": "100",
        "DROP": "200",
        "FRANCHISE_ID": None,
    }


def test_blind_bid_requires_bid(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    players = {"100": MFLPlayer("100", "Add"), "200": MFLPlayer("200", "Drop")}
    monkeypatch.setattr(client, "players", lambda refresh=False: players)
    monkeypatch.setattr(
        client, "free_agents", lambda: {"100": MFLAvailability("100")}
    )
    monkeypatch.setattr(client, "roster_ids", lambda: {"200"})

    with pytest.raises(ValueError, match="requires a non-negative --bid"):
        client.preview_add_drop(add="100", drop="200", mode="blind-bid")


def test_free_agents_preserve_mfl_lock_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    monkeypatch.setattr(
        client,
        "export",
        lambda request_type, **kwargs: {
            "freeAgents": {
                "league": {
                    "player": [
                        {"id": "100", "status": "available"},
                        {"id": "200", "status": "locked"},
                    ]
                }
            }
        },
    )

    free_agents = client.free_agents()
    assert free_agents["100"].claimable is True
    assert free_agents["100"].label == "Free agent"
    assert free_agents["200"].claimable is False
    assert free_agents["200"].label == "Locked"


def test_projected_scores_parse_league_scored_points(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    recorded: dict[str, Any] = {}

    def export(request_type: str, **kwargs: Any) -> dict[str, Any]:
        recorded.update({"request_type": request_type, **kwargs})
        return {
            "projectedScores": {
                "playerScore": [
                    {"id": "100", "score": "17.25"},
                    {"id": "200", "score": "not-published"},
                ]
            }
        }

    monkeypatch.setattr(client, "export", export)
    scores = client.projected_scores(player_ids=["100", "200"])
    assert scores == {"100": 17.25}
    assert recorded["request_type"] == "projectedScores"
    assert recorded["PLAYERS"] == "100,200"


def test_preview_rejects_locked_player(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    players = {
        "100": MFLPlayer("100", "Locked Player"),
        "200": MFLPlayer("200", "Drop Me"),
    }
    monkeypatch.setattr(client, "players", lambda refresh=False: players)
    monkeypatch.setattr(
        client,
        "free_agents",
        lambda: {"100": MFLAvailability("100", status="locked", locked=True)},
    )
    monkeypatch.setattr(client, "roster_ids", lambda: {"200"})

    with pytest.raises(ValueError, match="currently locked by MFL"):
        client.preview_add_drop(add="100", drop="200")


def test_lineup_settings_parse_fixed_and_flex_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    monkeypatch.setattr(
        client,
        "export",
        lambda request_type, **kwargs: {
            "league": {
                "starters": {
                    "count": "4",
                    "position": [
                        {"name": "QB", "limit": "1"},
                        {"name": "RB", "limit": "1-2"},
                        {"name": "WR", "limit": "1-2"},
                    ],
                }
            }
        },
    )

    settings = client.lineup_settings()
    assert settings.starter_count == 4
    assert [(rule.name, rule.minimum, rule.maximum) for rule in settings.rules] == [
        ("QB", 1, 1),
        ("RB", 1, 2),
        ("WR", 1, 2),
    ]


def test_player_roster_statuses_find_current_starters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    monkeypatch.setattr(
        client,
        "export",
        lambda request_type, **kwargs: {
            "playerRosterStatus": {
                "player": [
                    {
                        "id": "100",
                        "roster_franchise": {"id": "0007", "status": "S"},
                    },
                    {
                        "id": "200",
                        "roster_franchise": {"id": "0007", "status": "NS"},
                    },
                ]
            }
        },
    )
    assert client.player_roster_statuses(["100", "200"], week=1) == {
        "100": "S",
        "200": "NS",
    }


def test_submit_lineup_uses_official_mfl_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    recorded: dict[str, Any] = {}

    def record(request_type: str, **parameters: Any) -> dict[str, str]:
        recorded.update({"request_type": request_type, **parameters})
        return {"status": "ok"}

    monkeypatch.setattr(client, "import_request", record)
    result = client.submit_lineup(week=1, starter_ids=["200", "100"])
    assert result == {"status": "ok"}
    assert recorded == {
        "request_type": "lineup",
        "W": 1,
        "STARTERS": "100,200",
        "COMMENTS": "Submitted by Weekly Projections",
        "FRANCHISE_ID": None,
    }


def test_nfl_team_kickoffs_parse_real_mfl_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    monkeypatch.setattr(
        client,
        "export",
        lambda request_type, **kwargs: {
            "nflSchedule": {
                "matchup": [
                    {
                        "kickoff": "1788999600",
                        "team": [{"id": "NEP"}, {"id": "SEA"}],
                    }
                ]
            }
        },
    )
    assert client.nfl_team_kickoffs(week=1) == {
        "NEP": 1788999600,
        "SEA": 1788999600,
    }


def test_live_scoring_parses_matchup_and_players(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    monkeypatch.setattr(
        client,
        "export",
        lambda request_type, **kwargs: {
            "liveScoring": {
                "week": "1",
                "matchup": {
                    "franchise": [
                        {
                            "id": "0007",
                            "score": "18.25",
                            "isHome": "1",
                            "playersYetToPlay": "2",
                            "playersCurrentlyPlaying": "1",
                            "gameSecondsRemaining": "7200",
                            "players": {
                                "player": {
                                    "id": "100",
                                    "score": "8.25",
                                    "status": "starter",
                                    "gameSecondsRemaining": "1200",
                                }
                            },
                        }
                    ]
                },
            }
        },
    )
    live = client.live_scoring(week=1)
    team = live.matchups[0].franchises[0]
    assert team.franchise_id == "0007"
    assert team.score == 18.25
    assert team.players[0].player_id == "100"
    assert team.players[0].score == 8.25
