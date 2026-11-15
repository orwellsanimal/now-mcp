"""MCP stdio server for the change verbs (`now-mcp-change`, now-mcp#22).

Off by default: unless NOW_CHANGE_ENABLED is set, it starts with no tools, so an
agent that has it configured can't call a change verb by accident. The verbs live
in `change.py`; this module is the only place `mcp` is imported for them.
"""

from __future__ import annotations

import sys

from now_data import change


def build_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("now-change")
    if not change.enabled():
        print(f"now-change: {change.FLAG} is not set, so no change tools are offered.", file=sys.stderr)
        return server
    for name in change.__all__:
        server.tool()(getattr(change, name))
    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
