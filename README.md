# MFL Fantasy Football Assistant

A private web app for managing MyFantasyLeague teams. It supports multiple
leagues, lineup changes, automatic lineup suggestions, add/drop reviews, live
matchups, league standings, trades, the trade block, and league-scored weekly
projections.

## Run locally

Python 3.11 or newer is required.

```powershell
cd C:\WeeklyProjectionsML
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn weekly_projections.web.app:app --app-dir src --host 127.0.0.1 --port 8765 --no-access-log
```

Open `http://127.0.0.1:8765`. Sign into MFL inside the app. Login information
is held only in server memory and is cleared when the server restarts.

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

Add the Railway variable `WP_SECURE_COOKIES=1`, keep one replica, and generate
a domain under the service's Networking settings. Never commit `.env` or place
MFL login details in the repository.

## Behavior and safety

- The last selected league and theme are remembered in device-local cookies.
- MFL sessions are memory-only; a restart or redeployment requires a new login.
- Lineup and transaction changes always have a review step before submission.
- Kickoff locks are checked before lineup or player-move submissions.
- Live scoring automatically polls only while an NFL game is in progress.
- MFL scores remain authoritative when detailed stat feeds differ.
- Detailed diagnostic records rotate under `logs/errors.log` and are also sent
  to standard output for Railway Deploy Logs. They include the complete
  exception chain, stack frames, HTTP status, safe endpoint details, and rate
  limit headers. Credentials, cookies, authorization values, and request bodies
  are redacted or excluded.

## Tests

Install the development extras and run the website test suite:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```
