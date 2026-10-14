"""What a failed connection to the instance means, in words the reader can act on."""

from __future__ import annotations


def connection_problem(e: BaseException) -> str | None:
    """Describe a `requests` connection failure, or None if `e` isn't one (a timeout included).

    SSLError and ProxyError are ConnectionErrors too, but "check SN_INSTANCE" is the wrong
    advice for them: a TLS-inspecting corporate proxy shows up as CERTIFICATE_VERIFY_FAILED,
    and that line is the diagnosis, so `{e}` is kept in every arm (now-mcp#11).
    """
    import requests  # PySNC's own dependency; imported here so module load stays light

    if isinstance(e, requests.Timeout):  # ConnectTimeout is also a ConnectionError: callers word it as a timeout
        return None
    if isinstance(e, requests.exceptions.SSLError):
        return (f"the TLS connection to the instance failed ({type(e).__name__}: {e}); if a proxy inspects "
                "TLS, point REQUESTS_CA_BUNDLE at its CA certificate")
    if isinstance(e, requests.exceptions.ProxyError):
        return f"the proxy failed the connection ({type(e).__name__}: {e}); check HTTPS_PROXY and NO_PROXY"
    if isinstance(e, requests.ConnectionError):  # instance down, DNS, refused
        return (f"could not reach the instance ({type(e).__name__}: {e}); "
                "check SN_INSTANCE and that the instance is up")
    return None
