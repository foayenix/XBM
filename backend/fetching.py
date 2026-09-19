"""Guarded HTTP fetch for article enrichment.

Enrichment downloads URLs found in other people's tweets. That makes every
fetch attacker-influenceable: whoever wrote the tweet you bookmarked chose
the URL, and the response ends up stored locally and sent to Claude. A bare
httpx.get with follow_redirects=True is too trusting for that:

- No size limit. A bookmarked link to a large file is read fully into
  memory before anything notices it is not an article.
- No content-type check. PDFs, images and archives were handed to the HTML
  parser and then summarized as if they were prose.
- No destination check. A link (or a redirect chain ending) at
  http://127.0.0.1:8000, 169.254.169.254 or a RFC1918 address makes this
  app fetch from the machine's own network on someone else's behalf.

So: resolve the host, refuse anything that is not a public IP, follow
redirects one hop at a time re-checking each destination, require an
HTML-ish content type, and stop reading at MAX_ARTICLE_BYTES.
"""
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("xbm.fetching")

HTTP_HEADERS = {"User-Agent": "XBM/0.1 (personal bookmarks tool; not for redistribution)"}

MAX_ARTICLE_BYTES = 5_000_000  # stop reading past this; articles are far smaller
MAX_REDIRECTS = 5
FETCH_TIMEOUT = 20.0

# Only these get parsed as articles. Anything else (PDF, image, video,
# archive, JSON) is refused before its body is read.
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")

# Extensions worth rejecting before a request is even made.
NON_ARTICLE_EXTENSIONS = {
    ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z", ".dmg", ".exe", ".apk",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".ico", ".avif",
    ".mp4", ".webm", ".mov", ".avi", ".mkv", ".mp3", ".wav", ".ogg", ".flac",
    ".csv", ".xlsx", ".doc", ".docx", ".ppt", ".pptx", ".epub",
}


class UnsafeURLError(Exception):
    """The URL points somewhere we refuse to fetch from."""


class UnsupportedContentError(Exception):
    """The URL resolved to something that is not a readable article."""


def looks_like_a_file_download(url: str) -> bool:
    path = (urlparse(url).path or "").lower()
    return any(path.endswith(ext) for ext in NON_ARTICLE_EXTENSIONS)


def _resolved_addresses(hostname: str) -> list[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise UnsafeURLError(f"Could not resolve {hostname}: {e}")
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addresses:
        raise UnsafeURLError(f"No usable IP address for {hostname}")
    return addresses


def assert_safe_url(url: str) -> None:
    """Raise UnsafeURLError unless this URL is public http(s).

    Every resolved address must be public: a hostname with one public and
    one private A record is refused rather than raced.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Refusing non-http(s) URL: {url}")
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeURLError(f"URL has no hostname: {url}")

    for address in _resolved_addresses(hostname):
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved or address.is_unspecified):
            raise UnsafeURLError(
                f"Refusing to fetch {url}: {hostname} resolves to the non-public address {address}"
            )


def fetch_article_html(url: str, client: httpx.Client | None = None) -> tuple[str, str]:
    """Fetch an article, returning (html, final_url).

    Redirects are followed manually so each hop can be re-checked; httpx's
    own follow_redirects would happily land on a private address after
    starting at a public one.
    """
    if looks_like_a_file_download(url):
        raise UnsupportedContentError(f"Refusing to summarize what looks like a file download: {url}")

    owns_client = client is None
    client = client or httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=False)
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            assert_safe_url(current)
            with client.stream("GET", current, headers=HTTP_HEADERS) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise UnsupportedContentError(f"Redirect with no Location header at {current}")
                    current = str(response.url.join(location))
                    continue

                response.raise_for_status()

                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type and not content_type.startswith(HTML_CONTENT_TYPES):
                    raise UnsupportedContentError(
                        f"Refusing to summarize {current}: content type is {content_type}, not an article"
                    )

                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_ARTICLE_BYTES:
                    raise UnsupportedContentError(
                        f"Refusing to fetch {current}: {declared} bytes exceeds the "
                        f"{MAX_ARTICLE_BYTES} byte limit"
                    )

                # Content-Length can lie or be absent, so cap while reading too.
                chunks, total = [], 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_ARTICLE_BYTES:
                        raise UnsupportedContentError(
                            f"Refusing to fetch {current}: body exceeded the "
                            f"{MAX_ARTICLE_BYTES} byte limit"
                        )
                    chunks.append(chunk)

                encoding = response.encoding or "utf-8"
                return b"".join(chunks).decode(encoding, errors="replace"), current

        raise UnsupportedContentError(f"Too many redirects starting from {url}")
    finally:
        if owns_client:
            client.close()
