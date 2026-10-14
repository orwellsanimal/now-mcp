"""now_data.client: explicit credentials and instance normalisation (no network)."""

import sys
import types

import pytest

from now_data import client


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """A fake pysnc and a no-op wake check; reset the module's cached state."""
    made = []

    class FakeClient:
        def __init__(self, instance, auth):
            made.append((instance, auth))

    monkeypatch.setitem(sys.modules, "pysnc", types.SimpleNamespace(ServiceNowClient=FakeClient))
    woke = []
    monkeypatch.setattr(client, "ensure_awake", lambda url, u, p: woke.append(url))
    monkeypatch.setattr(client, "_client", None)
    monkeypatch.setattr(client, "_awake", False)
    monkeypatch.setattr(client, "_explicit", None)
    for k in ("SN_INSTANCE", "SN_USER", "SN_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(client, "load_dotenv", lambda *a, **k: None)
    return made, woke


@pytest.mark.parametrize(
    "given, url",
    [
        ("dev123", "https://dev123.service-now.com"),
        ("dev123.service-now.com", "https://dev123.service-now.com"),
        ("https://dev123.service-now.com/", "https://dev123.service-now.com"),
        (" acme.example.com ", "https://acme.example.com"),
    ],
)
def test_instance_url(given, url):
    assert client.instance_url(given) == url


def test_bare_env_instance_is_normalised_before_the_wake_check(fresh, monkeypatch):
    made, woke = fresh
    monkeypatch.setenv("SN_INSTANCE", "dev123")
    monkeypatch.setenv("SN_USER", "u")
    monkeypatch.setenv("SN_PASSWORD", "p")
    client.get_client()
    assert woke == ["https://dev123.service-now.com"]
    assert made == [("https://dev123.service-now.com", ("u", "p"))]


def test_configure_wins_over_env_and_resets_the_cached_client(fresh, monkeypatch):
    made, woke = fresh
    monkeypatch.setenv("SN_INSTANCE", "envinst")
    monkeypatch.setenv("SN_USER", "envuser")
    monkeypatch.setenv("SN_PASSWORD", "envpw")
    client.get_client()
    client.configure("other", "alice", "secret")
    client.get_client()
    assert made[-1] == ("https://other.service-now.com", ("alice", "secret"))
    assert woke == ["https://envinst.service-now.com", "https://other.service-now.com"]


def test_configure_rejects_blanks(fresh):
    with pytest.raises(client.ConfigError):
        client.configure("dev1", "", "pw")


def test_missing_credentials_raise_config_error(fresh):
    with pytest.raises(client.ConfigError):
        client.get_client()
