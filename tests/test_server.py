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


@pytest.mark.parametrize("tool", ["now_query", "now_get"])
def test_the_read_tools_offer_display_value_all(tool):
    # The stated contract reaches an MCP client's schema, not only the library (now-mcp#19).
    tools = {t.name: t for t in asyncio.run(data_server.build_server().list_tools())}
    schema = tools[tool].inputSchema["properties"]["display_value"]
    assert {"type": "boolean"} in schema["anyOf"]
    assert any(s.get("const") == "all" or s.get("enum") == ["all"] for s in schema["anyOf"])


def test_change_server_offers_nothing_unless_enabled(monkeypatch, tmp_path):
    from now_data import change, change_server

    monkeypatch.chdir(tmp_path)  # no .env from the checkout
    monkeypatch.delenv(change.FLAG, raising=False)
    assert asyncio.run(change_server.build_server().list_tools()) == []
    monkeypatch.setenv(change.FLAG, "1")
    tools = asyncio.run(change_server.build_server().list_tools())
    assert {t.name for t in tools} == set(change.__all__)
