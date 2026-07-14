"""Small helpers shared across backend modules."""
import re
from urllib.parse import parse_qs, urlparse


def extract_youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        return parsed.path.lstrip("/") or None
    if "youtube.com" in host:
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        match = re.match(r"^/(shorts|embed|live)/([^/]+)", parsed.path)
        if match:
            return match.group(2)
    return None
