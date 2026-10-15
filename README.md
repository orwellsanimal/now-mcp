# now-mcp

ServiceNow verbs for AI agents and scripts, packaged two ways: as plain Python functions, and as MCP stdio servers.

| Package | What it answers | Needs |
|---|---|---|
| [`now-config`](now-config/README.md) | "Does this table/field/choice/role/property exist?", from an exported **instance-config snapshot** | nothing (stdlib) |
| [`now-data`](now-data/README.md) | "What's on the instance right now?": query, get, aggregate, plus **gated** writes (`confirmed=False` is a dry run), and `now-mcp-export`, which writes the snapshot now-config reads | `pysnc`, `python-dotenv`, credentials |

## Library first, MCP optional

The verbs live in `now_config.verbs` and `now_data.verbs`. They import nothing from the MCP SDK, so the core installs are **pure Python**. That matters on locked-down machines (e.g. Windows App Control for Business), where unsigned compiled extensions such as `pydantic-core` may be blocked. CI checks this contract (`tests/test_import_safety.py`).

The MCP servers (`now-mcp-config`, `now-mcp-data`) are a thin layer over the same functions. You install them with the `server` extra.

```bash
pip install "now-mcp-data @ git+https://github.com/orwellsanimal/now-mcp#subdirectory=now-data"            # library
pip install "now-mcp-data[server] @ git+https://github.com/orwellsanimal/now-mcp#subdirectory=now-data"    # + MCP server
```

## Safety model

- `now-config` is read-only and offline.
- `now-data` read verbs never mutate the instance.
- `now_update` and `now_run_script` default to `confirmed=False`, which returns a preview and writes nothing. Your agent or harness decides when `confirmed=True` is allowed. Gate it with a human approval step.
- `now_update` never writes fields that don't exist on the record. It reports them under `unknown_fields`, because ServiceNow silently ignores writes to non-existent columns.

## Development

```bash
python -m venv .venv && . .venv/Scripts/activate   # or bin/activate
pip install -e "./now-config[server]" -e "./now-data[server]" pytest
pytest -q
```

Tests run against a synthetic snapshot and an in-memory fake client. They need no instance and no credentials.

## License

[Apache-2.0](LICENSE).
