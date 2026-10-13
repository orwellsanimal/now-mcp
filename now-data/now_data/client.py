"""Lazy ServiceNowClient singleton with a hibernation guard.

Credentials come from the same `.env` PySNC already reads (`SN_INSTANCE`,
`SN_USER`, `SN_PASSWORD`). The first connection runs `ensure_awake()` so a
freshly-hibernated developer instance is woken before any real query —
otherwise the first call gets the HTML wakeup page instead of JSON.
"""

from __future__ import annotations

import base64
import os
import time
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import find_dotenv, load_dotenv

if TYPE_CHECKING:
    from pysnc import ServiceNowClient

# pysnc is imported lazily inside get_client() so the server can start and
# advertise its tools without it (the credential-free CI smoke test). It's only
# needed once a verb actually talks to the instance.
_client: "ServiceNowClient | None" = None
_awake = False


class ConfigError(RuntimeError):
    """Raised when SN_* credentials are missing — distinct from instance errors."""


def _creds() -> tuple[str, str, str]:
    """Return (instance, user, password) from the environment / nearest .env."""
    # find_dotenv walks up from cwd; explicit env vars still win if already set.
    load_dotenv(find_dotenv(usecwd=True))
    instance = os.getenv("SN_INSTANCE")
    user = os.getenv("SN_USER")
    password = os.getenv("SN_PASSWORD")
    if not (instance and user and password):
        raise ConfigError(
            "SN_INSTANCE, SN_USER, and SN_PASSWORD must be set (in the "
            "environment or a .env file). now-data talks to a live instance "
            "and cannot run without credentials."
        )
    return instance, user, password


def ensure_awake(instance_url: str, user: str, password: str, timeout_s: int = 120) -> None:
    """Block until the instance returns JSON to an authenticated API call.

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
        except (URLError, HTTPError, TimeoutError):
            pass
        time.sleep(3)

    raise RuntimeError(
        f"Instance at {instance_url} did not respond with JSON within {timeout_s}s. "
        "It may be hibernating beyond expected wake time, or credentials may be invalid."
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
