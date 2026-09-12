from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Literal
from urllib.parse import urlsplit, urlunsplit

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class MFLApiError(RuntimeError):
    """A rejected or malformed response from MyFantasyLeague."""


@dataclass(frozen=True)
class MFLConfig:
    year: int
    league_id: str
    franchise_id: str
    username: str | None = None
    password: str | None = None
    user_cookie: str | None = None
    api_key: str | None = None
    base_url: str = "https://api.myfantasyleague.com"

    @classmethod
    def from_env(cls) -> "MFLConfig":
        load_dotenv()
        year = int(os.getenv("MFL_YEAR", "2026"))
        league_id = os.getenv("MFL_LEAGUE_ID", "").strip()
        franchise_id = os.getenv("MFL_FRANCHISE_ID", "").strip()
        if not league_id:
            raise ValueError("MFL_LEAGUE_ID is required in .env or the environment")
        if not franchise_id:
            raise ValueError("MFL_FRANCHISE_ID is required in .env or the environment")
        return cls(
            year=year,
            league_id=league_id,
            franchise_id=franchise_id,
            username=os.getenv("MFL_USERNAME") or None,
            password=os.getenv("MFL_PASSWORD") or None,
            user_cookie=os.getenv("MFL_USER_ID") or None,
            api_key=os.getenv("MFL_API_KEY") or None,
            base_url=os.getenv("MFL_BASE_URL", "https://api.myfantasyleague.com").rstrip("/"),
        )


@dataclass(frozen=True)
class MFLPlayer:
    id: str
    name: str
    position: str = ""
    team: str = ""
    espn_id: str = ""
    jersey: str = ""
    college: str = ""
    height: str = ""
    weight: str = ""
    draft_year: str = ""

    @property
    def photo_url(self) -> str:
        return f"https://a.espncdn.com/i/headshots/nfl/players/full/{self.espn_id}.png" if self.espn_id.isdecimal() else ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MFLPlayer":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", data.get("id", ""))),
            position=str(data.get("position", "")),
            team=str(data.get("team", "")),
            espn_id=str(data.get("espn_id") or ""),
            jersey=str(data.get("jersey") or ""),
            college=str(data.get("college") or ""),
            height=str(data.get("height") or ""),
            weight=str(data.get("weight") or ""),
            draft_year=str(data.get("draft_year") or ""),
        )


@dataclass(frozen=True)
class MFLAvailability:
    player_id: str
    status: str = "available"
    locked: bool = False

    @property
    def claimable(self) -> bool:
        return not self.locked

    @property
    def label(self) -> str:
        if self.locked:
            return "Locked"
        if self.status.casefold() in {"waiver", "waivers"}:
            return "Waivers"
        return "Free agent"


@dataclass(frozen=True)
class MFLLineupRule:
    name: str
    minimum: int
    maximum: int


@dataclass(frozen=True)
class MFLLineupSettings:
    starter_count: int
    rules: tuple[MFLLineupRule, ...]


@dataclass(frozen=True)
class MFLInjury:
    player_id: str
    status: str
    details: str = ""


@dataclass(frozen=True)
class MFLLivePlayer:
    player_id: str
    score: float
    status: str
    game_seconds_remaining: int


@dataclass(frozen=True)
class MFLLiveFranchise:
    franchise_id: str
    score: float
    is_home: bool
    players_yet_to_play: int
    players_currently_playing: int
    game_seconds_remaining: int
    players: tuple[MFLLivePlayer, ...]


@dataclass(frozen=True)
class MFLLiveMatchup:
    franchises: tuple[MFLLiveFranchise, ...]


@dataclass(frozen=True)
class MFLLiveScoring:
    week: int
    matchups: tuple[MFLLiveMatchup, ...]


@dataclass(frozen=True)
class MFLLeague:
    id: str
    franchise_id: str
    name: str
    url: str = ""


@dataclass(frozen=True)
class MFLFranchise:
    id: str
    name: str
    division_id: str = ""
    logo_url: str = ""


@dataclass(frozen=True)
class MFLLeagueDetails:
    divisions: tuple[tuple[str, str], ...]
    franchises: dict[str, MFLFranchise]


@dataclass(frozen=True)
class AddDropPreview:
    mode: str
    add: MFLPlayer
    drop: MFLPlayer
    league_id: str
    franchise_id: str
    bid: int | None = None
    round: int | None = None
    replace_existing: bool = False


def _iter_key(value: Any, wanted: str) -> Iterator[Any]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == wanted:
                if isinstance(child, list):
                    yield from child
                else:
                    yield child
            yield from _iter_key(child, wanted)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_key(child, wanted)


def _error_message(value: Any) -> str | None:
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("$t") or error)
        if error:
            return str(error)
        for child in value.values():
            message = _error_message(child)
            if message:
                return message
    elif isinstance(value, list):
        for child in value:
            message = _error_message(child)
            if message:
                return message
    return None


class MFLClient:
    """Small authenticated client for owner-level MFL roster operations."""

    def __init__(self, config: MFLConfig, *, session: requests.Session | None = None) -> None:
        self.config = config
        self.session = session or self._make_session()
        self._players: dict[str, MFLPlayer] | None = None
        self._league_details: MFLLeagueDetails | None = None
        if config.user_cookie:
            self._set_cookie(config.user_cookie)

    @staticmethod
    def _make_session() -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {"User-Agent": "WeeklyProjectionsML/0.2 (+personal MFL client)"}
        )
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.75,
            # Do not retry 429. Retrying a provider throttle immediately consumes
            # more of the same rate limit and hides the actionable response.
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    @property
    def year_url(self) -> str:
        return f"{self.config.base_url}/{self.config.year}"

    def _set_cookie(self, value: str) -> None:
        self.session.cookies.set("MFL_USER_ID", value, domain=".myfantasyleague.com", path="/")

    def user_cookie(self) -> str:
        cookie = self.session.cookies.get("MFL_USER_ID")
        if not cookie:
            raise MFLApiError("No authenticated MFL session is available")
        return str(cookie)

    def login(self) -> None:
        if self.session.cookies.get("MFL_USER_ID") or self.config.api_key:
            return
        if not self.config.username or not self.config.password:
            raise MFLApiError(
                "Set MFL_USERNAME and MFL_PASSWORD, or provide MFL_USER_ID, before using MFL"
            )
        try:
            response = self.session.post(
                f"{self.year_url}/login",
                data={
                    "USERNAME": self.config.username,
                    "PASSWORD": self.config.password,
                    "XML": "1",
                },
                timeout=(10, 30),
            )
            response.raise_for_status()
        except requests.RequestException as error:
            raise MFLApiError(f"MFL login failed: {error}") from error
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as error:
            raise MFLApiError("MFL login returned an unreadable response") from error
        if root.tag.lower() == "error":
            raise MFLApiError(root.text or "MFL rejected the login")
        status = root if root.tag.lower() == "status" else root.find(".//status")
        cookie = status.attrib.get("MFL_USER_ID") if status is not None else None
        if not cookie:
            raise MFLApiError("MFL login did not return an MFL_USER_ID cookie")
        self._set_cookie(cookie)

    def _decode(self, response: requests.Response) -> Any:
        try:
            response.raise_for_status()
        except requests.RequestException as error:
            if response.status_code == 429:
                retry_after = str(response.headers.get("Retry-After") or "").strip()
                wait = f" Wait {retry_after} seconds before refreshing." if retry_after.isdecimal() else " Wait a few minutes before refreshing."
                raise MFLApiError(
                    "MFL's request limit was reached (HTTP 429)." + wait
                ) from error
            raise MFLApiError(f"MFL request failed: {error}") from error
        try:
            payload = response.json()
        except requests.exceptions.JSONDecodeError:
            try:
                root = ET.fromstring(response.text)
            except ET.ParseError as error:
                raise MFLApiError("MFL returned neither JSON nor XML") from error
            if root.tag.lower() == "error":
                raise MFLApiError(root.text or "MFL rejected the request")
            return {root.tag: {**root.attrib, "$t": root.text or ""}}
        message = _error_message(payload)
        if message:
            raise MFLApiError(message)
        return payload

    def export(self, request_type: str, **parameters: Any) -> Any:
        global_feed = request_type in {"players", "nflSchedule", "injuries"}
        if not global_feed:
            self.login()
        params = {
            "TYPE": request_type,
            "L": self.config.league_id,
            "JSON": "1",
            **{key: value for key, value in parameters.items() if value is not None},
        }
        if global_feed:
            # League IDs cause MFL to route global feeds to a league server.
            params.pop("L", None)
        elif self.config.api_key:
            params["APIKEY"] = self.config.api_key
        url = f"https://api.myfantasyleague.com/{self.config.year}" if global_feed else self.year_url
        try:
            response = self.session.get(
                f"{url}/export", params=params, timeout=(10, 60)
            )
        except requests.RequestException as error:
            raise MFLApiError(f"Could not reach MFL: {error}") from error
        return self._decode(response)

    def account_leagues(self) -> list[MFLLeague]:
        """Return every league/franchise pair visible to the signed-in owner."""
        self.login()
        try:
            response = self.session.get(
                f"{self.year_url}/export",
                params={"TYPE": "myleagues", "JSON": "1", **({"APIKEY": self.config.api_key} if self.config.api_key else {})},
                timeout=(10, 60),
            )
        except requests.RequestException as error:
            raise MFLApiError(f"Could not load MFL leagues: {error}") from error
        payload = self._decode(response)
        leagues: list[MFLLeague] = []
        for item in _iter_key(payload, "league"):
            if not isinstance(item, dict) or not item.get("league_id"):
                continue
            leagues.append(
                MFLLeague(
                    id=str(item["league_id"]),
                    franchise_id=str(item.get("franchise_id", "")),
                    name=str(item.get("name") or f"League {item['league_id']}"),
                    url=str(item.get("url", "")),
                )
            )
        return sorted(leagues, key=lambda league: league.name.casefold())

    def import_request(self, request_type: str, **parameters: Any) -> Any:
        """POST one MFL write request. Calling this method changes MFL state."""
        self.login()
        data = {
            "TYPE": request_type,
            "L": self.config.league_id,
            "JSON": "1",
            **{key: value for key, value in parameters.items() if value is not None},
        }
        if self.config.api_key:
            data["APIKEY"] = self.config.api_key
        try:
            # POSTs are deliberately not retried: a timeout must not risk
            # submitting the same roster transaction twice.
            response = self.session.post(
                f"{self.year_url}/import",
                data=data,
                timeout=(10, 60),
                allow_redirects=True,
            )
        except requests.RequestException as error:
            raise MFLApiError(
                "MFL transaction status is uncertain after a network error; "
                "check MFL's Transactions report before trying again"
            ) from error
        return self._decode(response)

    def players(self, *, refresh: bool = False) -> dict[str, MFLPlayer]:
        if self._players is None or refresh:
            payload = self.export("players", DETAILS=1)
            players = [
                MFLPlayer.from_dict(item)
                for item in _iter_key(payload, "player")
                if isinstance(item, dict) and item.get("id")
            ]
            self._players = {player.id: player for player in players}
        return self._players

    def roster_ids(self) -> set[str]:
        payload = self.export("rosters", FRANCHISE=self.config.franchise_id)
        return {
            str(item["id"])
            for item in _iter_key(payload, "player")
            if isinstance(item, dict) and item.get("id")
        }

    def trade_rosters(self) -> dict[str, set[str]]:
        """Current players by franchise, never inferred from scoring lineups."""
        payload = self._roster_export()
        return self._parse_trade_rosters(payload)

    def _roster_export(self, franchise: str | None = None) -> Any:
        params = {"FRANCHISE": franchise} if franchise else {}
        if franchise:
            # Owner-visible public roster endpoint is enough for browsing, and
            # does not depend on the lifetime of the MFL login cookie.
            try:
                response = requests.get(f"https://api.myfantasyleague.com/{self.config.year}/export",
                    params={"TYPE": "rosters", "L": self.config.league_id, "JSON": "1", **params}, timeout=(5, 15))
                payload = self._decode(response)
                if franchise in self._parse_trade_rosters(payload):
                    return payload
            except (requests.RequestException, MFLApiError):
                pass  # Private feeds still require the normal owner session.
        try:
            return self.export("rosters", **params)
        except MFLApiError:
            # This export is public where the league permits it. A separate,
            # bounded read avoids a failed authenticated session/redirect path.
            # Never forward login cookies and never use this path for writes.
            try:
                response = requests.get(f"https://api.myfantasyleague.com/{self.config.year}/export",
                    params={"TYPE": "rosters", "L": self.config.league_id, "JSON": "1", **params}, timeout=(5, 15))
                return self._decode(response)
            except (requests.RequestException, MFLApiError) as error:
                raise MFLApiError("MFL could not load this roster. Retry, or reconnect if your MFL session expired.") from error

    def franchise_roster(self, franchise: str) -> set[str]:
        """Fetch exactly the selected team's current roster, never a live lineup."""
        franchise = str(franchise).zfill(4)
        if not franchise.isdecimal() or franchise == "0000":
            raise ValueError("Choose a team in this league.")
        rosters = self._parse_trade_rosters(self._roster_export(franchise))
        if franchise not in rosters:
            raise MFLApiError("MFL did not return the selected team's roster. Retry loading that team.")
        return rosters[franchise]

    @staticmethod
    def _parse_trade_rosters(payload: Any) -> dict[str, set[str]]:
        rosters = {}
        for franchise in _iter_key(payload, "franchise"):
            if not isinstance(franchise, dict) or not str(franchise.get("id", "")).isdecimal():
                continue
            rosters[str(franchise["id"]).zfill(4)] = {
                str(player["id"]) for player in _iter_key(franchise, "player")
                if isinstance(player, dict) and str(player.get("id", "")).isdecimal()
            }
        if not rosters:
            raise MFLApiError("MFL did not return league rosters")
        return rosters

    def nfl_refresh_state(self, *, week: int, now: float) -> dict:
        """Auto-refresh only with positive NFL game-clock evidence, not weekdays.

        Includes all NFL games, even games with no starters in this matchup.
        A future kickoff is a one-shot wake-up, not continuous score polling.
        """
        payload = self.export("nflSchedule", W=week)
        active = False
        future = []
        for game in _iter_key(payload, "matchup"):
            if not isinstance(game, dict):
                continue
            try:
                kickoff = int(game.get("kickoff", 0))
                remaining = int(game.get("gameSecondsRemaining", -1))
            except (TypeError, ValueError):
                continue
            if kickoff > now:
                future.append(kickoff)
            elif kickoff > 0 and 0 < remaining < 3600:
                active = True
        return {"active": active, "next_kickoff": min(future) if future else None}

    def trade_block(self) -> dict[str, dict]:
        """Read the complete block, retaining draft picks and blind-bid assets."""
        payload = self.export("tradeBait", INCLUDE_DRAFT_PICKS=1)
        if not isinstance(payload, dict) or "tradeBaits" not in payload:
            raise MFLApiError("MFL did not return a complete trade block")
        root = payload["tradeBaits"]
        if root in (None, ""):
            return {}
        if not isinstance(root, dict):
            raise MFLApiError("MFL returned an unreadable trade block")
        result = {}
        for entry in _iter_key(root, "tradeBait"):
            if not isinstance(entry, dict) or not str(entry.get("franchise_id", "")).isdecimal():
                raise MFLApiError("MFL returned an unreadable trade-block entry")
            assets, wanted = entry.get("willGiveUp", ""), entry.get("inExchangeFor", "")
            if isinstance(assets, dict):
                assets = assets.get("$t", "")
            if isinstance(wanted, dict):
                wanted = wanted.get("$t", "")
            if not isinstance(assets, str) or not isinstance(wanted, str):
                raise MFLApiError("MFL returned unreadable trade-block assets")
            fid = str(entry["franchise_id"]).zfill(4)
            if fid in result:
                raise MFLApiError("MFL returned duplicate trade-block entries")
            result[fid] = {"assets": tuple(sorted(set(p.strip() for p in assets.split(",") if p.strip()))),
                           "wanted": wanted, "timestamp": str(entry.get("timestamp", ""))}
        return result

    def add_to_trade_block(self, player_ids: Iterable[str], expected: dict, wanted: str) -> Any:
        """Additive UI over MFL's replace API. Never overwrite a stale preview."""
        own = self.config.franchise_id.zfill(4)
        ids = set(player_ids)
        if not ids or not all(p.isdecimal() for p in ids) or len(wanted) > 256:
            raise ValueError("Choose rostered players and keep your request under 257 characters.")
        if not ids <= self.trade_rosters().get(own, set()):
            raise ValueError("A selected player is no longer on your roster. Rebuild this preview.")
        current = self.trade_block().get(own, {"assets": (), "wanted": "", "timestamp": ""})
        if current != expected:
            raise ValueError("Your MFL trade block changed. Reload it and review a fresh preview.")
        return self.import_request("tradeBait", WILL_GIVE_UP=",".join(sorted(set(current["assets"]) | ids)),
                                   IN_EXCHANGE_FOR=wanted)

    def league_standings(self) -> list[dict]:
        payload = self.export("leagueStandings", ALL=1)
        if not isinstance(payload, dict) or "leagueStandings" not in payload:
            raise MFLApiError("MFL standings are unavailable")
        rows = []
        for item in _iter_key(payload["leagueStandings"], "franchise"):
            if not isinstance(item, dict) or not str(item.get("id", "")).isdecimal():
                continue
            row = {"id": str(item["id"]).zfill(4)}
            for key in ("h2hw", "h2hl", "h2ht", "pf", "pa", "vp"):
                value = item.get(key, "—")
                row[key] = str(value.get("$t", "—") if isinstance(value, dict) else value)
            rows.append(row)
        return rows

    @staticmethod
    def _mfl_image_url(value: Any) -> str:
        """Allow only HTTPS artwork served by MFL; reject manager-supplied hosts."""
        if not isinstance(value, str) or not value.strip() or len(value) > 2048:
            return ""
        try:
            parsed = urlsplit(value.strip())
            hostname = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError:
            return ""
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or port not in {None, 80, 443}
                or not hostname.endswith(".myfantasyleague.com")):
            return ""
        return urlunsplit(("https", parsed.netloc.split("@")[-1], parsed.path, parsed.query, ""))

    def league_details(self) -> MFLLeagueDetails:
        if self._league_details is not None:
            return self._league_details
        payload = self.export("league")
        root = payload.get("league") if isinstance(payload, dict) else None
        if not isinstance(root, dict):
            raise MFLApiError("MFL league details are unavailable")
        raw_divisions = root.get("divisions", {})
        divisions: list[tuple[str, str]] = []
        if isinstance(raw_divisions, dict):
            for item in _iter_key(raw_divisions, "division"):
                if not isinstance(item, dict) or not str(item.get("id", "")):
                    continue
                division_id = str(item["id"])
                divisions.append((division_id, str(item.get("name") or f"Division {division_id}")))
        franchises: dict[str, MFLFranchise] = {}
        raw_franchises = root.get("franchises", {})
        if isinstance(raw_franchises, dict):
            for item in _iter_key(raw_franchises, "franchise"):
                if not isinstance(item, dict) or not str(item.get("id", "")).isdecimal():
                    continue
                franchise_id = str(item["id"]).zfill(4)
                logo = self._mfl_image_url(item.get("logo")) or self._mfl_image_url(item.get("icon"))
                franchises[franchise_id] = MFLFranchise(
                    franchise_id,
                    str(item.get("name") or f"Franchise {franchise_id}"),
                    str(item.get("division") or ""),
                    logo,
                )
        self._league_details = MFLLeagueDetails(tuple(divisions), franchises)
        return self._league_details

    def validate_player_trade(self, target: str, give: Iterable[str], receive: Iterable[str]) -> tuple[str, list[str], list[str]]:
        own = self.config.franchise_id.zfill(4)
        target = str(target).zfill(4)
        give, receive = sorted(set(give)), sorted(set(receive))
        if not own.isdecimal() or own == "0000" or not target.isdecimal() or target in {own, "0000"}:
            raise ValueError("Choose another team in this league.")
        if not give or not receive:
            raise ValueError("Choose at least one player from each team.")
        if len(give) > 100 or len(receive) > 100 or not all(p.isdecimal() for p in give + receive):
            raise ValueError("Only rostered players can be included in this trade.")
        if set(give) & set(receive):
            raise ValueError("The same player cannot appear on both sides of this offer.")
        rosters = self.trade_rosters()
        if own not in rosters or target not in rosters:
            raise ValueError("Both teams must have a roster in this league.")
        if not set(give) <= rosters[own] or not set(receive) <= rosters[target]:
            raise ValueError("A selected player is no longer on that team. Rebuild the offer using the current rosters.")
        return target, give, receive

    def propose_player_trade(self, *, target: str, give: Iterable[str], receive: Iterable[str], comments: str = "") -> Any:
        if len(comments) > 500:
            raise ValueError("Keep your trade message to 500 characters or fewer.")
        target, give, receive = self.validate_player_trade(target, give, receive)
        return self.import_request(
            "tradeProposal", OFFEREDTO=target, WILL_GIVE_UP=",".join(give),
            WILL_RECEIVE=",".join(receive), COMMENTS=comments,
            # Also binds commissioner-owned sessions to their selected team;
            # never offer as commissioner or perform a forced trade.
            FRANCHISE_ID=self.config.franchise_id.zfill(4),
        )

    def lineup_settings(self) -> MFLLineupSettings:
        payload = self.export("league")
        starters = next(
            (item for item in _iter_key(payload, "starters") if isinstance(item, dict)),
            None,
        )
        if not starters:
            raise MFLApiError("MFL did not return this league's starting-lineup rules")
        try:
            starter_count = int(starters.get("count", 0))
        except (TypeError, ValueError) as error:
            raise MFLApiError("MFL returned an invalid starter count") from error
        rules: list[MFLLineupRule] = []
        for item in _iter_key(starters, "position"):
            if not isinstance(item, dict) or not item.get("name"):
                continue
            raw_limit = str(item.get("limit") or "0").strip()
            try:
                if "-" in raw_limit:
                    low, high = raw_limit.split("-", 1)
                    minimum, maximum = int(low), int(high)
                else:
                    minimum = maximum = int(raw_limit)
            except ValueError:
                continue
            rules.append(
                MFLLineupRule(
                    name=str(item["name"]),
                    minimum=minimum,
                    maximum=maximum,
                )
            )
        if starter_count < 1:
            raise MFLApiError("This league does not have a weekly starter count")
        return MFLLineupSettings(starter_count=starter_count, rules=tuple(rules))

    def player_roster_statuses(
        self, player_ids: Iterable[str], *, week: int | None = None
    ) -> dict[str, str]:
        ids = sorted({str(player_id) for player_id in player_ids})
        if not ids:
            return {}
        payload = self.export(
            "playerRosterStatus",
            P=",".join(ids),
            W=week,
            F=self.config.franchise_id,
        )
        statuses: dict[str, str] = {}
        for item in _iter_key(payload, "player"):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            player_id = str(item["id"])
            franchises = [*_iter_key(item, "roster_franchise"), *_iter_key(item, "franchise")]
            for franchise in franchises:
                if not isinstance(franchise, dict):
                    continue
                franchise_id = str(franchise.get("id") or franchise.get("franchise_id") or "")
                if franchise_id.lstrip("0") == self.config.franchise_id.lstrip("0"):
                    value = str(franchise.get("status") or "R").upper()
                    statuses[player_id] = {"STARTER":"S", "NONSTARTER":"NS", "NON-STARTER":"NS"}.get(value, value)
                    break
        unknown = {player_id for player_id in ids if statuses.get(player_id, "R") == "R"}
        if unknown and week is not None:
            # R means either no lineup or no visibility. Never assume it means bench.
            live = self.live_scoring(week=week)
            for matchup in live.matchups:
                for franchise in matchup.franchises:
                    if franchise.franchise_id.lstrip("0") != self.config.franchise_id.lstrip("0"):
                        continue
                    for player in franchise.players:
                        if player.player_id in unknown:
                            status = {"starter":"S", "nonstarter":"NS", "non-starter":"NS", "bench":"NS", "ir":"IR", "ts":"TS"}.get(player.status.lower())
                            if status:
                                statuses[player.player_id] = status
        return statuses

    def scoring_rules(self) -> dict:
        payload = self.export("rules")
        return payload.get("rules", {}) if isinstance(payload, dict) else {}

    def injuries(self, *, week: int | None = None) -> dict[str, MFLInjury]:
        payload = self.export("injuries", W=week)
        injuries: dict[str, MFLInjury] = {}
        for item in _iter_key(payload, "injury"):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            player_id = str(item["id"])
            injuries[player_id] = MFLInjury(
                player_id=player_id,
                status=str(item.get("status") or "").strip(),
                details=str(item.get("details") or "").strip(),
            )
        return injuries

    def nfl_team_kickoffs(self, *, week: int) -> dict[str, int]:
        """Return each MFL NFL team code's scheduled kickoff as a Unix timestamp."""
        payload = self.export("nflSchedule", W=week)
        kickoffs: dict[str, int] = {}
        self.week_games = {}
        for matchup in _iter_key(payload, "matchup"):
            if not isinstance(matchup, dict):
                continue
            try:
                kickoff = int(matchup.get("kickoff", 0))
            except (TypeError, ValueError):
                continue
            teams = [team for team in _iter_key(matchup, "team") if isinstance(team, dict)]
            for team in teams:
                if isinstance(team, dict) and team.get("id") and kickoff:
                    kickoffs[str(team["id"]).upper()] = kickoff
                    opponent = next((other.get("id", "") for other in teams if other.get("id") != team["id"]), "")
                    self.week_games[str(team["id"]).upper()] = {
                        "opponent": ("vs " if str(team.get("isHome")) == "1" else "@ ") + opponent,
                        "kickoff": kickoff,
                        "final": str(matchup.get("gameSecondsRemaining")) == "0",
                    }
        return kickoffs

    def franchise_names(self) -> dict[str, str]:
        return {franchise_id: team.name
                for franchise_id, team in self.league_details().franchises.items()}

    def live_scoring(self, *, week: int) -> MFLLiveScoring:
        payload = self.export("liveScoring", W=week, DETAILS=1)
        live = payload.get("liveScoring", {}) if isinstance(payload, dict) else {}
        raw_matchups = live.get("matchup", []) if isinstance(live, dict) else []
        if isinstance(raw_matchups, dict):
            raw_matchups = [raw_matchups]
        matchups: list[MFLLiveMatchup] = []
        for raw_matchup in raw_matchups:
            if not isinstance(raw_matchup, dict):
                continue
            raw_franchises = raw_matchup.get("franchise", [])
            if isinstance(raw_franchises, dict):
                raw_franchises = [raw_franchises]
            franchises: list[MFLLiveFranchise] = []
            for raw_franchise in raw_franchises:
                if not isinstance(raw_franchise, dict) or not raw_franchise.get("id"):
                    continue
                raw_players = raw_franchise.get("players", {}).get("player", [])
                if isinstance(raw_players, dict):
                    raw_players = [raw_players]
                players: list[MFLLivePlayer] = []
                for raw_player in raw_players:
                    if not isinstance(raw_player, dict) or not raw_player.get("id"):
                        continue
                    try:
                        score = float(raw_player.get("score") or 0)
                    except (TypeError, ValueError):
                        score = 0.0
                    try:
                        remaining = int(raw_player.get("gameSecondsRemaining") or 0)
                    except (TypeError, ValueError):
                        remaining = 0
                    players.append(
                        MFLLivePlayer(
                            player_id=str(raw_player["id"]),
                            score=score,
                            status=str(raw_player.get("status") or ""),
                            game_seconds_remaining=remaining,
                        )
                    )
                try:
                    score = float(raw_franchise.get("score") or 0)
                except (TypeError, ValueError):
                    score = 0.0
                franchises.append(
                    MFLLiveFranchise(
                        franchise_id=str(raw_franchise["id"]),
                        score=score,
                        is_home=str(raw_franchise.get("isHome") or "0") == "1",
                        players_yet_to_play=int(raw_franchise.get("playersYetToPlay") or 0),
                        players_currently_playing=int(
                            raw_franchise.get("playersCurrentlyPlaying") or 0
                        ),
                        game_seconds_remaining=int(
                            raw_franchise.get("gameSecondsRemaining") or 0
                        ),
                        players=tuple(players),
                    )
                )
            if franchises:
                matchups.append(MFLLiveMatchup(franchises=tuple(franchises)))
        try:
            response_week = int(live.get("week") or week)
        except (TypeError, ValueError):
            response_week = week
        return MFLLiveScoring(week=response_week, matchups=tuple(matchups))

    def submit_lineup(
        self,
        *,
        week: int,
        starter_ids: Iterable[str],
        comments: str = "Submitted by Weekly Projections",
        as_commissioner: bool = False,
    ) -> Any:
        starters = sorted({str(player_id) for player_id in starter_ids})
        if not starters:
            raise ValueError("A starting lineup cannot be empty")
        return self.import_request(
            "lineup",
            W=week,
            STARTERS=",".join(starters),
            COMMENTS=comments,
            FRANCHISE_ID=self.config.franchise_id if as_commissioner else None,
        )

    def free_agent_ids(self) -> set[str]:
        return set(self.free_agents())

    def free_agents(self) -> dict[str, MFLAvailability]:
        """Return the full league free-agent pool, including MFL lock state."""
        payload = self.export("freeAgents")
        available: dict[str, MFLAvailability] = {}
        for item in _iter_key(payload, "player"):
            if isinstance(item, dict) and item.get("id"):
                player_id = str(item["id"])
                status = str(item.get("status") or "available").strip().casefold()
                locked_flag = str(item.get("locked") or item.get("cant_add") or "").casefold()
                locked = status == "locked" or locked_flag in {"1", "true", "yes", "locked"}
                available[player_id] = MFLAvailability(
                    player_id=player_id,
                    status=status,
                    locked=locked,
                )
            elif isinstance(item, (str, int)):
                player_id = str(item)
                available[player_id] = MFLAvailability(player_id=player_id)
        return available

    def projected_scores(
        self,
        *,
        week: int | None = None,
        player_ids: Iterable[str] | None = None,
        free_agents_only: bool = False,
    ) -> dict[str, float]:
        """Return MFL's projections calculated with this league's scoring rules."""
        players = ",".join(str(player_id) for player_id in player_ids or []) or None
        payload = self.export(
            "projectedScores",
            W=week,
            PLAYERS=players,
            STATUS="freeagent" if free_agents_only else None,
        )
        scores: dict[str, float] = {}
        for item in _iter_key(payload, "playerScore"):
            if not isinstance(item, dict):
                continue
            player_id = item.get("id") or item.get("player_id")
            raw_score = item.get("score") or item.get("points")
            if not player_id or raw_score in (None, ""):
                continue
            try:
                scores[str(player_id)] = float(raw_score)
            except (TypeError, ValueError):
                continue
        return scores

    def current_week(self) -> int | None:
        """Return MFL's current NFL week when the status feed is available."""
        try:
            response = self.session.get(
                f"{self.config.base_url}/fflnetdynamic{self.config.year}/mfl_status.json",
                timeout=(10, 30),
            )
            payload = self._decode(response)
        except (MFLApiError, requests.RequestException):
            return None
        weeks = payload.get("mfl_status", {}).get("weeks", {}) if isinstance(payload, dict) else {}
        raw_week = (
            weeks.get("CurrentWeek")
            or weeks.get("LiveScoringWeek")
            or weeks.get("LineupWeek")
            or weeks.get("UpcomingWeek")
        )
        try:
            return max(1, min(18, int(raw_week)))
        except (TypeError, ValueError):
            return None

    def find_players(self, query: str, *, available_only: bool = False) -> list[MFLPlayer]:
        needle = query.casefold().strip()
        matches = [player for player in self.players().values() if needle in player.name.casefold()]
        if available_only:
            available = self.free_agent_ids()
            matches = [player for player in matches if player.id in available]
        return sorted(matches, key=lambda player: (player.name.casefold(), player.id))

    def resolve_player(self, selector: str) -> MFLPlayer:
        catalog = self.players()
        if selector in catalog:
            return catalog[selector]
        exact = [player for player in catalog.values() if player.name.casefold() == selector.casefold()]
        if len(exact) == 1:
            return exact[0]
        partial = self.find_players(selector)
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise ValueError(f"No MFL player matched {selector!r}")
        choices = ", ".join(f"{player.name} ({player.id})" for player in partial[:10])
        raise ValueError(f"Player name is ambiguous; use an ID: {choices}")

    def preview_add_drop(
        self,
        *,
        add: str,
        drop: str,
        mode: Literal["fcfs", "waiver", "blind-bid"] = "fcfs",
        bid: int | None = None,
        round_number: int | None = None,
        replace_existing: bool = False,
    ) -> AddDropPreview:
        add_player = self.resolve_player(add)
        drop_player = self.resolve_player(drop)
        if add_player.id == drop_player.id:
            raise ValueError("The add and drop players must be different")
        free_agents = self.free_agents()
        availability = free_agents.get(add_player.id)
        if not availability:
            raise ValueError(f"{add_player.name} is not listed as a free agent in this league")
        if not availability.claimable:
            raise ValueError(
                f"{add_player.name} is currently locked by MFL; keep the recommendation and submit when the league reopens"
            )
        if drop_player.id not in self.roster_ids():
            raise ValueError(f"{drop_player.name} is not on franchise {self.config.franchise_id}")
        if mode == "blind-bid" and (bid is None or bid < 0):
            raise ValueError("Blind-bid waivers require a non-negative FAAB bid")
        if mode == "waiver" and (round_number is None or round_number < 1):
            raise ValueError("Priority waivers require a positive claim round")
        if replace_existing and mode == "fcfs":
            raise ValueError("Immediate FCFS moves cannot replace waiver claims")
        return AddDropPreview(
            mode=mode,
            add=add_player,
            drop=drop_player,
            league_id=self.config.league_id,
            franchise_id=self.config.franchise_id,
            bid=bid,
            round=round_number,
            replace_existing=replace_existing,
        )

    def submit_add_drop(
        self,
        preview: AddDropPreview,
        *,
        replace: bool = False,
        as_commissioner: bool = False,
    ) -> Any:
        impersonate = preview.franchise_id if as_commissioner else None
        if preview.mode == "fcfs":
            return self.import_request(
                "fcfsWaiver",
                ADD=preview.add.id,
                DROP=preview.drop.id,
                FRANCHISE_ID=impersonate,
            )
        if preview.mode == "waiver":
            return self.import_request(
                "waiverRequest",
                ROUND=preview.round,
                PICKS=f"{preview.add.id}_{preview.drop.id}",
                REPLACE="1" if replace else None,
                FRANCHISE_ID=impersonate,
            )
        if preview.mode == "blind-bid":
            return self.import_request(
                "blindBidWaiverRequest",
                ROUND=preview.round,
                PICKS=f"{preview.add.id}_{preview.bid}_{preview.drop.id}",
                REPLACE="1" if replace else None,
                FRANCHISE_ID=impersonate,
            )
        raise ValueError(f"Unsupported transaction mode: {preview.mode}")

    def named_players(self, player_ids: Iterable[str]) -> list[MFLPlayer]:
        catalog = self.players()
        return [catalog.get(player_id, MFLPlayer(id=player_id, name=player_id)) for player_id in player_ids]
