"""FastAPI app entrypoint. Serves the local web UI and API routes."""
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import config, db, queries, scheduler, x_auth
from backend.enrich import EnrichmentError, run_enrichment
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
def auth_callback(code: str = Query(...), state: str = Query(...)):
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
    except (XAuthError, XApiError) as e:
        logger.warning("Sync request failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/enrich")
def api_enrich():
    try:
        return run_enrichment()
    except EnrichmentError as e:
        logger.warning("Enrich request failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/search")
def api_search(q: str | None = None, tag: list[str] = Query(default=[]), type: str | None = None,
               limit: int = 30, offset: int = 0):
    conn = db.get_connection()
    try:
        return queries.search_bookmarks(conn, q, tag, type, limit, offset)
    finally:
        conn.close()


@app.get("/api/stats")
def api_stats():
    conn = db.get_connection()
    try:
        return queries.get_stats(conn)
    finally:
        conn.close()


@app.get("/api/bookmarks/{bookmark_id}")
def api_bookmark_detail(bookmark_id: int):
    conn = db.get_connection()
    try:
        result = queries.get_bookmark_detail(conn, bookmark_id)
        if result is None:
            raise HTTPException(status_code=404, detail="No such bookmark")
        return result
    finally:
        conn.close()


class TagBody(BaseModel):
    name: str


@app.post("/api/bookmarks/{bookmark_id}/tags")
def api_add_tag(bookmark_id: int, body: TagBody):
    name = body.name.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Tag name can't be empty")
    conn = db.get_connection()
    try:
        tags = queries.add_tag(conn, bookmark_id, name)
        if tags is None:
            raise HTTPException(status_code=404, detail="No such bookmark")
        return {"tags": tags}
    finally:
        conn.close()


@app.delete("/api/bookmarks/{bookmark_id}/tags/{tag_name}")
def api_remove_tag(bookmark_id: int, tag_name: str):
    conn = db.get_connection()
    try:
        tags = queries.remove_tag(conn, bookmark_id, tag_name)
        if tags is None:
            raise HTTPException(status_code=404, detail="No such bookmark")
        return {"tags": tags}
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
