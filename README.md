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

### 7. Run enrichment

After a sync, run the enrichment pipeline to fetch YouTube transcripts,
summarize linked articles, and auto-tag every bookmark:

```bash
curl -X POST http://127.0.0.1:8000/api/enrich
```

This is resumable: it only processes linked-content items that aren't yet
`done` and only tags bookmarks that don't have tags yet, so re-running
after a crash or a rate limit picks up where it left off instead of
redoing finished work. It uses Claude Haiku (cheap, fast) and reports how
many summarization/tagging calls it made, since those are billed too.

### 8. Use the web UI

With the server running, http://127.0.0.1:8000 opens **Later**, the
library UI (light and dark mode, toggle in the sidebar footer):

- A **Library** view: search bar over the full-text index (tweet text,
  thread text, linked article summaries/transcripts, and tags), with
  press-`/`-to-search, type filter chips (threads / articles / videos /
  tweets), and every item classified and color-coded by content type
- A tag sidebar — click a tag to filter, click again to clear it
- A **Watch Later** queue for bookmarks with a YouTube link (added
  automatically during enrichment): an "up next" card, the remaining
  queue, and a dimmed watched section with per-item toggles
- An **item detail** view per bookmark: full expanded thread, embedded
  video link + transcript excerpt, or article summary with source link —
  plus editable tags, so you can correct the auto-tagging (click × to
  remove, type in the dashed box and press Enter to add)
- A **sync now** button in the sidebar footer that runs sync + enrichment
  as a background job with live progress: a bookmark count while
  fetching, then a determinate "enriching n / m" bar, then a summary of
  new items and billed API reads. Reloading the page mid-sync (or during
  a scheduled sync) picks the progress display back up.

The UI loads its three fonts from Google Fonts; without internet it
falls back to system fonts and still works fine.

### 9. Logs and unattended scheduled sync

Every run writes to `logs/xbm.log` (rotated at 2 MB, 3 backups kept) as
well as the console, so if X rate-limits you or a token expires, there's
always a clear message waiting for you — not just a crash. That matters
most for the next feature:

By default, sync only runs when you click **sync now** (which starts a
background job via `POST /api/sync/start`; `GET /api/sync/status` reports
its progress) or when you hit the synchronous `/api/sync` endpoint
yourself. If you'd rather it happen automatically, set
`SYNC_INTERVAL_MINUTES` in `.env` to a positive number and restart the
app — it'll run sync + enrichment on that interval in the background,
through the same job machinery, so a scheduled run shows its progress in
the UI and can never overlap a manual one. A failed scheduled run (rate
limit, expired token, network blip) is logged and retried on the next
interval; it never crashes the app.

## Project layout

```
backend/    FastAPI app, config, sync + enrichment logic, scheduler
frontend/   Static single-page web UI, served by FastAPI
db/         SQLite database file lives here (gitignored)
logs/       Rotating log file lives here (gitignored)
scripts/    One-off / maintenance scripts
run.py      Starts the whole app with one command
```

## Roadmap

- [x] Phase 1: Project scaffold
- [x] Phase 2: Database schema (SQLite + FTS5)
- [x] Phase 3: X API sync engine (OAuth PKCE, pagination, thread expansion)
- [x] Phase 4: Enrichment pipeline (transcripts, article summaries, auto-tagging)
- [x] Phase 5: Local web UI (search, tag filters, watch-later queue)
- [x] Phase 6: Polish (error handling, logging, optional scheduled sync)
