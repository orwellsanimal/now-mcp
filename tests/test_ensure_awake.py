"""now_data.client.ensure_awake: what it waits out, and what it refuses at once (no network)."""

import socket
import ssl
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from now_data import client

URL = "https://dev123.service-now.com"


def _headers(content_type):
    m = Message()
    m["Content-Type"] = content_type
    return m


class Resp:
    def __init__(self, content_type):
        self.headers = _headers(content_type)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def wake(monkeypatch):
    """Feed ensure_awake a script of urlopen outcomes; count the sleeps between tries."""
    state = {"sleeps": 0, "calls": 0}
    # No proxy unless a test sets one: not this machine's environment or registry settings.
    monkeypatch.setattr(client, "getproxies", lambda: {}, raising=False)

    def run(*outcomes, timeout_s=120):
        script = list(outcomes)

        def urlopen(req, timeout=None):
            state["calls"] += 1
            nxt = script.pop(0)
            if isinstance(nxt, BaseException):
                raise nxt
            return nxt

        monkeypatch.setattr(client, "urlopen", urlopen)
        monkeypatch.setattr(client.time, "sleep", lambda s: state.__setitem__("sleeps", state["sleeps"] + 1))
        client.ensure_awake(URL, "alice", "pw", timeout_s=timeout_s)
        return state

    return run


def _http(code, content_type="application/json"):
    return HTTPError(URL, code, "x", _headers(content_type), None)


def test_json_on_the_first_try_returns_without_sleeping(wake):
    assert wake(Resp("application/json;charset=UTF-8")) == {"sleeps": 0, "calls": 1}


def test_a_401_fails_at_once_and_names_the_user_and_the_role(wake):
    with pytest.raises(client.ConfigError) as caught:
        wake(_http(401))
    msg = str(caught.value)
    assert "HTTP 401" in msg and "'alice'" in msg and "snc_basic_auth_api_access" in msg, msg


def test_a_json_403_means_awake(wake):
    # The user can't read sys_user, but the instance answered: the verbs report their own permissions.
    assert wake(_http(403)) == {"sleeps": 0, "calls": 1}


def test_a_name_that_does_not_resolve_fails_at_once(wake):
    with pytest.raises(client.ConfigError, match="'dev123.service-now.com' does not resolve.*SN_INSTANCE"):
        wake(URLError(socket.gaierror(11001, "getaddrinfo failed")))


def test_an_untrusted_certificate_fails_at_once_and_keeps_the_reason(wake):
    err = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                                          "self-signed certificate in certificate chain")
    with pytest.raises(client.ConfigError) as caught:
        wake(URLError(err))
    msg = str(caught.value)
    assert "CERTIFICATE_VERIFY_FAILED" in msg and "SSL_CERT_FILE" in msg and "Windows certificate store" in msg, msg


@pytest.fixture
def via_proxy(monkeypatch, wake):
    """urllib would send the request through a proxy (with credentials in its URL)."""
    def use(bypass=False):
        monkeypatch.setattr(client, "getproxies", lambda: {"https": "http://bob:s3cret@corp-proxy.invalid:8080"}, raising=False)
        monkeypatch.setattr(client, "proxy_bypass", lambda host: bypass, raising=False)
    return use


def _gaierror():
    return URLError(socket.gaierror(11001, "getaddrinfo failed"))


def test_through_a_proxy_a_name_that_does_not_resolve_is_the_proxys(wake, via_proxy):
    via_proxy()
    with pytest.raises(client.ConfigError) as caught:
        wake(_gaierror())
    msg = str(caught.value)
    assert "proxy host 'corp-proxy.invalid' does not resolve" in msg and "HTTPS_PROXY" in msg, msg
    assert "SN_INSTANCE" not in msg and "s3cret" not in msg and "bob" not in msg, msg


def test_a_bypassed_proxy_still_blames_the_instance_name(wake, via_proxy):
    via_proxy(bypass=True)
    with pytest.raises(client.ConfigError, match="'dev123.service-now.com' does not resolve.*SN_INSTANCE"):
        wake(_gaierror())


@pytest.mark.parametrize("status", ["407 Proxy Authentication Required", "403 Forbidden"])
def test_a_proxy_that_refuses_the_tunnel_fails_at_once_and_keeps_its_text(wake, status):
    with pytest.raises(client.ConfigError) as caught:
        wake(URLError(OSError(f"Tunnel connection failed: {status}")))
    msg = str(caught.value)
    assert f"Tunnel connection failed: {status}" in msg and "HTTPS_PROXY" in msg, msg


@pytest.mark.parametrize("first", [
    URLError(OSError("Tunnel connection failed: 502 Bad Gateway")),  # the proxy's upstream may come back
    _http(503, "text/html"),                 # waking
    _http(403, "text/html"),                 # a non-JSON 403 isn't the instance answering
    Resp("text/html"),                       # the hibernation page
    URLError(ConnectionRefusedError(10061, "refused")),  # not measured on a waking PDI yet: keep waiting
    TimeoutError("timed out"),
])
def test_what_waking_can_fix_is_still_retried(wake, first):
    assert wake(first, Resp("application/json")) == {"sleeps": 1, "calls": 2}


def test_still_asleep_at_the_deadline_says_hibernating_not_credentials(wake, monkeypatch):
    with pytest.raises(RuntimeError) as caught:
        wake(timeout_s=0)
    assert "hibernating" in str(caught.value) and "credentials" not in str(caught.value)
    assert not isinstance(caught.value, client.ConfigError)
