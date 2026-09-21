# AGENTS.md — MFL Fantasy Football Assistant

This file is the operating guide for coding agents working in this repository.
Follow it before changing application behavior, provider calls, transactions,
authentication, logging, or deployment.

## 1. Product scope

This repository contains a private, mobile-friendly web application for managing
MyFantasyLeague (MFL) fantasy-football teams. It supports multiple MFL leagues
for one signed-in owner and provides:

- a league home dashboard;
- saved lineups, drag/touch start-sit editing, and an automatic best-lineup suggestion;
- a complete player market with free agents, waivers, locked players, and every roster;
- a league player-leaders board with official YTD/position ranks, ownership, and the selected projection rank;
- reviewed FCFS, priority-waiver, and blind-bid FAAB add/drop requests;
- live and historical matchup scoring with starter-only summaries and point details;
- standings grouped by MFL division when division data exists;
- a League HQ with transaction activity, waiver trends/FAAB, power and luck rankings,
  playoff outlooks, automated score recaps, manager profiles, and session-only props;
- trade offers, target analysis, suggested trades, and trade-block management;
- session-only player watchlists, side-by-side player comparison, actionable alerts,
  and combined local/MFL transaction receipts;
- player cards with bounded weekly MFL point history and honest scoring trends;
- a device-local, user-editable primary ranking preference for ESPN PPR, ESPN
  Standard, MFL league-scored projections, or a combined positional consensus;
- a full MFL league schedule, readable rules/scoring pages, cache status, and
  first-use guidance;
- MFL league-scored projections and clearly separated third-party reference projections;
- Maize & Blue, Detroit Lions, Midnight Aurora, and Detroit Tigers themes across desktop and iPhone layouts.

Provider scope is **MFL only**. Do not add ESPN, Sleeper, Yahoo, or another fantasy
provider unless a future user request explicitly reopens that work.

This is not the removed offline WR training/backtest project. Do not recreate the
old `data`, `features`, `models`, `evaluation`, `scrapers`, `outputs`, or training
script trees unless the user explicitly requests a new offline modeling project.

## 2. Actual technology stack

- Python 3.11+
- FastAPI and Uvicorn
- Server-rendered Jinja templates
- Vanilla JavaScript
- Plain CSS with shared theme overrides
- Requests/urllib3 for MFL and projection-source HTTP calls
- NumPy/SciPy for lineup optimization and probability calculations
- Pytest with FastAPI TestClient
- Railway deployment from GitHub

There is no Node application, React/Next.js stack, database, background worker,
or Vercel deployment in this repository.

## 3. Repository map

```text
src/weekly_projections/
  config.py                    project path configuration
  lineup.py                    legal lineup slots and recommendation solver
  live_stats.py                NFL stat-line and scoring-component helpers
  projection_sources.py        MFL projections plus separate reference feed
  recommendations.py           waiver/player-market recommendation ranking
  trade_engine.py              bounded trade analysis and suggestion engine
  league_intelligence.py       power/luck, waiver trends, recaps, and playoff odds
  mfl/client.py                all MFL reads, writes, parsing, and data classes
  web/app.py                   FastAPI routes, sessions, page composition, validation
  web/diagnostics.py           redacted local and Railway error logging
  web/templates/               Jinja pages and reusable partials
  web/static/                  JavaScript and CSS/theme assets
tests/                         website, client, solver, scoring, trade, and UI tests
requirements.txt               production web dependencies
pyproject.toml                 package metadata and development dependencies
README.md                      operator-facing setup and Railway notes
```

Keep provider transport/parsing in `mfl/client.py`, domain calculations in their
dedicated modules, request orchestration in `web/app.py`, markup in templates,
and browser behavior in static JavaScript. Avoid moving all logic into route functions.

## 4. Local setup and commands

Run from `C:\WeeklyProjectionsML` in PowerShell.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Start the website:

```powershell
python -m uvicorn weekly_projections.web.app:app --app-dir src --host 127.0.0.1 --port 8765 --no-access-log
```

Open `http://127.0.0.1:8765`.

Run all tests and validate browser JavaScript:

```powershell
.\.venv\Scripts\python.exe -m pytest
node --check src\weekly_projections\web\static\app.js
```

Run `git diff --check` before committing. Generated caches, `.env`, and `logs/`
must remain untracked.

## 5. Authentication and session invariants

- Users sign into MFL inside this web app. Never ask them to provide credentials in chat.
- Passwords and API keys must never be placed in URLs, HTML, browser storage, Git,
  diagnostic output, environment files, or persistent session records.
- Active authentication state is held in the server-side `BrowserSession` object.
- A user-approved remembered login may persist only the MFL session cookie inside
  authenticated encryption. SQLite stores ciphertext plus a hash of an opaque token;
  the encryption key must remain separate in `WP_SESSION_SECRET` or the local user
  profile key directory.
- The browser receives only opaque `wp_session`/`wp_remember` identifiers and a CSRF token.
- Active sessions expire after eight hours. Remembered sessions expire after at most
  30 days, create fresh CSRF state after restart, and are revoked on disconnect.
- Web API-key login is intentionally unsupported because MFL read authentication places
  API keys in GET URLs.
- Every state-changing browser request requires CSRF validation.
- Device-local cookies may remember only non-secret preferences such as league ID,
  season, selected week, theme, and primary ranking choice.
- Pending lineup, add/drop, trade, trade-block, and side-bet state remains memory-only
  and must never be restored with a remembered login.
- Watchlists, notification state, and local transaction receipts are also memory-only;
  they clear on disconnect, expiration, restart, or redeploy.

Do not expand the encrypted persistence schema without a new explicit security review.

## 6. MFL request rules

- Use `https://api.myfantasyleague.com` for global feeds such as players, injuries,
  and the NFL schedule. Do not attach a league ID to global feeds.
- League-specific reads and all writes retain the selected year and league ID.
- Treat player IDs and league/franchise IDs as strings. Normalize franchise IDs to
  four digits only where MFL franchise matching requires it.
- Never fabricate missing MFL data. Show an honest unavailable/partial state.
- MFL scores and MFL league rules are authoritative for this product.
- POST transaction requests are never automatically retried. A timeout can leave a
  write uncertain, so direct the user to MFL's transaction report before retrying.
- HTTP 429 is not automatically retried. Display a clear wait message and preserve
  safe rate-limit details in diagnostics.
- Safe GET requests may retry transient 5xx/connect/read failures with bounded backoff.
- Prefer one broader MFL read over multiple overlapping reads when it reduces rate-limit load.

All requests need explicit connect/read timeouts. Never introduce unbounded polling.

## 7. Player-market contract

The Players/Add-Drop page merges:

- claimable free agents;
- waiver players;
- MFL-locked free agents;
- the user's roster;
- every other franchise roster.

It supports client-side filtering by search text, normalized position, NFL team,
availability, fantasy team, and projection coverage. It supports sorting by
recommendation, projected points, roster edge, player name, NFL team, and fantasy team.

Roster ownership wins if MFL feeds temporarily disagree: a player reported on a
roster must not be rendered as addable. Rostered players are research/trade targets
only. Only a player confirmed by the selected league's free-agent feed and not locked
may enter the add/drop builder.

Team defenses (`Def`, `DEF`, `DST`, or `D/ST`) remain visible and normalize to `DEF`
for filtering. Individual defensive players are hidden from this non-IDP market,
including DB, CB, S/SAF, DE/EDGE, DT/NT, DL, LB, ILB, and OLB designations. Do not
remove team defense while filtering IDP records.

The add/drop builder must retain:

- exact add and drop player identities;
- FCFS immediate moves;
- priority waiver claims with claim round;
- blind-bid FAAB claims with bid and optional round;
- append-by-default behavior;
- an explicit, strongly worded option to replace existing claims in the round;
- a separate review page before confirmation;
- a final ownership, availability, and kickoff-lock validation before submission.

Never enable add controls for rostered or locked players.

The Player Leaders page ranks non-IDP players from official MFL YTD league-scored
points, shows both overall and position rank, and identifies the owning franchise or
free-agent state. Player cards repeat those official ranks and separately label the
user's selected weekly projection rank. Never present a projection rank as an official
season scoring rank.

## 8. Lineup contract

- Load the lineup MFL currently saved for the selected week.
- Default to MFL's current scoring week, not the next lineup week.
- Derive legal starters and flex capacity from that league's MFL lineup rules.
- Keep drag/touch movement and the three-dot action alternative usable.
- Auto-start suggestions must return a legal lineup, not simply the highest raw scorers.
- Recheck locks immediately before submitting.
- A player whose NFL game has started cannot move between starter and bench.
- If MFL does not expose a locked player's saved status, stop instead of guessing.
- All edits remain a local draft until the user reviews and confirms them.

## 9. Scoring and projections contract

- MFL totals are authoritative.
- Weekly MFL projections are scored using the selected league's rules.
- Third-party/ML projections are a separately labeled reference. Do not silently
  blend generic scoring into MFL league totals or use it as an unlabeled fallback.
- Combined rankings may average only position ranks from covered MFL, ESPN, and ML
  sources, require at least two sources, and must never average incompatible raw
  point totals. Label the result as a positional consensus, not an official rank.
- Player cards show weekly projected points, actual points when available, and the
  selected league's scoring basis.
- Live point-breakdown cards may explain supported components but must disclose when
  detailed stat feeds cannot exactly reproduce MFL's total.
- Win percentages are estimates, not official MFL probabilities. Missing inputs must
  result in no estimate rather than a fabricated percentage.

## 10. Live-scoring contract

- Users can open their matchup or any other matchup for weeks 1–18.
- Starter rows show player state, projection, actual points, stat line, and a separate
  points-breakdown card.
- Bench rows should maximize same-position pairing across teams; imperfect remaining
  pairs are acceptable and must not imply a shared lineup slot.
- Playing, yet-to-play, and final states use distinct accessible text and colors.
- Automatic score refresh runs only when NFL schedule/game-clock evidence confirms a
  game is in progress.
- Between games, schedule a one-time wake-up around the next kickoff instead of polling.
- Pause automatic requests in hidden tabs and while a scoring detail dialog is open.
- Manual refresh and historical-week browsing remain available at all times.

## 11. Trade and standings contract

- Load other-team rosters from MFL; never infer ownership from a weekly scoring lineup.
- Trade ideas are suggestions only and never send automatically.
- Validate ownership again immediately before proposing a trade.
- Uncertain trade writes are not retried automatically.
- Trade-block updates preserve unrelated existing players, picks, bid money, and notes.
- Require review before changing the trade block and read it back afterward when possible.
- Standings preserve MFL's values and feed order.
- Group standings by MFL divisions when division metadata exists; keep unassigned teams
  in an explicit fallback group.
- Use MFL-hosted franchise logos only after existing URL validation accepts them.
- Rest-of-season trade pace is a labeled extrapolation of the current weekly
  league-scored projection, not a dynasty market value or trained acceptance model.

## 11a. League-intelligence contract

- Build power rankings only from completed schedule weeks. Weight scoring 45%, point
  differential 25%, record 20%, and recent three-week form 10%.
- Expected wins are weekly all-play wins; luck is actual wins minus expected wins.
- Recaps and team one-liners are deterministic score summaries unless a real LLM
  provider is explicitly configured. Never label template text as LLM-generated.
- Transaction reads stay bounded by count and recent days and share a 90-second
  session cache with the rest of League HQ to protect MFL rate limits.
- Playoff probabilities are model estimates derived from power scores, not official
  MFL odds. If MFL has not published the bracket, show an empty state.
- Manager profiles must not invent career, championship, rivalry, draft, trophy, or
  retired-player history. Show only imported seasons and computed facts.
- Side bets are private, session-memory notes with no payment handling. They clear on
  disconnect, expiration, restart, or redeploy and are never posted to MFL.
- An incomplete lineup must retain one visible blank row per unfilled legal starter
  slot in both the editor and matchup presentation.

## 12. UI and accessibility expectations

- Maize & Blue, Detroit Lions, Midnight Aurora, and Detroit Tigers themes must cover every route and reusable card.
- The Lions theme uses a white/light page background with official-inspired blue,
  silver, black, and white accents.
- Midnight Aurora uses a dark navy base with violet and electric-cyan accents while
  preserving readable contrast and the same interaction states as the other themes.
- Detroit Tigers uses a dark navy base with orange accents and warm white text while
  preserving readable contrast and the same interaction states as the other themes.
- Desktop is information-dense; iPhone layouts must remain touch-friendly and readable.
- Maintain visible keyboard focus, semantic labels, real buttons, and accessible dialogs.
- Never make color the only indication of playing/locked/final/error state.
- Keep destructive or externally mutating actions visually distinct and confirmation-gated.
- Preserve useful empty, error, locked, and partial-data states.
- When changing static assets, update the relevant cache-busting query string in templates.
- Avoid unnecessary browser requests. Filtering and sorting already-loaded board rows
  should stay client-side.

## 13. Diagnostics and privacy

`web/diagnostics.py` writes rotating local logs to `logs/errors.log` and also writes
to stdout so Railway captures the same entries in Deploy Logs.

Diagnostics may include:

- the error reference shown to the user;
- route and safe HTTP method;
- exception types and complete exception chains;
- stack filenames, line numbers, function names, and source lines;
- safe provider endpoint/status and rate-limit headers.

Diagnostics must redact or omit:

- usernames and passwords;
- MFL session cookies and API keys;
- authorization/cookie headers;
- request and response bodies;
- form values and query-string secrets;
- Python frame locals.

Every caught operational failure should call `log_error` with a stable event name.
The displayed error reference must remain searchable in the logs.

## 14. Testing expectations

Every behavior change needs focused tests. At minimum, preserve coverage for:

- login/session/CSRF behavior and secret exclusion;
- MFL host selection, parsing, timeouts, rate limits, and non-retried writes;
- free-agent availability, roster ownership, locks, and transaction parameters;
- team-defense inclusion and IDP exclusion;
- legal lineup/flex assignment and locked-player protection;
- live scoring, week selection, refresh gating, and matchup pairing;
- projections and honest missing-data behavior;
- trade validation, trade-block preservation, and uncertain writes;
- standings divisions, logos, all themes, player leaders, and responsive template output;
- complete redacted diagnostic chains.

Use synthetic clients and monkeypatches in tests. Tests must never make real MFL
transactions, require real credentials, or depend on a live league.

Before handing off a change, run:

```powershell
.\.venv\Scripts\python.exe -m pytest
node --check src\weekly_projections\web\static\app.js
git diff --check
```

Warnings already emitted by third-party TestClient compatibility are not test failures,
but new warnings should be investigated.

## 15. Railway deployment

Railway deploys the GitHub `main` branch.

Build command:

```text
pip install -r requirements.txt
```

Start command:

```text
python -m uvicorn weekly_projections.web.app:app --app-dir src --host 0.0.0.0 --port $PORT --workers 1 --no-access-log
```

Required Railway variables:

```text
WP_SECURE_COOKIES=1
WP_SESSION_SECRET=<Fernet key stored only in Railway Variables>
WP_SESSION_DB=/data/sessions.sqlite3
```

Mount a Railway volume at `/data`. Do not manually define `PORT`; Railway provides it.
Keep one worker and one replica because active CSRF and review state remains process
memory. Do not place MFL credentials or an MFL API key in Railway variables.

## 16. Change checklist

Before finalizing work:

1. Confirm the change is MFL-only and within the requested product scope.
2. Preserve read/review/confirm boundaries for every MFL mutation.
3. Revalidate ownership, availability, and locks at submission time.
4. Check that added network calls are bounded and do not worsen rate-limit pressure.
5. Check both themes and desktop/mobile layouts for changed templates or CSS.
6. Add or update focused tests and run the complete validation commands.
7. Confirm `.env`, logs, cookies, keys, and passwords are not tracked or printed.
8. Update `README.md` when operator setup or deployment behavior changes.
9. Do not push, deploy, or send an MFL transaction unless the user explicitly authorizes it.
