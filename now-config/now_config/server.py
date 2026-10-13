"""MCP stdio server for the now-config verbs.

The verbs live in `verbs.py` and import nothing from `mcp`, so they can be used
as a library on machines where the MCP SDK's compiled dependencies are not
allowed. This module is the only place `mcp` is imported; install it with the
`server` extra (`pip install "now-config[server]"`).
"""

from __future__ import annotations

from now_config import verbs


def build_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("now-config")
    for name in verbs.__all__:
        if name.startswith("now_"):
            server.tool()(getattr(verbs, name))
    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
