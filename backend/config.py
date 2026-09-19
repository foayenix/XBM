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

# Auto-reload on code changes. A development convenience: it runs a reloader
# subprocess and restarts the app (and the background scheduler with it) on
# every file write, so it stays off unless you ask for it.
APP_RELOAD = os.environ.get("APP_RELOAD", "").strip().lower() in ("1", "true", "yes", "on")

LOG_DIR = REPO_ROOT / "logs"

# Optional background sync. 0 (default) means off — sync only runs when you
# click "Sync now" or hit /api/sync yourself.
SYNC_INTERVAL_MINUTES = float(os.environ.get("SYNC_INTERVAL_MINUTES", "0") or "0")

# Hostnames this app answers to. The browser sends the name it was asked
# for, so pinning it blocks DNS rebinding: a page on evil.com whose DNS
# points at 127.0.0.1 still sends "Host: evil.com".
#
# Ports are deliberately NOT part of this check. Rebinding is a hostname
# attack — the port is whatever this app happens to listen on — and
# including it would 403 the entire app whenever the served port differs
# from APP_PORT. Cross-site POSTs are caught separately by the same-origin
# check in backend/security.py, which does compare ports.
DEFAULT_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _parse_allowed_hosts(raw: str) -> set[str]:
    """Parse a comma-separated host list, tolerating a "host:port" entry."""
    hosts = set()
    for entry in raw.split(","):
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry.startswith("[") and "]:" in entry:  # [::1]:8000
            entry = entry.split("]:")[0] + "]"
        elif entry.count(":") == 1:  # host:port, but not a bare IPv6 literal
            entry = entry.split(":")[0]
        hosts.add(entry)
    return hosts


ALLOWED_HOSTS = _parse_allowed_hosts(os.environ.get("ALLOWED_HOSTS", "")) or set(DEFAULT_ALLOWED_HOSTS)
