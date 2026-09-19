#!/usr/bin/env python3
"""Single entrypoint: starts the local web server.

Usage:
    python run.py
"""
import uvicorn

from backend import config
from scripts.init_db import init_db

if __name__ == "__main__":
    init_db()
    # reload defaults to off: it spawns a reloader subprocess and restarts the
    # app (and the background scheduler) on every file write. Set APP_RELOAD=1
    # in .env while working on the code.
    uvicorn.run(
        "backend.main:app",
        host=config.APP_HOST,
        port=config.APP_PORT,
        reload=config.APP_RELOAD,
    )
