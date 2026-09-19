"""XBM has no login, so anything that can reach the port can spend API
budget via POST /api/sync. Binding to 127.0.0.1 does not stop a page in
your own browser from posting there, nor DNS rebinding from making that
page same-origin. These cover the Host/Origin checks that do.
"""
import pytest


def test_a_normal_browser_request_is_allowed(client):
    assert client.get("/api/health").status_code == 200


def test_a_same_origin_post_is_allowed(client):
    r = client.post("/api/sync", headers={"origin": "http://127.0.0.1:8000"})
    assert r.status_code != 403  # 400 (not logged in) is fine; 403 is not


def test_localhost_and_127_0_0_1_are_different_origins(client):
    """Browsers treat these as distinct origins, and so do we.

    Reaching the app as http://localhost:8000 works end to end (see the
    test below); what is refused is a page on one spelling posting to the
    other, which is a genuine cross-origin request.
    """
    r = client.post("/api/sync", headers={"origin": "http://localhost:8000"})
    assert r.status_code == 403


def test_browsing_via_localhost_works(db_path):
    """The Host check is port-agnostic and accepts either local spelling."""
    from fastapi.testclient import TestClient
    from backend.main import app

    with TestClient(app, base_url="http://localhost:8000", raise_server_exceptions=False) as c:
        assert c.get("/api/health").status_code == 200
        # Same-origin POST from that spelling is accepted.
        assert c.post("/api/sync", headers={"origin": "http://localhost:8000"}).status_code != 403


def test_the_host_check_ignores_the_port(db_path):
    """Serving on a port other than APP_PORT must not 403 the whole app.

    Rebinding is a hostname attack; the port is whatever we listen on.
    """
    from fastapi.testclient import TestClient
    from backend.main import app

    with TestClient(app, base_url="http://127.0.0.1:9123", raise_server_exceptions=False) as c:
        assert c.get("/api/health").status_code == 200
        assert c.post("/api/sync", headers={"origin": "http://127.0.0.1:9123"}).status_code != 403
        # ...while a page on a different port is still cross-origin.
        assert c.post("/api/sync", headers={"origin": "http://127.0.0.1:8000"}).status_code == 403


@pytest.mark.parametrize("origin", [
    "https://evil.example",
    "http://evil.example:8000",
    "http://127.0.0.1.evil.example",
    "http://127.0.0.1:9999",      # right host, wrong port
    "null",
])
def test_cross_site_posts_are_rejected(client, origin):
    """The CSRF case: a page you have open posting to this app."""
    r = client.post("/api/sync", headers={"origin": origin})
    assert r.status_code == 403
    assert "not allowed" in r.json()["detail"]


def test_cross_site_enrich_is_rejected(client):
    assert client.post("/api/enrich", headers={"origin": "https://evil.example"}).status_code == 403


def test_cross_site_watch_later_toggle_is_rejected(client):
    r = client.post("/api/watch-later/1/toggle", headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_a_request_with_no_origin_is_allowed(client):
    """curl and scripts send no Origin; a browser cannot omit it cross-origin."""
    r = client.post("/api/sync")
    assert r.status_code != 403


@pytest.mark.parametrize("host", ["evil.example", "attacker.test:8000", "xbm.example.com"])
def test_dns_rebinding_hosts_are_rejected(client, host):
    """A rebound name still sends its own Host header, which we pin."""
    r = client.get("/api/health", headers={"host": host})
    assert r.status_code == 403
    assert "Unexpected Host" in r.json()["detail"]


def test_rebinding_protection_covers_reads_not_just_writes(client):
    """Rebinding is about reading your bookmarks, so GETs are checked too."""
    assert client.get("/api/search", headers={"host": "evil.example"}).status_code == 403


def test_allowed_hosts_can_be_configured(client, monkeypatch):
    from backend import config

    monkeypatch.setattr(config, "ALLOWED_HOSTS", {"127.0.0.1:8000", "xbm.local"})
    assert client.get("/api/health", headers={"host": "xbm.local"}).status_code == 200
    assert client.get("/api/health", headers={"host": "other.local"}).status_code == 403


def test_safe_methods_skip_the_origin_check(client):
    """A cross-origin GET cannot change anything and the browser blocks the read."""
    r = client.get("/api/health", headers={"origin": "https://evil.example"})
    assert r.status_code == 200
