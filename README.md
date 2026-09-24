# MFL Fantasy Football Assistant

A private web app for managing MyFantasyLeague teams. It supports multiple
leagues, lineup changes, automatic lineup suggestions, add/drop reviews, live
matchups, league standings, trades, the trade block, and league-scored weekly
projections. League HQ adds a transaction feed, waiver trends and FAAB balances,
power rankings, an expected-wins luck index, playoff outlooks, score-driven weekly
recaps, current-season manager profiles, and a private session-only prop tracker.
League HQ also shows the most recent completed MFL matchups. Player views include
MFL YTD, average, a bounded recent-game median, and league-scored opponent strength.

Every connected league also has an operations layer: **My Transactions** combines
local review/submit receipts with authoritative MFL activity; a private session
watchlist feeds a three-player comparison screen; Notifications gathers lineup,
injury, transaction, and uncertain-write alerts; and the League area includes the
full MFL schedule and a readable rules/scoring reference. A Data Status page shows
which reports are cached and permits a CSRF-protected refresh of read-only league
data without replaying any transaction. New browsers receive a dismissible quick
start tour, with the same guide always available from Settings. On first sign-in,
the browser asks whether player lists should lead with ESPN PPR consensus, ESPN
Standard consensus, MFL league-scored projections, or a Combined positional rank.
Combined converts MFL points, ESPN PPR order, and StatHead ML points into separate
position ranks and averages the available ranks equally; it requires at least two
covered sources and never averages incompatible point totals. The non-secret
preference is remembered on that device and remains editable from Settings.

Player detail cards include the selected week's MFL projection and actual score,
up to six weekly MFL point totals, YTD and season averages, a recent average and
high, official overall and position ranks, the actual saved-list projection rank,
and a labeled recent-versus-prior trend. History reads reuse the same bounded weekly
caches as the player market; missing weeks stay visibly unavailable.

Appearance includes six device-local themes: Maize & Blue, the light Detroit
Lions palette, Midnight Aurora, Detroit Tigers navy-and-orange, Detroit Red Wings
red-and-white, and Detroit Pistons blue-and-red. Every theme uses the same responsive
layout and accessible state labels.

The Rosters tab shows every member's official MFL roster in expandable team cards.
Its League Leaders board ranks all non-IDP scorers by official MFL YTD and position
rank, identifies their fantasy team or free-agent status, and keeps the selected
weekly projection rank separate. Free Agents and Trades remain grouped beside it as roster tools. The player market
combines MFL free agents, waiver players, and every league
roster. It can filter by name, position, NFL team, availability, fantasy team,
and projection coverage, with multiple sort choices. Team defenses remain in
the market while individual defensive players are intentionally hidden. Only
claimable free agents can enter the add/drop builder; rostered players are
clearly labeled for trade research. FCFS, priority-waiver, and blind-bid FAAB
moves retain round, bid, append/replace, review, and kickoff-lock safeguards.

## Run locally

Python 3.11 or newer is required.

```powershell
cd C:\WeeklyProjectionsML
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn weekly_projections.web.app:app --app-dir src --host 127.0.0.1 --port 8765 --no-access-log
```

Open `http://127.0.0.1:8765`. Sign into MFL inside the app. If “Stay signed in”
is checked, the app remembers only an encrypted MFL session for up to 30 days.
The password is discarded immediately after MFL sign-in. Local encryption keys
live outside the repository in your user profile.

## Deploy on Railway

Connect a private GitHub repository to Railway and use:

Build command:

```text
pip install -r requirements.txt
```

Start command:

```text
python -m uvicorn weekly_projections.web.app:app --app-dir src --host 0.0.0.0 --port $PORT --workers 1 --no-access-log
```

Add a Railway volume mounted at `/data`, then configure:

```text
WP_SECURE_COOKIES=1
WP_SESSION_DB=/data/sessions.sqlite3
WP_SESSION_SECRET=<your generated Fernet key>
```

## Supabase PostgreSQL on Azure App Service

The application can use the free Supabase PostgreSQL project instead of the
SQLite remembered-session file. The database must contain schema migrations 1–4 in
the private `fantasy_hq` schema. When `WP_DATABASE_URL` is absent, local and
existing Railway deployments continue to use SQLite without any behavior change.

In Supabase, open **Connect**, choose the **Session pooler** connection string,
and keep TLS enabled. The session pooler is preferable to the direct endpoint for
Azure App Service because it supports IPv4. Store the complete connection string
only in Azure App Service **Environment variables**:

```text
WP_DATABASE_URL=postgresql://.../postgres?sslmode=require
WP_SESSION_SECRET=<the existing stable Fernet key>
WP_SECURE_COOKIES=1
WP_ALLOWED_HOSTS=<app-name>.azurewebsites.net
```

`WP_DATABASE_URL` is a secret: never put it in GitHub, `.env`, diagnostics, or a
browser-visible setting. Keep `WP_SESSION_SECRET` stable so remembered MFL session
ciphertext remains decryptable. `WP_SESSION_DB` is ignored when
`WP_DATABASE_URL` is configured. Restart the App Service after saving variables.

The first PostgreSQL phase stores only:

- a one-way owner fingerprint and connected-league metadata;
- cross-device theme, ranking source, default league, selected week, and onboarding choices;
- an opaque remember-token digest and authenticated encrypted MFL session.

MFL passwords, CSRF state, pending lineup/add-drop/trade drafts, side bets, API
keys, request bodies, and raw browser tokens are never stored. Existing SQLite
remember tokens are not copied to Supabase; users sign in once after the switch.
The application validates the installed schema version before using PostgreSQL
and falls back to normal signed-in operation if optional persistence is temporarily
unavailable.

Apply [`supabase/migrations/002_mfl_cross_device_preferences.sql`](supabase/migrations/002_mfl_cross_device_preferences.sql)
in the Supabase SQL Editor before deploying code that requires migration 2. MFL
login is the account identity: a normalized login is converted to a one-way
application fingerprint, so the same MFL user receives the same preferences on
every device without storing the MFL username or password.

Apply [`supabase/migrations/003_mfl_historical_archive.sql`](supabase/migrations/003_mfl_historical_archive.sql)
to add the private historical archive. After migration 3 is installed, sign in,
open **Data status → Historical archive**, and import one season or all seasons
linked by MFL. The import reads only MFL league metadata, final standings, and
weekly matchup results. Each season is replaced transactionally, so rerunning an
import refreshes corrected MFL data without duplicating rows. The archive tables
stay in the private `fantasy_hq` schema, have RLS enabled as defense in depth, and
grant no browser Data API access to `anon` or `authenticated` roles.

MFL includes the active season in its linked-season count, so a report of 11
seasons from 2016–2026 means there are 10 completed seasons (2016–2025) to archive;
2026 remains the live current season. If MFL rate-limits a batch, wait for the
cooldown and run the all-seasons import again. It resumes with a missing season
instead of repeatedly refreshing one already stored.

Apply [`supabase/migrations/004_per_league_themes.sql`](supabase/migrations/004_per_league_themes.sql)
to add private per-league theme overrides. In the theme control, choose **All
leagues** to keep one account theme or **This league only** to let each league use
its own theme. The overrides follow the same MFL login across devices and remain
server-only; the browser never receives database credentials.

Database connection failures are written to standard output as the structured
event `database_status_unavailable`. In Azure, enable **Monitoring → App Service
logs → Application logging (File System)**, then open **Monitoring → Log stream**.
Open the website's **Data status** page once and search the stream for that event
or the reference displayed on the database card. The record identifies store
initialization versus the schema health check and includes a redacted exception
chain; it never includes the connection URL, database password, or session data.

Free weekly consensus rankings are loaded from ESPN Fantasy's unauthenticated
read feed. CBS Sports' public PPR projections are converted to within-position
ranks. FantasyPros uses its full Half-PPR ranking pages rather than the limited
prototype API response. The optional settings belong in Azure App Service
**Environment variables** (or a private local environment):

```text
WP_ESPN_RANKING_FORMAT=PPR
WP_FANTASYPROS_SESSION_COOKIE=<the Cookie request-header value from your signed-in FantasyPros session>
```

`WP_ESPN_RANKING_FORMAT` accepts `PPR` or `STANDARD` and supplies the default only
until the user makes a device-level ranking choice. The normal default is `PPR`.
Rankings are cached for 12 hours, are shown as a separate reference, and fail
softly if ESPN changes or temporarily disables the feed. ESPN access never uses
your ESPN account, cookies, or credentials. This does not add ESPN league login
or management; MFL remains the application's only league provider. The player
market defaults to ESPN's weekly consensus order, with MFL league projections,
season performance, matchup strength, and recommendation order still available.

`WP_FANTASYPROS_SESSION_COOKIE` is optional and is sent only from the server to
FantasyPros. Treat it like a password: mark the Azure setting as deployment-slot
specific, never paste it into Git or chat, and rotate/remove it after signing out
or if it is exposed. The application never logs or returns this value. FantasyPros
and CBS results are cached for 12 hours and fail softly if a source is unavailable.

The Players page also includes a free **Defense streaming** panel comparing your
defense with up to six claimable targets. An explainable 0–100 fit score uses
ESPN Mike Clay's [2026 unit talent grades](https://g.espncdn.com/s/ffldraftkit/26/NFLDK2026_CS_ClayProjections2026.pdf)
(September 9, page 63): 50% defensive talent and 50% opposing offense weakness.
When existing MFL DEF points-allowed matchup ranks are present, weights become
45/45/10. These are dated analyst talent ranks, not live offensive results,
injury-adjusted ratings, fantasy points, or probabilities. MFL projected points
remain separate and league-scored. Talent cards require no paid key or extra
runtime provider calls. Waiver targets also show advisory whole-unit FAAB bids
using the owner's MFL balance and the median single-defense BBID awards in the
last 14 days (bounded to 200 transactions, sharing League HQ's 90-second cache).
Fit adjusts the median and a conservative 10%-of-remaining-budget ceiling applies.
Without history, an explicitly labeled 2%-of-remaining-budget heuristic is used;
missing balances produce no bid. Recent defense prices are shown alongside the
estimate. Suggestions are not winning-bid predictions or pending-claim reservations.
The opt-in button only fills the blind-bid draft; normal review remains required.
Missing ranks/schedules and games already started receive no fit
score; the 2026 snapshot is never reused for another season. Selecting a target
uses the normal add/drop builder and still requires review and confirmation.

League HQ includes authenticated MFL message-board summaries and league chat.
Threads load on demand, board/chat summaries use short session caches, and private
chat rows are shown only to the sender or recipient. New threads, replies, public
chat, and direct chat messages use a review/confirm step with CSRF validation.
Writes are sent exactly once and uncertain chat/message results cannot be retried
from the same draft. Content is escaped in the browser; credentials never enter
message URLs, markup, logs, or persistent storage.

Live scoring has separate **Matchup** and **All scores** views. The league-wide
scoreboard reuses the already-loaded MFL live-scoring response and follows the
same NFL-game-aware refresh gate, so it does not add per-matchup polling.

The signed-in home page now opens with a personalized weekly briefing. The full
Roster Intelligence page combines the saved MFL lineup and official MFL injury
designations with current nflverse depth-chart roles and Open-Meteo stadium
forecasts. External reference feeds have bounded timeouts and caches and fail
independently; an unavailable depth chart or forecast never hides the MFL lineup.
MFL player-news links remain the source of full news stories because MFL does not
publish those articles through its API.

The plain-language projection tracker evaluates at most the previous three completed weeks. MFL
league-scored projections and position-scaled StatHead ML projections are measured
against official MFL player scores by position. The resulting inverse-error weights
and heuristic 80% ranges are displayed only as a labeled reference and never replace
MFL totals, recommendations, or submitted lineups. Its scoreboard names the current
points leader, explains average miss in plain language, and keeps RMSE/bias in an
expandable advanced section. ESPN weekly ranks are evaluated separately as a top-half
ranking hit rate because ranks are not point projections.

The site is installable as a PWA on supported browsers. Its service worker caches
only the public offline shell and static icon assets; authenticated league HTML,
MFL responses, credentials, CSRF state, and transactions are never cached offline.
The Settings menu includes installation guidance, device-local alert preferences,
and app-badge support. Device alerts are evaluated after an authenticated refresh;
there is no background push server and no push service receives MFL credentials.
The installed app exposes a direct Team Score shortcut where the host platform
supports manifest shortcuts. A true iPhone Home Screen widget is not a web/PWA
feature; that would require a separately shipped native iOS app and WidgetKit
extension. The signed-in Home screen remains the private, one-tap score surface.

The multi-week roster planner reads the current MFL roster and lineup rules, then
uses one cached nflverse schedule download to show upcoming byes, likely open
starter slots, Weeks 15–17 opponents, available-player upgrades, and depth-chart
RB handcuffs. Missing or partial schedules never create a fake bye. Future-week
point projections are not fabricated; the displayed projection remains the current
MFL league-scored value and every suggestion is advisory.

The Players page also builds a conditional waiver queue from positive projected
roster upgrades. It orders up to six claims, excludes locked drop players, compares
recent same-position winning bids, and caps the combined suggested FAAB at the
franchise's displayed remaining balance. Each row only fills the existing add/drop
builder: the user still chooses the league's current waiver mode and reviews and
confirms every MFL request separately. Existing claims are never silently replaced.

Depth-chart data is provided by
[nflverse](https://github.com/nflverse/nflverse-data) and is subject to its source
attribution and licensing terms. Forecast data is provided by
[Open-Meteo](https://open-meteo.com/). Both are clearly separated from authoritative
MFL scoring and roster state in the interface.

Generate the secret once with:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put that output only in Railway Variables and keep it stable across deployments.
Keep one replica, generate a domain under Networking, and set the health check to
`/health`. Railway's generated domain is allowed automatically; for a custom domain,
set `WP_ALLOWED_HOSTS` to that hostname (comma-separate multiple hostnames). Never
commit `.env`, the key, or MFL login details.

## Behavior and safety

- The last selected league is remembered locally and, when Supabase is configured,
  as a non-secret account preference. Themes can be shared across all leagues or
  saved per league and restored on other devices for the same MFL login.
- Active sessions last eight hours; an opted-in encrypted session can restore a
  fresh active session after restart for up to 30 days.
- Remembered sessions never contain the MFL password, API key, CSRF token, caches,
  pending lineup/add-drop/trade actions, or side bets.
- Web API-key login is disabled because MFL requires the key in GET URLs; password
  login exchanges the password for an MFL session cookie and discards the password.
- Lineup and transaction changes always have a review step before submission.
- Ownership, free-agent availability, administrative locks, and kickoff locks are
  checked again before player-move submissions.
- Live scoring automatically polls only while an NFL game is in progress.
- Current-week scores refresh every 30 seconds only while an NFL game clock is
  active. Between games, the last score is retained until the next kickoff; after
  the final game, it is retained until the next daily refresh. Historical weeks
  are treated as final and cached for 30 days.
- MFL scores remain authoritative when detailed stat feeds differ.
- ESPN weekly consensus ranks and StatHead ML projections are separately labeled
  references; neither silently replaces or changes MFL's league-scored projection.
- Opponent strength comes from MFL's league-scored points-allowed report. A player's
  recent median uses at most the five latest completed weeks, cached for seven days.
- Incomplete rosters keep visible open rows for every unfilled legal starter slot.
- MFL display reads are shared within a signed-in session, temporarily fall back to
  recent data during provider trouble, and stop retrying during an MFL 429 cooldown.
- Standings and completed fantasy-schedule results refresh on the first view after
  midnight Eastern, then use an account-scoped in-process cache for the day. Set
  `WP_TIME_ZONE` to another IANA time-zone name only if midnight should mean a
  different league time zone. Home loads its two visible cards first and defers
  lower widgets until they approach the viewport.
- League scoring rules are refreshed at most once every seven days because they are
  season configuration, while live scores use a separate short-lived snapshot.
- The playoff bracket is projected locally: three division leaders receive seeds
  1–3, then the best remaining records fill seeds 4–8.
- Side bets are notes only: they have no payment handling and clear with the session.
- Detailed diagnostic records rotate under `logs/errors.log` and are also sent
  to standard output for Railway Deploy Logs. They include the complete
  exception chain, stack frames, HTTP status, canonical client IP, safe endpoint
  details, and rate-limit headers. On Railway, only Railway's `X-Real-IP` header
  is trusted; local/direct requests use the connection address. Credentials,
  cookies, authorization values, and request bodies are redacted or excluded.
- Every non-static request also writes a structured `access_request` record to
  stdout so Railway Deploy Logs show the client IP, safe route template, method,
  status, and error reference. Access records never appear in the website and do
  not contain raw URLs, query values, cookies, or request bodies.

## Tests

Install the development extras and run the website test suite:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```
