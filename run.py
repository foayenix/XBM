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
    uvicorn.run("backend.main:app", host=config.APP_HOST, port=config.APP_PORT, reload=True)
