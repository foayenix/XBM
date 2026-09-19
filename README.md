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

`requirements.txt` gives minimum versions. For a reproducible install of the
exact versions this was last verified against, use
`pip install -r requirements.lock` instead.

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

While working on the code, set `APP_RELOAD=1` in `.env` to restart the
server on file changes. It's off by default because the reloader restarts
the app — and the background scheduler with it — on every write.

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

#### What gets fetched, and what doesn't

The URLs enrichment downloads come from other people's tweets, so the
fetch is guarded (`backend/fetching.py`):

- **Only public destinations.** A link — or a redirect chain ending — at
  `127.0.0.1`, a private LAN address, or a cloud metadata endpoint like
  `169.254.169.254` is refused. Redirects are followed one hop at a time so
  each destination is re-checked, rather than trusting the first one.
- **Only articles.** A response that isn't HTML is refused instead of being
  pushed through the HTML parser and summarized as prose, and URLs that
  look like file downloads (`.pdf`, `.png`, `.zip`, …) are skipped before a
  request is made.
- **Bounded size.** Reading stops at 5 MB whether or not `Content-Length`
  says so.

YouTube links are matched by hostname, so `music.youtube.com`,
`m.youtube.com` and `youtu.be` all take the transcript path and land in the
watch-later queue.

**Note:** bookmarks you remove on X are not deleted locally. Syncing only
adds and updates, so the local database keeps acting as an archive. Delete
rows by hand if you don't want that.

### 8. Use the web UI

With the server running, http://127.0.0.1:8000 gives you:

- A search bar over the full-text index (tweet text, thread text, linked
  article summaries/transcripts, and tags), 30 results at a time with a
  **Load more** button and a running "showing X of Y" count
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

## UI notes

The interface is a single static page with no build step and no frontend
dependencies — one HTML file, one stylesheet, one script. The only external
request is the webfont pair (IBM Plex Sans + JetBrains Mono); if it fails,
the stacks fall back to system faces and nothing else changes.

It is a dense, keyboard-first list rather than a feed of cards. One line
per bookmark, monospace metadata, and a type badge so you can tell at a
glance what you are about to open:

| badge | meaning |
|-------|---------|
| `THR` | a self-authored thread, expanded during sync |
| `VID` | has a YouTube link, so it is also in the watch-later queue |
| `ART` | has a linked article, summarised during enrichment |
| `TWT` | a plain tweet |

A bookmark that is both a thread and a video shows `VID` — one badge slot,
and the video is the one with a queue attached.

### Keyboard

| key | does |
|-----|------|
| `j` / `k` | move down / up the list |
| `o` | open the focused bookmark on X |
| `w` | toggle the focused bookmark's watched state |
| `/` or `Ctrl`/`Cmd`+`K` | jump to the filter box |
| `Esc` | leave the filter box |

Shortcuts are suppressed while you are typing, so `/` and `j` in a search
term behave like ordinary characters.

- **Responsive.** The rail is a fixed column on wide screens; below 900px
  the shell stacks, navigation becomes a row, the tag list a wrapping chip
  cloud, and each table row becomes three stacked lines. No horizontal
  scrolling at any width down to 320px.
- **Light and dark** are both first-class, driven by
  `prefers-color-scheme` over one set of CSS custom properties. All text
  meets WCAG AA contrast in both.
- **Keyboard and screen reader.** Every control clears a 44x44px hit area
  and shows a 2px accent focus ring. Toggle state lives in `aria-current`
  and `aria-pressed`, not only in a CSS class, and result counts, sync
  progress and errors are announced through `aria-live` regions. No CSS
  `order` anywhere, so the visual sequence and the DOM sequence cannot
  drift apart at any width.
- **Motion** is one shared timing scale (120/160ms), disabled under
  `prefers-reduced-motion`.

`tests/test_ui.py` asserts all of the above against a real browser, so
these do not quietly regress. Run them with:

```bash
pip install -r requirements-dev.txt
playwright install chromium
pytest -m ui
```

They skip automatically if no browser is installed, so `pytest` still
works anywhere.

## Security notes

XBM has no login of its own, so anything that can reach the port can spend
your X and Anthropic budget through `POST /api/sync`. Binding to
`127.0.0.1` alone does not cover that, so two header checks run on every
request (`backend/security.py`):

- **Host** must be a name XBM answers to (`127.0.0.1`, `localhost`, `::1`
  by default; ports are not part of this check). This blocks DNS
  rebinding, where a page on `evil.com` whose DNS points at `127.0.0.1`
  would otherwise become same-origin with this app.
- **Origin**, on state-changing requests, must name the same host and port
  as the request itself. This blocks cross-site requests from other pages
  open in your browser. Requests with no `Origin` at all (curl, scripts)
  are allowed — a browser cannot omit it on a cross-origin POST.

Set `ALLOWED_HOSTS` in `.env` if you deliberately serve XBM under another
name. Note that `localhost` and `127.0.0.1` are different origins, so pick
one spelling and stay on it.

Still worth knowing: your X tokens are stored in plain text in the SQLite
file. It's gitignored and lives under `db/`, which is the bar this project
sets for a local single-user tool — but it is not encryption at rest.

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite runs against a throwaway SQLite file built by the real
`init_db.py`, and stubs the X and Anthropic APIs — it makes no network
calls and costs nothing.

CI (`.github/workflows/ci.yml`) runs it on Python 3.11, 3.12 and 3.13,
separately against the pinned versions in `requirements.lock`, and runs the
browser-level UI checks in a job of their own.

## Roadmap

- [x] Phase 1: Project scaffold
- [x] Phase 2: Database schema (SQLite + FTS5)
- [x] Phase 3: X API sync engine (OAuth PKCE, pagination, thread expansion)
- [x] Phase 4: Enrichment pipeline (transcripts, article summaries, auto-tagging)
- [x] Phase 5: Local web UI (search, tag filters, watch-later queue)
- [x] Phase 6: Polish (error handling, logging, optional scheduled sync)
