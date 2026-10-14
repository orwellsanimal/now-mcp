"""Lazy ServiceNowClient singleton with a hibernation guard.

Credentials come from the same `.env` PySNC already reads (`SN_INSTANCE`,
`SN_USER`, `SN_PASSWORD`). The first connection runs `ensure_awake()` so a
freshly-hibernated developer instance is woken before any real query —
otherwise the first call gets the HTML wakeup page instead of JSON.
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import time
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from dotenv import find_dotenv, load_dotenv

if TYPE_CHECKING:
    from pysnc import ServiceNowClient

# pysnc is imported lazily inside get_client() so the server can start and
# advertise its tools without it (the credential-free CI smoke test). It's only
# needed once a verb actually talks to the instance.
_client: "ServiceNowClient | None" = None
_awake = False
# Set by configure(): explicit credentials win over SN_* env / .env.
_explicit: tuple[str, str, str] | None = None


class ConfigError(RuntimeError):
    """Raised when the SN_* settings are missing or wrong (a 401, a host that doesn't resolve, an
    untrusted certificate): waiting won't fix them. Distinct from a slow or sleeping instance."""


def instance_url(instance: str) -> str:
    """`dev123`, `dev123.service-now.com` or a full URL -> `https://dev123.service-now.com`.

    Keeps only scheme, host and port, so a URL pasted from the browser's address bar
    (`https://dev123.service-now.com/now/nav/ui/...`) still points the wake
    check and PySNC at the instance root.
    """
    raw = instance.strip()
    bare = "://" not in raw
    try:
        parts = urlsplit(f"https://{raw}" if bare else raw)
        host, port = parts.hostname, parts.port  # hostname drops any user:pw@
    except ValueError as e:
        raise ConfigError(f"Can't read an instance URL from {instance!r}: {e}") from None
    if not host:
        raise ConfigError(f"Can't read an instance host from {instance!r}.")
    if bare and "." not in host:
        host = f"{host}.service-now.com"
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def configure(instance: str, user: str, password: str) -> None:
    """Use these credentials instead of SN_INSTANCE / SN_USER / SN_PASSWORD.

    For library callers that resolve credentials their own way. Drops any
    cached client, so the next verb connects (and wakes the instance) afresh.
    """
    global _explicit, _client, _awake
    if not all(v and v.strip() for v in (instance, user, password)):
        raise ConfigError("configure() needs a non-empty instance, user and password.")
    _explicit = (instance_url(instance), user, password)
    _client = None
    _awake = False


def _creds() -> tuple[str, str, str]:
    """Return (instance URL, user, password): from configure(), else the env / nearest .env."""
    if _explicit is not None:
        return _explicit
    # find_dotenv walks up from cwd; explicit env vars still win if already set.
    load_dotenv(find_dotenv(usecwd=True))
    instance = os.getenv("SN_INSTANCE")
    user = os.getenv("SN_USER")
    password = os.getenv("SN_PASSWORD")
    if not all(v and v.strip() for v in (instance, user, password)):
        raise ConfigError(
            "SN_INSTANCE, SN_USER, and SN_PASSWORD must be set (in the "
            "environment or a .env file). now-data talks to a live instance "
            "and cannot run without credentials."
        )
    return instance_url(instance), user, password


def ensure_awake(instance_url: str, user: str, password: str, timeout_s: int = 120) -> None:
    """Block until the instance returns JSON to an authenticated API call.

    Waiting only helps a sleeping instance, so answers that waiting can't fix fail at once with
    ConfigError: a 401 (now-mcp#2), a name that doesn't resolve (#10), a certificate that doesn't
    verify. A JSON 403 means the instance is awake (the user just can't read sys_user), so it
    returns. Anything else (5xx, the HTML wake page, a timeout, a refused connection) is retried.

    Raises RuntimeError if the instance does not wake within `timeout_s`.
    """
    creds = base64.b64encode(f"{user}:{password}".encode()).decode()
    url = (
        f"{instance_url.rstrip('/')}"
        "/api/now/table/sys_user?sysparm_limit=1&sysparm_fields=sys_id"
    )
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        try:
            req = Request(
                url,
                headers={
                    "Authorization": f"Basic {creds}",
                    "Accept": "application/json",
                },
            )
            with urlopen(req, timeout=15) as resp:
                if "application/json" in resp.headers.get("Content-Type", ""):
                    return
        except HTTPError as e:  # before URLError: HTTPError is one
            if e.code == 401:
                raise ConfigError(
                    f"{instance_url} refused user {user!r} (HTTP 401): the password is wrong, or the "
                    "user can't use Basic auth over REST (when "
                    "glide.authenticate.basic_auth.restriction.enforce is true, it needs the "
                    "snc_basic_auth_api_access role)."
                ) from None
            if e.code == 403 and "application/json" in (e.headers.get("Content-Type") or ""):
                return
        except URLError as e:
            if isinstance(e.reason, socket.gaierror):
                raise ConfigError(
                    f"could not reach the instance: {urlsplit(instance_url).hostname!r} does not resolve "
                    f"({e.reason}); check SN_INSTANCE."
                ) from None
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                raise ConfigError(
                    f"the TLS connection to {instance_url} failed ({e.reason}); if a proxy inspects "
                    "TLS, its CA must be trusted: in the Windows certificate store, or in a file "
                    "named by SSL_CERT_FILE."
                ) from None
        except TimeoutError:
            pass
        time.sleep(3)

    raise RuntimeError(
        f"Instance at {instance_url} did not respond with JSON within {timeout_s}s. "
        "It may be hibernating beyond expected wake time."
    )


def get_client() -> "ServiceNowClient":
    """Return the shared ServiceNowClient, connecting (and waking) on first use."""
    global _client, _awake
    if _client is None:
        from pysnc import ServiceNowClient

        instance, user, password = _creds()
        if not _awake:
            ensure_awake(instance, user, password)
            _awake = True
        _client = ServiceNowClient(instance, (user, password))
    return _client
