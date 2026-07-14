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

**Important:** the OAuth login flow redirects your browser back to
`127.0.0.1`, so this only works when you run it directly on your own
machine — not over SSH port forwarding or in a remote/cloud sandbox where
`127.0.0.1` doesn't reach your browser.

### 6. Log in to X and run your first sync

1. With the server running, open http://127.0.0.1:8000/auth/login in your
   browser and approve access.
2. You'll land on `/api/auth/status`, which should show
   `"logged_in": true` and your username.
3. Trigger a sync:
   ```bash
   curl -X POST http://127.0.0.1:8000/api/sync
   ```
   This paginates through all your bookmarks, expands any self-authored
   threads, and upserts everything into the local SQLite database. The
   response reports how many bookmarks were new vs. already-known, plus
   how many X API reads the sync used (bookmarks are billed per read —
   see the cost note in step 2 above).
4. Re-running the sync is safe — it's idempotent by tweet id and won't
   create duplicates or re-bill for unchanged bookmarks reads beyond
   what pagination requires.

**Known limitation:** thread expansion walks *backward* from a bookmarked
reply to reconstruct the thread up to that point, using the standard tweet
lookup endpoint. It can't walk *forward* to later replies the author
posted after the bookmarked tweet — that requires the recent-search
endpoint, which sits behind a more restricted X API access tier and isn't
used here to avoid scraping-adjacent workarounds.

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
- [x] Phase 2: Database schema (SQLite + FTS5)
- [x] Phase 3: X API sync engine (OAuth PKCE, pagination, thread expansion)
- [ ] Phase 4: Enrichment pipeline (transcripts, article summaries, auto-tagging)
- [ ] Phase 5: Local web UI (search, tag filters, watch-later queue)
- [ ] Phase 6: Polish (error handling, logging, optional scheduled sync)
