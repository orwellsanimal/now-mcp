"""MCP stdio server for the now-data verbs.

The verbs live in `verbs.py` and import nothing from `mcp`, so they can be used
as a library on machines where the MCP SDK's compiled dependencies are not
allowed. This module is the only place `mcp` is imported; install it with the
`server` extra (`pip install "now-data[server]"`).
"""

from __future__ import annotations

from now_data import kb, verbs

# Every module whose `__all__` is served as tools. The change verbs have their own server,
# off by default (change_server.py).
TOOL_MODULES = (verbs, kb)


def build_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("now-data")
    for module in TOOL_MODULES:
        for name in module.__all__:
            server.tool()(getattr(module, name))
    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
