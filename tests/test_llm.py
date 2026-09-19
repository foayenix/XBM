"""Enrichment must be able to run against a model on your own machine.

Two backends: the Claude API through the official SDK, and anything
speaking the OpenAI /chat/completions shape - Ollama, LM Studio,
llama.cpp's server, vLLM. These cover the second, plus the messier replies
small local models give compared with a hosted one.
"""
import importlib

import httpx
import pytest

from backend import config, enrich, llm
from backend.llm import LLMError, OpenAICompatibleProvider


@pytest.fixture(autouse=True)
def _restore_config():
    yield
    importlib.reload(config)


def configure(monkeypatch, **env):
    for key in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY",
                "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "LLM_TIMEOUT_SECONDS",
                "LLM_MAX_TOKENS", "LLM_INPUT_CHAR_LIMIT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(config)


def local_provider(handler, *, model="llama3.1:8b", api_key="", timeout=30.0):
    """A provider whose HTTP layer is a stub - no network, no real model."""
    provider = OpenAICompatibleProvider(
        base_url="http://localhost:11434/v1", model=model, api_key=api_key, timeout=timeout
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider_post = client.post

    def post(url, **kwargs):
        return provider_post(url, **kwargs)

    return provider, post


def reply(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


# --- provider selection ---------------------------------------------------

def test_defaults_to_anthropic(monkeypatch):
    assert configure(monkeypatch).LLM_PROVIDER == "anthropic"


def test_a_base_url_alone_selects_the_local_provider(monkeypatch):
    """Pointing at your own server is an unambiguous "use it"."""
    cfg = configure(monkeypatch, LLM_BASE_URL="http://localhost:11434/v1", LLM_MODEL="llama3.1:8b")
    assert cfg.LLM_PROVIDER == "openai"


def test_provider_can_be_forced(monkeypatch):
    cfg = configure(monkeypatch, LLM_PROVIDER="anthropic",
                    LLM_BASE_URL="http://localhost:11434/v1", LLM_MODEL="x")
    assert cfg.LLM_PROVIDER == "anthropic"


def test_an_unknown_provider_is_rejected(monkeypatch):
    configure(monkeypatch, LLM_PROVIDER="gpt4all")
    with pytest.raises(LLMError, match="Unknown LLM_PROVIDER"):
        llm.get_provider()


def test_the_local_provider_needs_no_anthropic_key(monkeypatch):
    configure(monkeypatch, LLM_BASE_URL="http://localhost:11434/v1", LLM_MODEL="llama3.1:8b")
    provider = llm.get_provider()
    assert provider.name == "openai"
    assert provider.model == "llama3.1:8b"


def test_anthropic_without_a_key_explains_the_local_option(monkeypatch):
    configure(monkeypatch)
    with pytest.raises(LLMError, match="LLM_BASE_URL"):
        llm.get_provider()


@pytest.mark.parametrize("env,missing", [
    ({"LLM_PROVIDER": "openai", "LLM_MODEL": "x"}, "LLM_BASE_URL"),
    ({"LLM_PROVIDER": "openai", "LLM_BASE_URL": "http://localhost:11434/v1"}, "LLM_MODEL"),
])
def test_incomplete_local_config_says_what_is_missing(monkeypatch, env, missing):
    configure(monkeypatch, **env)
    with pytest.raises(LLMError, match=missing):
        llm.get_provider()


# --- endpoint shapes people actually paste --------------------------------

@pytest.mark.parametrize("given,expected", [
    ("http://localhost:11434", "http://localhost:11434/v1/chat/completions"),
    ("http://localhost:11434/", "http://localhost:11434/v1/chat/completions"),
    ("http://localhost:1234/v1", "http://localhost:1234/v1/chat/completions"),
    ("http://localhost:1234/v1/", "http://localhost:1234/v1/chat/completions"),
    ("http://localhost:8080/v1/chat/completions", "http://localhost:8080/v1/chat/completions"),
])
def test_base_url_is_accepted_in_any_common_shape(given, expected):
    assert OpenAICompatibleProvider._resolve_endpoint(given) == expected


# --- talking to a local server --------------------------------------------

def test_a_normal_local_reply_comes_back(monkeypatch):
    def handler(request):
        return reply("A concise summary of the article.")

    provider, post = local_provider(handler)
    monkeypatch.setattr(llm.httpx, "post", post)
    assert provider.complete("summarize this", max_tokens=512) == "A concise summary of the article."


def test_the_request_is_the_openai_shape(monkeypatch):
    captured = {}

    def handler(request):
        import json
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        captured["url"] = str(request.url)
        return reply("ok")

    provider, post = local_provider(handler, api_key="sk-local")
    monkeypatch.setattr(llm.httpx, "post", post)
    provider.complete("hello", max_tokens=256)

    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    assert captured["body"]["model"] == "llama3.1:8b"
    assert captured["body"]["messages"] == [{"role": "user", "content": "hello"}]
    assert captured["body"]["max_tokens"] == 256
    assert captured["body"]["stream"] is False
    assert captured["auth"] == "Bearer sk-local"


def test_no_auth_header_when_no_key_is_set(monkeypatch):
    captured = {}

    def handler(request):
        captured["auth"] = request.headers.get("authorization")
        return reply("ok")

    provider, post = local_provider(handler, api_key="")
    monkeypatch.setattr(llm.httpx, "post", post)
    provider.complete("hello", max_tokens=64)
    assert captured["auth"] is None


def test_a_server_that_is_not_running_says_so(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("Connection refused")

    provider, post = local_provider(handler)
    monkeypatch.setattr(llm.httpx, "post", post)
    with pytest.raises(LLMError, match="Is it running"):
        provider.complete("hello", max_tokens=64)


def test_a_slow_model_names_the_timeout_knob(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("too slow")

    provider, post = local_provider(handler)
    monkeypatch.setattr(llm.httpx, "post", post)
    with pytest.raises(LLMError, match="LLM_TIMEOUT_SECONDS"):
        provider.complete("hello", max_tokens=64)


def test_a_404_points_at_the_base_url(monkeypatch):
    def handler(request):
        return httpx.Response(404, text="not found")

    provider, post = local_provider(handler)
    monkeypatch.setattr(llm.httpx, "post", post)
    with pytest.raises(LLMError, match="LLM_BASE_URL"):
        provider.complete("hello", max_tokens=64)


def test_a_model_the_server_does_not_have_surfaces_its_error(monkeypatch):
    def handler(request):
        return httpx.Response(404, json={"error": {"message": 'model "llama9" not found'}})

    provider, post = local_provider(handler, model="llama9")
    monkeypatch.setattr(llm.httpx, "post", post)
    with pytest.raises(LLMError):
        provider.complete("hello", max_tokens=64)


def test_an_error_payload_is_surfaced(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"error": {"message": "context length exceeded"}})

    provider, post = local_provider(handler)
    monkeypatch.setattr(llm.httpx, "post", post)
    with pytest.raises(LLMError, match="context length exceeded"):
        provider.complete("hello", max_tokens=64)


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{}]}])
def test_an_unexpected_shape_does_not_crash_the_run(monkeypatch, payload):
    def handler(request):
        return httpx.Response(200, json=payload)

    provider, post = local_provider(handler)
    monkeypatch.setattr(llm.httpx, "post", post)
    with pytest.raises(LLMError, match="unexpected response shape"):
        provider.complete("hello", max_tokens=64)


def test_an_empty_reply_is_an_error_not_an_empty_summary(monkeypatch):
    def handler(request):
        return reply("   ")

    provider, post = local_provider(handler)
    monkeypatch.setattr(llm.httpx, "post", post)
    with pytest.raises(LLMError, match="empty reply"):
        provider.complete("hello", max_tokens=64)


def test_non_json_reply_is_reported_clearly(monkeypatch):
    def handler(request):
        return httpx.Response(200, text="<html>proxy error</html>")

    provider, post = local_provider(handler)
    monkeypatch.setattr(llm.httpx, "post", post)
    with pytest.raises(LLMError, match="non-JSON"):
        provider.complete("hello", max_tokens=64)


# --- health ---------------------------------------------------------------

def test_health_reports_the_local_backend(monkeypatch):
    configure(monkeypatch, LLM_BASE_URL="http://localhost:11434/v1", LLM_MODEL="llama3.1:8b")
    assert llm.describe() == {
        "provider": "openai",
        "model": "llama3.1:8b",
        "base_url": "http://localhost:11434/v1",
        "configured": True,
    }


def test_health_reports_an_incomplete_local_backend(monkeypatch):
    configure(monkeypatch, LLM_PROVIDER="openai", LLM_BASE_URL="http://localhost:11434/v1")
    assert llm.describe()["configured"] is False


def test_health_uses_the_canonical_haiku_id_by_default(monkeypatch):
    """The model table's IDs are complete as-is; no date suffix."""
    configure(monkeypatch, ANTHROPIC_API_KEY="sk-test")
    described = llm.describe()
    assert described["model"] == "claude-haiku-4-5"
    assert described["configured"] is True


# --- messy replies from small local models --------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("sqlite, databases", ["sqlite", "databases"]),
    ("Here are the tags:\n- sqlite\n- databases", ["sqlite", "databases"]),
    ("**Tags:** sqlite, databases", ["sqlite", "databases"]),
    ("1. sqlite\n2. databases", ["sqlite", "databases"]),
    ("* sqlite\n* databases", ["sqlite", "databases"]),
    ('"sqlite", "databases"', ["sqlite", "databases"]),
    ("`sqlite`, `databases`", ["sqlite", "databases"]),
    ("SQLite, Databases", ["sqlite", "databases"]),
    ("sqlite, SQLite, SQLITE", ["sqlite"]),
    ("sqlite,databases,,   ,search", ["sqlite", "databases", "search"]),
])
def test_tags_survive_the_shapes_local_models_reply_in(raw, expected):
    assert enrich._parse_tags(raw) == expected


def test_a_reasoning_models_scratchpad_is_discarded():
    raw = (
        "<think>The user wants topic tags. This bookmark is about SQLite "
        "internals, so databases fits too.</think>\n"
        "sqlite, databases"
    )
    assert enrich._parse_tags(raw) == ["sqlite", "databases"]


def test_a_scratchpad_full_of_commas_does_not_become_tags():
    raw = "<think>Maybe sqlite, or databases, or storage, or wal, or indexing?</think>\nsqlite"
    assert enrich._parse_tags(raw) == ["sqlite"]


def test_prose_is_not_mistaken_for_tags():
    assert enrich._parse_tags("I am unable to determine tags for this content.") == []


def test_a_sentence_wrapped_around_real_tags_keeps_only_the_tags():
    assert enrich._parse_tags("Sure! Tags: sqlite, databases") == ["sqlite", "databases"]


def test_at_most_four_tags():
    assert len(enrich._parse_tags("a, b, c, d, e, f, g")) == 4


def test_reasoning_is_stripped_from_summaries_too():
    summary = enrich._strip_reasoning("<think>Let me read it.</think>\nThe article argues X.")
    assert summary == "The article argues X."


# --- enrichment end to end against a stubbed local model ------------------

def test_tagging_runs_against_a_local_model(conn, seed, monkeypatch):
    seed(1, "A thread on why SQLite is underrated for local-first apps")

    def handler(request):
        return reply("Here are the tags:\n- sqlite\n- local-first")

    provider, post = local_provider(handler)
    monkeypatch.setattr(llm.httpx, "post", post)
    monkeypatch.setattr(enrich, "_llm", lambda: provider)

    summary = enrich.tag_untagged_bookmarks(conn)
    assert summary["bookmarks_tagged"] == 1

    tags = [r["name"] for r in conn.execute(
        "SELECT t.name FROM tags t JOIN bookmark_tags bt ON bt.tag_id=t.id "
        "WHERE bt.bookmark_id=1 ORDER BY t.name"
    ).fetchall()]
    assert tags == ["local-first", "sqlite"]


def test_a_local_model_that_is_down_fails_the_run_readably(conn, seed, monkeypatch):
    seed(1, "tweet")

    def handler(request):
        raise httpx.ConnectError("Connection refused")

    provider, post = local_provider(handler)
    monkeypatch.setattr(llm.httpx, "post", post)
    monkeypatch.setattr(enrich, "_llm", lambda: provider)

    # Individual failures are caught and counted, not raised.
    summary = enrich.tag_untagged_bookmarks(conn)
    assert summary["bookmarks_tagged"] == 0
    assert conn.execute("SELECT tag_attempts FROM bookmarks WHERE id=1").fetchone()[0] == 1
