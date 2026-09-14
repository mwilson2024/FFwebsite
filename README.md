# MFL Fantasy Football Assistant

A private web app for managing MyFantasyLeague teams. It supports multiple
leagues, lineup changes, automatic lineup suggestions, add/drop reviews, live
matchups, league standings, trades, the trade block, and league-scored weekly
projections. League HQ adds a transaction feed, waiver trends and FAAB balances,
power rankings, an expected-wins luck index, playoff outlooks, score-driven weekly
recaps, current-season manager profiles, and a private session-only prop tracker.

The player market combines MFL free agents, waiver players, and every league
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

- The last selected league and theme are remembered in device-local cookies.
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
- MFL scores remain authoritative when detailed stat feeds differ.
- Incomplete rosters keep visible open rows for every unfilled legal starter slot.
- MFL display reads are shared within a signed-in session, temporarily fall back to
  recent data during provider trouble, and stop retrying during an MFL 429 cooldown.
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
