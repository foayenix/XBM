"""FastAPI app entrypoint. Serves the local web UI and API routes.

Search, tag filters, and the watch-later UI are added in Phase 5.
"""
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend import config, db, queries, x_auth
from backend.enrich import EnrichmentError, run_enrichment
from backend.sync import sync_bookmarks
from backend.x_auth import XAuthError
from backend.x_client import XApiError

logging.basicConfig(level=logging.INFO)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="XBM - X Bookmarks Watch/Read Later")

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
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/auth/callback")
def auth_callback(code: str = Query(...), state: str = Query(...)):
    try:
        x_auth.exchange_code_for_tokens(code, state)
    except XAuthError as e:
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
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/enrich")
def api_enrich():
    try:
        return run_enrichment()
    except EnrichmentError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/search")
def api_search(q: str | None = None, tag: list[str] = Query(default=[]), limit: int = 30, offset: int = 0):
    conn = db.get_connection()
    try:
        return queries.search_bookmarks(conn, q, tag, limit, offset)
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
