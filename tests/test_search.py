"""Blocker 1: search must survive ordinary text.

FTS5 parses its MATCH argument as a query language, so before the fix an
apostrophe, a plus sign, a trailing boolean or a colon each produced an
unhandled OperationalError -> HTTP 500. The UI searches on every keystroke,
so half-typed input hit this constantly.
"""
import pytest

from backend.queries import build_fts_query, search_bookmarks

# Every one of these raised sqlite3.OperationalError before the fix.
CRASHERS = [
    "what's",        # syntax error near "'"
    "c++",           # syntax error near "+"
    "foo AND",       # dangling boolean
    "alice OR",      # dangling boolean
    '"unclosed',     # unterminated string
    "x:",            # read as a column filter -> no such column: x
    "NEAR(a b)",
    "a AND (b OR",
    "-",
    "*",
    "^foo",
    "{bar}",
    "it's a 'test' \"quote\"",
]


@pytest.mark.parametrize("q", CRASHERS)
def test_search_survives_fts_syntax(conn, seed, q):
    seed(1, "hello world")
    result = search_bookmarks(conn, q, [], 30, 0)
    assert isinstance(result["total"], int)


@pytest.mark.parametrize("q", CRASHERS)
def test_search_endpoint_never_500s(client, conn, seed, q):
    seed(1, "hello world")
    assert client.get("/api/search", params={"q": q}).status_code == 200


def test_build_fts_query_quotes_every_token():
    assert build_fts_query("foo bar") == '"foo" "bar"'


def test_build_fts_query_escapes_embedded_quotes():
    # FTS5 escapes a double quote inside a string by doubling it.
    assert build_fts_query('say "hi"') == '"say" """hi"""'


def test_build_fts_query_empty_for_blank_input():
    assert build_fts_query("") == ""
    assert build_fts_query("   \t ") == ""
    assert build_fts_query(None) == ""


def test_operators_are_treated_as_literals(conn, seed):
    """"foo AND bar" must mean three words, not a boolean expression."""
    seed(1, "foo AND bar together")
    seed(2, "foo only")
    ids = [r["id"] for r in search_bookmarks(conn, "foo AND bar", [], 30, 0)["results"]]
    assert ids == [1]  # bookmark 2 lacks "and"/"bar", so a literal AND excludes it


def test_search_still_finds_normal_words(conn, seed):
    seed(1, "Great post about python testing")
    seed(2, "unrelated cooking content")
    assert [r["id"] for r in search_bookmarks(conn, "python", [], 30, 0)["results"]] == [1]
    assert [r["id"] for r in search_bookmarks(conn, "python testing", [], 30, 0)["results"]] == [1]


def test_punctuation_only_query_browses_instead_of_matching_nothing(conn, seed):
    """A query that tokenizes to nothing falls back to browsing, not an error."""
    seed(1, "one")
    seed(2, "two")
    assert search_bookmarks(conn, "   ", [], 30, 0)["total"] == 2


def test_search_matches_thread_and_tag_text(conn, seed):
    seed(1, "short tweet", thread_text="the full thread mentions kubernetes")
    assert search_bookmarks(conn, "kubernetes", [], 30, 0)["total"] == 1


def test_snippet_markers_are_non_html(conn, seed):
    """Highlights use control characters, never HTML - tweet text is untrusted."""
    seed(1, "python is great")
    snippet = search_bookmarks(conn, "python", [], 30, 0)["results"][0]["snippet"]
    assert "\x01python\x02" in snippet
    assert "<" not in snippet


@pytest.mark.parametrize("limit,expected", [(0, 422), (201, 422), (1, 200), (200, 200)])
def test_search_limit_is_bounded(client, limit, expected):
    assert client.get("/api/search", params={"limit": limit}).status_code == expected


def test_search_offset_rejects_negative(client):
    assert client.get("/api/search", params={"offset": -1}).status_code == 422
