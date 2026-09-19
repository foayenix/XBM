# XBM — X Bookmarks Watch/Read Later

A local, single-user tool that pulls your X (Twitter) bookmarks, enriches
them (thread text, article summaries, YouTube transcripts), auto-tags them
with Claude, and gives you a searchable local web UI with a watch-later
queue for video items.

Everything runs on your own machine with your own API keys. There's no
hosting, no accounts, no multi-user auth.

## Status

All six build phases are complete (see the roadmap at the bottom): sync,
enrichment, search, the web UI, and optional scheduled sync all work.

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
   create duplicates. Bookmarks you've already synced cost only the
   pagination reads needed to walk past them; thread expansion is *not*
   repeated, because a tweet's reply chain never changes, so already-known
   threads cost nothing on a re-sync. The sync summary reports
   `threads_reused_from_cache` so you can see that happening.

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

Work that can never succeed is given up on rather than retried forever. A
link that fails (404, paywall, deleted video) is retried up to 3 times;
after that it's left alone and counted in `linked_content_gave_up`. Tagging
works the same way, via `bookmarks_tagging_gave_up`. Without those caps, one
dead link would be re-fetched and re-billed on every future run. To force a
retry after fixing whatever was wrong:

```bash
sqlite3 db/xbm.sqlite3 "UPDATE linked_content SET attempts = 0 WHERE status = 'failed'"
sqlite3 db/xbm.sqlite3 "UPDATE bookmarks SET tag_attempts = 0"
```

### 8. Use the web UI

With the server running, http://127.0.0.1:8000 gives you:

- A search bar over the full-text index (tweet text, thread text, linked
  article summaries/transcripts, and tags)
- A tag sidebar — click a tag to filter, click again to clear it
- A **Watch later** view listing every bookmark with a YouTube link
  (added automatically during enrichment), with unwatched/watched/all
  filters and a toggle per item
- A **Sync now** button that runs a full sync followed by enrichment and
  reports progress and final counts

### 9. Logs and unattended scheduled sync

Every run writes to `logs/xbm.log` (rotated at 2 MB, 3 backups kept) as
well as the console, so if X rate-limits you or a token expires, there's
always a clear message waiting for you — not just a crash. That matters
most for the next feature:

By default, sync only runs when you click **Sync now** or hit
`/api/sync` yourself. If you'd rather it happen automatically, set
`SYNC_INTERVAL_MINUTES` in `.env` to a positive number and restart the
app — it'll run sync + enrichment on that interval in the background. A
failed scheduled run (rate limit, expired token, network blip) is logged
and retried on the next interval; it never crashes the app.

Only one sync or enrichment runs at a time. If a scheduled run is already
in flight, **Sync now** (and `POST /api/sync`) returns **409 Conflict**
instead of starting a second one, and a scheduled run that lands during a
manual sync logs a line and waits for its next interval. This matters for
more than tidiness: two concurrent runs meant two writers on one SQLite
file, and two simultaneous OAuth token refreshes — X rotates refresh
tokens, so the loser's token would be silently invalidated and you'd be
logged out. `GET /api/jobs/status` reports what's running, if anything.

## Project layout

```
backend/    FastAPI app, config, sync + enrichment logic, scheduler
frontend/   Static single-page web UI, served by FastAPI
db/         Schema + the SQLite database file (the .sqlite3 is gitignored)
logs/       Rotating log file lives here (gitignored)
scripts/    One-off / maintenance scripts, including DB init + migrations
tests/      pytest suite
run.py      Starts the whole app with one command
```

## Database migrations

`scripts/init_db.py` runs automatically on startup and is safe to re-run.
It applies `db/schema.sql` (everything in it is `IF NOT EXISTS`) and then
any outstanding migrations, tracked with SQLite's `user_version` pragma.

Schema changes need a migration: `CREATE TABLE IF NOT EXISTS` does nothing
on a database that already has the table, so a column added to
`schema.sql` alone would never reach an existing install. Add the column to
both `schema.sql` (for new installs) and a numbered migration in
`scripts/init_db.py` (for existing ones). Migrations run *before*
`schema.sql`, so statements in `schema.sql` may reference migrated columns.

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite runs against a throwaway SQLite file built by the real
`init_db.py`, and stubs the X and Anthropic APIs — it makes no network
calls and costs nothing.

## Roadmap

- [x] Phase 1: Project scaffold
- [x] Phase 2: Database schema (SQLite + FTS5)
- [x] Phase 3: X API sync engine (OAuth PKCE, pagination, thread expansion)
- [x] Phase 4: Enrichment pipeline (transcripts, article summaries, auto-tagging)
- [x] Phase 5: Local web UI (search, tag filters, watch-later queue)
- [x] Phase 6: Polish (error handling, logging, optional scheduled sync)
