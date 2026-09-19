"""Article fetching used to be a bare httpx.get with follow_redirects=True.

The URLs come from other people's tweets, so every fetch is
attacker-influenceable: no size limit, no content-type check, and no
restriction on where the request could land.
"""
import httpx
import pytest

from backend import fetching
from backend.fetching import (
    MAX_ARTICLE_BYTES,
    UnsafeURLError,
    UnsupportedContentError,
    assert_safe_url,
    fetch_article_html,
    looks_like_a_file_download,
)


def transport(handler):
    """An httpx client whose responses come from `handler`, no network."""
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


@pytest.fixture
def allow_all_hosts(monkeypatch):
    """Skip DNS/IP checks so transport-level behaviour can be tested alone."""
    monkeypatch.setattr(fetching, "assert_safe_url", lambda url: None)


# --- where we refuse to go ------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/api/sync",     # this app's own API
    "http://localhost:8000/api/sync",
    "http://169.254.169.254/latest/meta-data/",  # cloud instance metadata
    "http://10.0.0.5/admin",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://[::1]:8000/",
    "http://0.0.0.0/",
])
def test_private_and_loopback_destinations_are_refused(url):
    with pytest.raises(UnsafeURLError):
        assert_safe_url(url)


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "mailto:a@b.com"])
def test_non_http_schemes_are_refused(url):
    with pytest.raises(UnsafeURLError):
        assert_safe_url(url)


def test_a_public_address_is_allowed(monkeypatch):
    import ipaddress
    monkeypatch.setattr(fetching, "_resolved_addresses",
                        lambda h: [ipaddress.ip_address("93.184.216.34")])
    assert_safe_url("https://example.com/article")  # does not raise


def test_a_host_with_any_private_record_is_refused(monkeypatch):
    """A split-horizon answer is refused outright rather than raced."""
    import ipaddress
    monkeypatch.setattr(fetching, "_resolved_addresses", lambda h: [
        ipaddress.ip_address("93.184.216.34"),
        ipaddress.ip_address("127.0.0.1"),
    ])
    with pytest.raises(UnsafeURLError):
        assert_safe_url("https://sneaky.example/")


def test_a_redirect_into_a_private_address_is_caught(monkeypatch):
    """httpx's own follow_redirects would land here; we re-check each hop."""
    import ipaddress

    def resolve(hostname):
        return [ipaddress.ip_address("127.0.0.1" if hostname == "internal.example"
                                     else "93.184.216.34")]

    monkeypatch.setattr(fetching, "_resolved_addresses", resolve)

    def handler(request):
        return httpx.Response(302, headers={"location": "http://internal.example/secret"})

    with pytest.raises(UnsafeURLError, match="internal.example"):
        fetch_article_html("https://public.example/start", client=transport(handler))


# --- what we refuse to read ----------------------------------------------

@pytest.mark.parametrize("url", [
    "https://e.com/paper.pdf", "https://e.com/pic.PNG", "https://e.com/clip.mp4",
    "https://e.com/data.csv", "https://e.com/archive.zip", "https://e.com/book.epub",
])
def test_file_downloads_are_detected_by_extension(url):
    assert looks_like_a_file_download(url)


@pytest.mark.parametrize("url", ["https://e.com/post", "https://e.com/a/b.html", "https://e.com/"])
def test_normal_article_urls_are_not_mistaken_for_downloads(url):
    assert not looks_like_a_file_download(url)


def test_a_pdf_is_refused_before_any_request(allow_all_hosts):
    def handler(request):
        raise AssertionError("should never have been requested")

    with pytest.raises(UnsupportedContentError):
        fetch_article_html("https://e.com/paper.pdf", client=transport(handler))


@pytest.mark.parametrize("content_type", [
    "application/pdf", "image/jpeg", "video/mp4", "application/json", "application/zip",
])
def test_non_html_content_types_are_refused(allow_all_hosts, content_type):
    def handler(request):
        return httpx.Response(200, headers={"content-type": content_type}, content=b"x" * 100)

    with pytest.raises(UnsupportedContentError, match="not an article"):
        fetch_article_html("https://e.com/thing", client=transport(handler))


@pytest.mark.parametrize("content_type", ["text/html", "text/html; charset=utf-8", "application/xhtml+xml"])
def test_html_content_types_are_accepted(allow_all_hosts, content_type):
    def handler(request):
        return httpx.Response(200, headers={"content-type": content_type},
                              content=b"<html><body>hi</body></html>")

    html, final = fetch_article_html("https://e.com/post", client=transport(handler))
    assert "hi" in html
    assert final == "https://e.com/post"


# --- how much we read -----------------------------------------------------

def test_an_oversized_declared_length_is_refused(allow_all_hosts):
    def handler(request):
        return httpx.Response(200, headers={
            "content-type": "text/html",
            "content-length": str(MAX_ARTICLE_BYTES + 1),
        }, content=b"x")

    with pytest.raises(UnsupportedContentError, match="exceeds"):
        fetch_article_html("https://e.com/big", client=transport(handler))


def test_a_body_with_no_content_length_is_capped_while_reading(allow_all_hosts):
    """Content-Length is often absent (chunked) or simply wrong, so the read
    itself is capped rather than trusting the header."""
    def chunks():
        for _ in range((MAX_ARTICLE_BYTES // 1000) + 10):
            yield b"x" * 1000

    def handler(request):
        # An iterator body streams with no Content-Length header at all.
        return httpx.Response(200, headers={"content-type": "text/html"}, content=chunks())

    with pytest.raises(UnsupportedContentError, match="exceeded"):
        fetch_article_html("https://e.com/big", client=transport(handler))


def test_the_cap_applies_before_the_whole_body_is_buffered(allow_all_hosts):
    """The point is to stop reading, not to read it all and complain after."""
    produced = {"bytes": 0}

    def chunks():
        while True:
            produced["bytes"] += 10_000
            yield b"x" * 10_000

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=chunks())

    with pytest.raises(UnsupportedContentError):
        fetch_article_html("https://e.com/endless", client=transport(handler))

    # An unbounded generator would never end if we buffered everything first.
    assert produced["bytes"] < MAX_ARTICLE_BYTES * 2


def test_a_normal_page_is_returned_whole(allow_all_hosts):
    body = "<html><body><p>" + ("word " * 500) + "</p></body></html>"

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=body.encode())

    html, _ = fetch_article_html("https://e.com/post", client=transport(handler))
    assert html == body


def test_redirect_chains_are_bounded(allow_all_hosts):
    def handler(request):
        return httpx.Response(302, headers={"location": "https://e.com/next"})

    with pytest.raises(UnsupportedContentError, match="Too many redirects"):
        fetch_article_html("https://e.com/start", client=transport(handler))


def test_a_redirect_to_a_public_page_is_followed(allow_all_hosts):
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(301, headers={"location": "https://e.com/final"})
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>done</html>")

    html, final = fetch_article_html("https://e.com/start", client=transport(handler))
    assert "done" in html
    assert final == "https://e.com/final"


def test_http_errors_still_raise(allow_all_hosts):
    def handler(request):
        return httpx.Response(404, headers={"content-type": "text/html"}, content=b"nope")

    with pytest.raises(httpx.HTTPStatusError):
        fetch_article_html("https://e.com/gone", client=transport(handler))
