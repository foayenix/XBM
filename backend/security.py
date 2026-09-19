"""Host and Origin checks for a server that binds to localhost.

XBM has no login of its own: anything that can reach the port can spend your
X and Anthropic API budget through POST /api/sync. Two browser-level attacks
follow from that, and neither is stopped by binding to 127.0.0.1:

1. Cross-site request forgery. Any page you have open can POST to
   http://127.0.0.1:8000/api/sync. It is a "simple request", so the browser
   sends it without a preflight; CORS blocks the attacker from *reading* the
   response, but the sync has already run and the money is already spent.

2. DNS rebinding. A page on evil.com whose DNS answer flips to 127.0.0.1
   becomes same-origin with this app and can then read responses too.

Both are caught by checking the headers the browser sets and a page cannot
forge: Host (what name was asked for) and Origin (who is asking). A
non-browser client like curl sends no Origin at all, so the documented
`curl -X POST .../api/sync` workflow keeps working - a browser cannot omit
Origin on a cross-origin POST, so its absence is not a hole.
"""
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from backend import config

logger = logging.getLogger("xbm.security")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _hostname_of(host_header: str) -> str:
    """The hostname part of a Host header, without any :port."""
    host = host_header.strip().lower().rstrip(".")
    if host.startswith("["):  # IPv6 literal, e.g. [::1]:8000
        return host.split("]")[0] + "]" if "]" in host else host
    return host.split(":")[0]


def _host_is_allowed(host_header: str | None) -> bool:
    if not host_header:
        return False
    hostname = _hostname_of(host_header)
    # Accept the bracketed and bare spellings of an IPv6 literal.
    return hostname in config.ALLOWED_HOSTS or hostname.strip("[]") in config.ALLOWED_HOSTS


def _origin_is_same_site(origin: str, host_header: str | None) -> bool:
    """True if Origin names the very same host:port as this request.

    Comparing against the request's own Host, rather than a configured
    port, keeps this correct no matter which port the app is served on -
    and it is exactly the same-origin rule, so http://127.0.0.1:9999 is
    correctly treated as a different site from http://127.0.0.1:8000.
    """
    from urllib.parse import urlparse

    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    if not host_header:
        return False
    return parsed.netloc.strip().lower() == host_header.strip().lower()


async def guard_requests(request: Request, call_next):
    host = request.headers.get("host")
    if not _host_is_allowed(host):
        logger.warning("Rejected request with unexpected Host header: %r", host)
        return JSONResponse(
            status_code=403,
            content={"detail": (
                f"Unexpected Host header {host!r}. XBM only answers to "
                f"{sorted(config.ALLOWED_HOSTS)}; set ALLOWED_HOSTS in .env to change that."
            )},
        )

    if request.method not in SAFE_METHODS:
        origin = request.headers.get("origin")
        # No Origin means a non-browser client (curl, a script). A browser
        # always sets it on a cross-origin state-changing request, so this
        # does not weaken the CSRF check.
        if origin and not _origin_is_same_site(origin, host):
            logger.warning("Rejected %s %s from cross-site origin %r",
                           request.method, request.url.path, origin)
            return JSONResponse(
                status_code=403,
                content={"detail": f"Cross-site {request.method} from {origin} is not allowed."},
            )

    return await call_next(request)
