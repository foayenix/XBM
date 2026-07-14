# XBM — X Bookmarks Watch/Read Later

A local, single-user tool that pulls your X (Twitter) bookmarks, enriches
them (thread text, article summaries, YouTube transcripts), auto-tags them
with Claude, and gives you a searchable local web UI with a watch-later
queue for video items.

Everything runs on your own machine with your own API keys. There's no
hosting, no accounts, no multi-user auth.

## Status

This repo is being built in phases. Right now: **Phase 1 — project
scaffold**. The server starts and serves a placeholder page; syncing,
enrichment, and search are not wired up yet.

## Requirements

- Python 3.11+
- An X (Twitter) developer app with OAuth 2.0 credentials
- An Anthropic API key

## Setup

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Get your X API credentials

1. Go to the [X Developer Portal](https://developer.x.com/en/portal/dashboard)
   and sign in with the X account whose bookmarks you want to sync.
2. Create a new **Project** and **App** if you don't have one yet.
3. In the app's **User authentication settings**, click "Set up" and
   configure:
   - **App permissions:** Read (bookmarks access requires at least read
     access with the `bookmark.read` scope).
   - **Type of app:** "Native App / Public client" is the right choice for
     a tool that runs entirely on your own machine (no client secret
     needed).
   - **Callback URI / Redirect URL:** `http://127.0.0.1:8000/auth/callback`
     (must match `X_REDIRECT_URI` in your `.env` exactly).
   - **Website URL:** anything valid, e.g. `http://127.0.0.1:8000`.
4. Save, then open the app's **Keys and tokens** tab and copy the
   **OAuth 2.0 Client ID**. That's the only credential you need for a
   public client.
5. Note on cost: X's bookmarks endpoint (`GET /2/users/{id}/bookmarks`) is
   billed per read under X's "owned reads" pricing (roughly $0.001 per
   bookmark read as of mid-2026, pay-per-use — there's no free tier for
   new developer accounts). This app logs roughly how many bookmark reads
   each sync uses so you can keep an eye on cost.

### 3. Get your Anthropic API key

1. Go to the [Anthropic Console](https://console.anthropic.com/settings/keys)
   and create an API key.
2. Claude is used for article summarization and topic tagging. Costs are
   small per bookmark but scale with how many bookmarks you sync.

### 4. Configure your environment

```bash
cp .env.example .env
```

Then edit `.env` and fill in:

- `X_CLIENT_ID` — from step 2 above
- `X_CLIENT_SECRET` — leave blank if you registered a public client (recommended)
- `ANTHROPIC_API_KEY` — from step 3 above

`.env` is gitignored — your keys never get committed.

### 5. Run it

```bash
python run.py
```

Then open http://127.0.0.1:8000 in your browser.

## Project layout

```
backend/    FastAPI app, config, sync + enrichment logic (added in later phases)
frontend/   Static single-page web UI, served by FastAPI
db/         SQLite database file lives here (gitignored)
scripts/    One-off / maintenance scripts
run.py      Starts the whole app with one command
```

## Roadmap

- [x] Phase 1: Project scaffold
- [ ] Phase 2: Database schema (SQLite + FTS5)
- [ ] Phase 3: X API sync engine (OAuth PKCE, pagination, thread expansion)
- [ ] Phase 4: Enrichment pipeline (transcripts, article summaries, auto-tagging)
- [ ] Phase 5: Local web UI (search, tag filters, watch-later queue)
- [ ] Phase 6: Polish (error handling, logging, optional scheduled sync)
