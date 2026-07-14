"""FastAPI app entrypoint. Serves the local web UI and API routes.

Search, tag filters, and the watch-later UI are added in Phase 5.
"""
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend import config, x_auth
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


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
