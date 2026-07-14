"""Central place for reading configuration from the environment."""
import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

X_CLIENT_ID = os.environ.get("X_CLIENT_ID", "")
X_CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET", "")
X_REDIRECT_URI = os.environ.get("X_REDIRECT_URI", "http://127.0.0.1:8000/auth/callback")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DATABASE_PATH = REPO_ROOT / os.environ.get("DATABASE_PATH", "db/xbm.sqlite3")

APP_HOST = os.environ.get("APP_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("APP_PORT", "8000"))

LOG_DIR = REPO_ROOT / "logs"

# Optional background sync. 0 (default) means off — sync only runs when you
# click "Sync now" or hit /api/sync yourself.
SYNC_INTERVAL_MINUTES = float(os.environ.get("SYNC_INTERVAL_MINUTES", "0") or "0")
