"""The MCP veneer registers every verb (skipped when the `server` extra is absent)."""

import asyncio

import pytest

pytest.importorskip("mcp")

from now_config import server as config_server, verbs as config_verbs  # noqa: E402
from now_data import server as data_server, verbs as data_verbs  # noqa: E402


@pytest.mark.parametrize(
    "server_mod, verbs_mod",
    [(config_server, config_verbs), (data_server, data_verbs)],
)
def test_every_verb_is_registered(server_mod, verbs_mod):
    tools = asyncio.run(server_mod.build_server().list_tools())
    expected = {n for n in verbs_mod.__all__ if n.startswith("now_")}
    assert {t.name for t in tools} == expected
