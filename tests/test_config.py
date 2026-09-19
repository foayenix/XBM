"""run.py shipped reload=True, a development setting: it runs a reloader
subprocess and restarts the app (and the background scheduler) on every
file write. It is now opt-in via APP_RELOAD.
"""
import importlib

import pytest


def reload_config(monkeypatch, **env):
    for key in ("APP_RELOAD", "APP_PORT", "ALLOWED_HOSTS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from backend import config

    return importlib.reload(config)


@pytest.fixture(autouse=True)
def _restore_config():
    yield
    from backend import config

    importlib.reload(config)


def test_reload_is_off_by_default(monkeypatch):
    assert reload_config(monkeypatch).APP_RELOAD is False


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
def test_reload_can_be_turned_on(monkeypatch, value):
    assert reload_config(monkeypatch, APP_RELOAD=value).APP_RELOAD is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_other_values_leave_reload_off(monkeypatch, value):
    assert reload_config(monkeypatch, APP_RELOAD=value).APP_RELOAD is False


def test_run_py_uses_the_setting_rather_than_a_hardcoded_true():
    source = (__import__("pathlib").Path(__file__).resolve().parent.parent / "run.py").read_text()
    assert "reload=True" not in source
    assert "reload=config.APP_RELOAD" in source


def test_allowed_hosts_are_hostnames_not_host_port_pairs(monkeypatch):
    """Ports are not part of the rebinding check - see backend/config.py."""
    config = reload_config(monkeypatch, APP_PORT="9001")
    assert config.ALLOWED_HOSTS == {"127.0.0.1", "localhost", "::1", "[::1]"}


def test_allowed_hosts_can_be_overridden(monkeypatch):
    config = reload_config(monkeypatch, ALLOWED_HOSTS="xbm.local, 10.0.0.9")
    assert config.ALLOWED_HOSTS == {"xbm.local", "10.0.0.9"}


def test_a_port_in_allowed_hosts_is_tolerated(monkeypatch):
    """People will write host:port out of habit; accept it and strip it."""
    config = reload_config(monkeypatch, ALLOWED_HOSTS="xbm.local:8000, 10.0.0.9:1234")
    assert config.ALLOWED_HOSTS == {"xbm.local", "10.0.0.9"}


def test_an_ipv6_literal_survives_parsing(monkeypatch):
    config = reload_config(monkeypatch, ALLOWED_HOSTS="[::1]:8000, [fe80::1]")
    assert config.ALLOWED_HOSTS == {"[::1]", "[fe80::1]"}
