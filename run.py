#!/usr/bin/env python3
"""Single entrypoint: starts the local web server.

Usage:
    python run.py
"""
import uvicorn

from backend import config

if __name__ == "__main__":
    uvicorn.run("backend.main:app", host=config.APP_HOST, port=config.APP_PORT, reload=True)
