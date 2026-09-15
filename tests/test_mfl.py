from __future__ import annotations

from typing import Any

import pytest
import requests

from weekly_projections.mfl.client import (
    AddDropPreview,
    MFLApiError,
    MFLAvailability,
    MFLClient,
    MFLConfig,
    MFLLeague,
    MFLPlayer,
    MFLRateLimitError,
    MFLWriteUncertainError,
)


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


class HtmlResponse:
    text = "<html><body>No API receipt</body></html>"
    status_code = 200
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        raise requests.exceptions.JSONDecodeError("not json", self.text, 0)


class RedirectResponse:
    text = ""
    status_code = 302
    headers = {"Location": "https://www42.myfantasyleague.com/2026/import"}


def test_http_429_returns_actionable_message_without_retrying():
    response = requests.Response()
    response.status_code = 429
    response.url = "https://api.myfantasyleague.com/2026/export?TYPE=playerRosterStatus"
    response.headers["Retry-After"] = "45"
    client = MFLClient(_config())
    with pytest.raises(MFLRateLimitError, match="HTTP 429.*45 seconds") as caught:
        client._decode(response)
    assert caught.value.retry_after == 45


def test_default_transport_does_not_retry_http_429():
    client = MFLClient(_config())
    retries = client.session.get_adapter("https://").max_retries
    assert 429 not in retries.status_forcelist
    assert 500 in retries.status_forcelist


def test_import_html_response_is_unconfirmed_and_never_retried(monkeypatch):
    client = MFLClient(_config(user_cookie="cookie"))
    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return HtmlResponse()

    monkeypatch.setattr(client.session, "post", post)
    with pytest.raises(MFLWriteUncertainError, match="readable transaction receipt"):
        client.import_request("fcfsWaiver", ADD="100", DROP="200")
    assert len(calls) == 1
    assert calls[0][1]["allow_redirects"] is False


def test_import_redirect_requires_reconnect_and_is_never_followed(monkeypatch):
    client = MFLClient(_config(user_cookie="cookie"))
    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return RedirectResponse()

    monkeypatch.setattr(client.session, "post", post)
    with pytest.raises(MFLApiError, match="Sign out and reconnect"):
        client.import_request("lineup", W=1, STARTERS="100")
    assert len(calls) == 1
    assert calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www42.myfantasyleague.com/2026/home/12345", "https://www42.myfantasyleague.com"),
        ("http://www7.myfantasyleague.com/2026/home/12345", "https://www7.myfantasyleague.com"),
        ("https://www42.myfantasyleague.com.evil.example/2026", "https://api.myfantasyleague.com"),
        ("https://user:secret@www42.myfantasyleague.com/2026", "https://api.myfantasyleague.com"),
        ("", "https://api.myfantasyleague.com"),
    ],
)
def test_league_api_base_url_only_accepts_mfl_hosts(url, expected):
    assert MFLLeague("12345", "0001", "League", url).api_base_url == expected


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
            self.url = url
            return JsonResponse({"mfl_status": {"weeks": {"LineupWeek": "2", "UpcomingWeek": "2", "CurrentWeek": "1", "LiveScoringWeek": "1"}}})
    session = StatusSession()
    client = MFLClient(_config(base_url="https://www49.myfantasyleague.com"), session=session)
    assert client.current_week() == 1
    assert session.url == "https://api.myfantasyleague.com/fflnetdynamic2026/mfl_status.json"


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


def test_league_details_parse_faab_weeks_and_history(monkeypatch):
    client = MFLClient(_config(user_cookie="cookie"))
    monkeypatch.setattr(client, "export", lambda kind, **params: {"league": {
        "name": "Test League", "startWeek": "1", "endWeek": "17",
        "lastRegularSeasonWeek": "14", "bbidSeasonLimit": "100",
        "history": {"league": [{"year": "2026"}, {"year": "2025"}]},
        "franchises": {"franchise": {"id": "1", "name": "One",
            "bbidAvailableBalance": "74", "waiverSortOrder": "3"}},
    }})
    details = client.league_details()
    assert details.name == "Test League"
    assert (details.start_week, details.end_week, details.last_regular_season_week) == (1, 17, 14)
    assert details.faab_limit == 100
    assert details.history_years == (2026, 2025)
    assert details.franchises["0001"].faab_balance == 74
    assert details.franchises["0001"].waiver_order == 3


def test_schedule_and_transactions_parse_singleton_and_list_payloads(monkeypatch):
    client = MFLClient(_config(user_cookie="cookie"))
    payloads = {
        "schedule": {"schedule": {"weeklySchedule": [
            {"week": "1", "matchup": {"franchise": [
                {"id": "1", "score": "121.5"}, {"id": "2", "score": {"$t": "110"}}]}},
            {"week": "2", "matchup": [{"franchise": [
                {"id": "1", "score": ""}, {"id": "3", "score": ""}]}]},
        ]}},
        "transactions": {"transactions": {"transaction": [
            {"id": "t1", "type": "BBID_WAIVER", "timestamp": "100",
             "franchise": "1", "transaction": "1234|ADD,5678|DROP"},
            {"id": "t2", "type": "TRADE", "timestamp": "200", "franchise1": "1", "franchise2": "2"},
            {"id": "t3", "type": "FREE_AGENT", "timestamp": "300",
             "franchise": "1", "transaction": "17474,|15738,"},
            {"id": "t4", "type": "BBID_WAIVER", "timestamp": "400",
             "franchise": "2", "transaction": "0514,|3|0504,"},
            {"id": "t5", "type": "TRADE", "timestamp": "500",
             "franchise1": "1", "franchise2": "2", "transaction": "17474,|15738,"},
        ]}},
    }
    calls = []
    monkeypatch.setattr(client, "export", lambda kind, **params: calls.append((kind, params)) or payloads[kind])
    games = client.fantasy_schedule()
    assert games[0].team_ids == ("0001", "0002")
    assert games[0].scores == (121.5, 110.0)
    assert games[1].scores == (None, None)
    activity = client.transactions()
    by_id = {item.id: item for item in activity}
    assert by_id["t1"].adds == ("1234",)
    assert by_id["t1"].drops == ("5678",)
    assert by_id["t1"].franchise_ids == ("0001",)
    assert by_id["t3"].adds == ("17474",)
    assert by_id["t3"].drops == ("15738",)
    assert by_id["t3"].bid is None
    assert by_id["t4"].adds == ("0514",)
    assert by_id["t4"].drops == ("0504",)
    assert by_id["t4"].bid == 3
    assert by_id["t4"].assets == ("0514", "0504")
    assert by_id["t5"].kind == "TRADE"
    assert by_id["t5"].adds == ()
    assert by_id["t5"].drops == ()
    assert calls[-1][1]["TRANS_TYPE"] == "*"


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


def test_unauthenticated_reads_do_not_make_provider_requests():
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
    client = MFLClient(_config(username=None, password=None), session=transport)
    with pytest.raises(MFLApiError, match="Sign in through the website"):
        client.export("league")
    assert transport.gets == [] and transport.posts == []


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


def test_final_add_drop_validation_rejects_changed_mfl_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    preview = AddDropPreview(
        mode="fcfs",
        add=MFLPlayer("100", "Add Me"),
        drop=MFLPlayer("200", "Drop Me"),
        league_id="12345",
        franchise_id="0007",
    )
    monkeypatch.setattr(client, "free_agents", lambda: {})
    monkeypatch.setattr(client, "roster_ids", lambda: {"200"})
    with pytest.raises(ValueError, match="no longer a free agent"):
        client.validate_add_drop(preview)

    monkeypatch.setattr(client, "free_agents", lambda: {"100": MFLAvailability("100")})
    monkeypatch.setattr(client, "roster_ids", lambda: set())
    with pytest.raises(ValueError, match="no longer on your roster"):
        client.validate_add_drop(preview)


def test_blind_bid_requires_bid(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    players = {"100": MFLPlayer("100", "Add"), "200": MFLPlayer("200", "Drop")}
    monkeypatch.setattr(client, "players", lambda refresh=False: players)
    monkeypatch.setattr(
        client, "free_agents", lambda: {"100": MFLAvailability("100")}
    )
    monkeypatch.setattr(client, "roster_ids", lambda: {"200"})

    with pytest.raises(ValueError, match="non-negative FAAB bid"):
        client.preview_add_drop(add="100", drop="200", mode="blind-bid")


def test_waiver_preview_preserves_replace_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    players = {"100": MFLPlayer("100", "Add"), "200": MFLPlayer("200", "Drop")}
    monkeypatch.setattr(client, "players", lambda refresh=False: players)
    monkeypatch.setattr(client, "free_agents", lambda: {"100": MFLAvailability("100", status="waiver")})
    monkeypatch.setattr(client, "roster_ids", lambda: {"200"})
    preview = client.preview_add_drop(
        add="100", drop="200", mode="waiver", round_number=2, replace_existing=True
    )
    assert preview.round == 2
    assert preview.replace_existing is True
    recorded = {}
    monkeypatch.setattr(client, "import_request", lambda kind, **values: recorded.update(kind=kind, **values) or {})
    client.submit_add_drop(preview, replace=preview.replace_existing)
    assert recorded["kind"] == "waiverRequest"
    assert recorded["REPLACE"] == "1"


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
    assert free_agents["200"].waiver_claimable is True
    assert free_agents["200"].label == "Waiver claim only"


def test_free_agents_preserve_absolute_cant_add_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    monkeypatch.setattr(
        client,
        "export",
        lambda request_type, **kwargs: {
            "freeAgents": {"player": {"id": "200", "locked": "1", "cant_add": "1"}}
        },
    )
    player = client.free_agents()["200"]
    assert player.claimable is False
    assert player.waiver_claimable is False
    assert player.label == "Unavailable"


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


def test_player_scores_parse_ytd_average_and_week_periods(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    calls = []

    def export(request_type: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((request_type, kwargs))
        return {"playerScores": {"playerScore": [
            {"id": "100", "score": "31.25"},
            {"id": "200", "score": "unavailable"},
        ]}}

    monkeypatch.setattr(client, "export", export)
    assert client.player_scores(period="YTD", player_ids=["200", "100"]) == {"100": 31.25}
    assert client.player_scores(period=2) == {"100": 31.25}
    assert calls == [
        ("playerScores", {"W": "YTD", "PLAYERS": "100,200"}),
        ("playerScores", {"W": "2", "PLAYERS": None}),
    ]
    with pytest.raises(ValueError, match="week 1-18"):
        client.player_scores(period="week")


def test_points_allowed_parses_league_scored_position_totals(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MFLClient(_config(user_cookie="cookie"))
    monkeypatch.setattr(client, "export", lambda request_type, **kwargs: {
        "pointsAllowed": {"team": [
            {"id": "DET", "position": [
                {"name": "QB", "points": "34.2"},
                {"name": "WR+TE", "points": "74.8"},
            ]},
            {"id": "KCC", "position": {"name": "RB", "points": "9.7"}},
        ]}
    })
    assert client.points_allowed() == {
        "DET": {"QB": 34.2, "WR+TE": 74.8},
        "KCC": {"RB": 9.7},
    }


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

    with pytest.raises(ValueError, match="cannot be added immediately"):
        client.preview_add_drop(add="100", drop="200")

    preview = client.preview_add_drop(
        add="100", drop="200", mode="waiver", round_number=1,
    )
    assert preview.mode == "waiver"
    client.validate_add_drop(preview)


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
