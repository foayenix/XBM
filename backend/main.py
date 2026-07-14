"""FastAPI app entrypoint. Serves the local web UI and API routes.

Phase 1 scaffold only: a health check and the static frontend.
Sync, enrichment, and search endpoints are added in later phases.
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend import config

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


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
