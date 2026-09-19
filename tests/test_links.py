"""Blocker 2: link extraction matched hostnames by substring.

`"x.com" in url` is true for netflix.com, linux.com, phoenix.com and every
other domain ending in those characters, so those bookmarks were silently
dropped before enrichment ever saw them - no error, no log line.
"""
import pytest

from backend.sync import _extract_external_links, _is_x_url


def tweet_with(*urls):
    return {"entities": {"urls": [{"expanded_url": u} for u in urls]}}


# Domains that merely end in "x.com" / "twitter.com" and must be KEPT.
FALSE_POSITIVES = [
    "https://www.netflix.com/title/81234",
    "https://www.linux.com/news/kernel",
    "https://phoenix.com/guide",
    "https://matrix.com/docs",
    "https://onyx.com/a",
    "https://notreallytwitter.com/post",
]

X_URLS = [
    "https://x.com/user/status/1",
    "https://www.x.com/user",
    "https://twitter.com/user/status/2",
    "https://www.twitter.com/user",
    "https://mobile.twitter.com/user",
    "https://t.co/abc123",
    "https://pic.twitter.com/xyz",
]


@pytest.mark.parametrize("url", FALSE_POSITIVES)
def test_lookalike_domains_are_kept(url):
    assert not _is_x_url(url)
    assert _extract_external_links(tweet_with(url)) == [url]


@pytest.mark.parametrize("url", X_URLS)
def test_real_x_urls_are_dropped(url):
    assert _is_x_url(url)
    assert _extract_external_links(tweet_with(url)) == []


def test_x_in_query_string_does_not_drop_the_link():
    url = "https://example.com/redirect?target=x.com"
    assert _extract_external_links(tweet_with(url)) == [url]


def test_non_http_schemes_are_skipped():
    links = _extract_external_links(tweet_with(
        "mailto:someone@example.com",
        "javascript:alert(1)",
        "ftp://files.example.com/x",
        "https://example.com/keep",
    ))
    assert links == ["https://example.com/keep"]


def test_mixed_batch_keeps_only_external_http_links():
    links = _extract_external_links(tweet_with(
        "https://x.com/user/status/1",
        "https://www.netflix.com/title/1",
        "https://t.co/short",
        "https://example.com/article",
    ))
    assert links == ["https://www.netflix.com/title/1", "https://example.com/article"]


def test_falls_back_to_short_url_when_no_expanded_url():
    tweet = {"entities": {"urls": [{"url": "https://example.com/only-short"}]}}
    assert _extract_external_links(tweet) == ["https://example.com/only-short"]


def test_handles_tweet_with_no_entities():
    assert _extract_external_links({}) == []
    assert _extract_external_links({"entities": None}) == []


def test_hostname_match_is_case_and_dot_insensitive():
    assert _is_x_url("https://X.COM/user")
    assert _is_x_url("https://x.com./user")  # trailing-dot FQDN form
