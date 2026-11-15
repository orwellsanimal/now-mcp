"""MCP stdio server for the change verbs (`now-mcp-change`, now-mcp#22).

Off by default: unless NOW_CHANGE_ENABLED is set in the process environment, it
starts with no tools, so an agent that has it configured can't call a change verb
by accident. When it's on, the server's instructions are `change.AGENT_GUIDE`, and
`now_change_guide` returns the same text for clients that don't show instructions.
The verbs live in `change.py`; this module is the only place `mcp` is imported for
them.
"""

from __future__ import annotations

import sys

from now_data import change


def build_server():
    from mcp.server.fastmcp import FastMCP

    if not change.enabled():
        print(f"now-change: {change.FLAG} is not set, so no change tools are offered.", file=sys.stderr)
        return FastMCP("now-change")
    server = FastMCP("now-change", instructions=change.AGENT_GUIDE)
    for name in change.__all__:
        server.tool()(getattr(change, name))
    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
