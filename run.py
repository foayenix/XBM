#!/usr/bin/env python3
"""Single entrypoint: starts the local web server.

Usage:
    python run.py
"""
import sys

# Checked before importing anything of ours: the backend uses PEP 604
# annotations (str | None), which are a TypeError at import time on 3.9,
# so a too-old interpreter otherwise fails with a traceback that says
# nothing about the version. macOS still ships 3.9 as `python3`.
MINIMUM_PYTHON = (3, 11)
if sys.version_info < MINIMUM_PYTHON:
    sys.exit(
        f"XBM needs Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer, "
        f"but this is Python {sys.version_info.major}.{sys.version_info.minor} "
        f"({sys.executable}).\n"
        "Create the virtualenv with a newer interpreter, e.g.:\n"
        "    rm -rf .venv && python3.12 -m venv .venv && source .venv/bin/activate\n"
        "    pip install -r requirements.txt"
    )

import uvicorn  # noqa: E402

from backend import config  # noqa: E402
from scripts.init_db import init_db  # noqa: E402

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
