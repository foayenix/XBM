"""FastAPI app entrypoint. Serves the local web UI and API routes."""
import logging
import sqlite3
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend import config, db, jobs, queries, scheduler, security, x_auth
from backend.enrich import EnrichmentError, run_enrichment
from backend.jobs import JobBusyError
from backend.sync import sync_bookmarks
from backend.x_auth import XAuthError
from backend.x_client import XApiError

config.LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(config.LOG_DIR / "xbm.log", maxBytes=2_000_000, backupCount=3),
    ],
)
logger = logging.getLogger("xbm.main")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="XBM - X Bookmarks Watch/Read Later", lifespan=lifespan)

# Registered before anything else so every route, including the static
# mount, is covered.
app.middleware("http")(security.guard_requests)

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "database_path": str(config.DATABASE_PATH),
        "anthropic_key_configured": bool(config.ANTHROPIC_API_KEY),
        "x_client_configured": bool(config.X_CLIENT_ID),
    }


@app.get("/auth/login")
def auth_login():
    try:
        return RedirectResponse(x_auth.build_authorize_url())
    except XAuthError as e:
        logger.warning("Login failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/auth/callback")
def auth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    # X sends ?error=access_denied (and no code) when the user clicks Cancel
    # on the consent screen. Declaring code/state as required would make that
    # an unreadable 422 validation dump.
    if error:
        detail = f"X declined the login: {error}"
        if error_description:
            detail += f" ({error_description})"
        logger.warning("Login callback returned an error: %s", detail)
        raise HTTPException(status_code=400, detail=detail)
    if not code or not state:
        raise HTTPException(status_code=400, detail="Login callback is missing code/state. Start again at /auth/login.")
    try:
        x_auth.exchange_code_for_tokens(code, state)
    except XAuthError as e:
        logger.warning("Login callback failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse("/api/auth/status")


@app.get("/api/auth/status")
def auth_status():
    user = x_auth.get_logged_in_user()
    return {"logged_in": user is not None, "user": user}


@app.post("/api/sync")
def api_sync():
    try:
        return sync_bookmarks()
    except JobBusyError as e:
        # 409 rather than queueing: sync can run for minutes, and stacking
        # requests behind it is how you end up with two writers on one
        # SQLite file and two racing OAuth token refreshes.
        logger.info("Sync request rejected: %s", e)
        raise HTTPException(status_code=409, detail=str(e))
    except (XAuthError, XApiError) as e:
        logger.warning("Sync request failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/enrich")
def api_enrich():
    try:
        return run_enrichment()
    except JobBusyError as e:
        logger.info("Enrich request rejected: %s", e)
        raise HTTPException(status_code=409, detail=str(e))
    except EnrichmentError as e:
        logger.warning("Enrich request failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/jobs/status")
def api_job_status():
    running = jobs.current_job()
    return {"running": running is not None, "job": running}


# Upper bound so a hand-written limit cannot ask for the whole table at once.
MAX_SEARCH_LIMIT = 200


@app.get("/api/search")
def api_search(
    q: str | None = None,
    tag: list[str] = Query(default=[]),
    limit: int = Query(default=30, ge=1, le=MAX_SEARCH_LIMIT),
    offset: int = Query(default=0, ge=0),
):
    conn = db.get_connection()
    try:
        return queries.search_bookmarks(conn, q, tag, limit, offset)
    except sqlite3.OperationalError as e:
        # queries.build_fts_query neutralises FTS5 syntax, so this should be
        # unreachable for search text. Kept so that anything it misses is a
        # 400 with a readable message rather than a 500 stack trace on a
        # search box that fires on every keystroke.
        logger.warning("Search failed for q=%r tags=%r: %s", q, tag, e)
        raise HTTPException(status_code=400, detail=f"Could not run that search: {e}")
    finally:
        conn.close()


@app.get("/api/tags")
def api_tags():
    conn = db.get_connection()
    try:
        return queries.list_tags(conn)
    finally:
        conn.close()


@app.get("/api/watch-later")
def api_watch_later(status: str | None = None):
    conn = db.get_connection()
    try:
        return queries.list_watch_later(conn, status)
    finally:
        conn.close()


@app.post("/api/watch-later/{bookmark_id}/toggle")
def api_toggle_watch_later(bookmark_id: int):
    conn = db.get_connection()
    try:
        result = queries.toggle_watch_later(conn, bookmark_id)
        if result is None:
            raise HTTPException(status_code=404, detail="That bookmark isn't in the watch-later queue")
        return result
    finally:
        conn.close()


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
