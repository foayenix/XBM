"""Small helpers shared across backend modules."""
import re
from urllib.parse import parse_qs, urlparse

YOUTUBE_HOSTS = {"youtube.com", "youtu.be", "youtube-nocookie.com"}


def host_matches(host: str, domains: set[str]) -> bool:
    """True if `host` is one of `domains` or a subdomain of one.

    Hostname comparison, never substring: `"youtube.com" in host` is also
    true for notyoutube.com, and `"x.com" in url` is true for netflix.com.
    """
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def is_youtube_url(url: str) -> bool:
    """True for youtube.com and every subdomain of it (www, m, music, ...)."""
    return host_matches(urlparse(url).hostname or "", YOUTUBE_HOSTS)


def extract_youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host_matches(host, {"youtu.be"}):
        return parsed.path.lstrip("/").split("/")[0] or None
    if host_matches(host, {"youtube.com", "youtube-nocookie.com"}):
        # music.youtube.com and m.youtube.com use the same /watch?v= form.
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        match = re.match(r"^/(shorts|embed|live|v)/([^/?#]+)", parsed.path)
        if match:
            return match.group(2)
    return None
