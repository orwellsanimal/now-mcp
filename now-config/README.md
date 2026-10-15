# now-config

Read-only ServiceNow verbs over an **instance-config snapshot**: a set of JSON files exported from an instance (tables, columns, choices, properties, scopes, roles, catalog items). It needs no credentials and makes no instance calls. Use it to check that a table, field, choice value or role really exists before generating code that names it.

Usable two ways:
- **as a library** (`from now_config import verbs`). This is stdlib only, with no MCP SDK and no compiled dependencies.
- **as an MCP stdio server** (`now-mcp-config`), which needs the `server` extra.

## Verbs

| Verb | Returns |
|---|---|
| `now_table_columns(table, include_inherited=False)` | column rows; inherited rows carry `_inherited_from` |
| `now_field_exists(table, field)` | `{exists, internal_type, reference, mandatory, read_only, column_label, inherited_from?}` or `{exists: False, reason}` |
| `now_choice_values(table, field, include_inactive=False)` | `[{value, label}]`, walking `super_class` |
| `now_role_exists(role)` | `{exists, name, description, elevated_privilege, scope}` |
| `now_scope_info(scope)` | `{sys_id, name, scope, version, vendor, active}` |
| `now_catalog_item(sys_id_or_name)` | catalog item fields |
| `now_property(name)` | `sys_properties` record |

**Errors.** The `*_exists` verbs return `{exists: False, …}` on a miss. The absolute lookups raise `ValueError`; treat that as a typo, not an empty result.

## Install

```bash
# library only (pure Python)
pip install "now-mcp-config @ git+https://github.com/orwellsanimal/now-mcp#subdirectory=now-config"
# with the MCP server
pip install "now-mcp-config[server] @ git+https://github.com/orwellsanimal/now-mcp#subdirectory=now-config"
```

## Snapshot location

1. `verbs.use_snapshot(path)` when used as a library. `path` is the directory holding `schema/tables.json`.
2. The `INSTANCE_CONFIG_ROOT` env var, set to that same directory.
3. The first ancestor of the CWD that contains `instance-config/schema/tables.json`.

The snapshot is **per instance**: choice values and custom fields drift between instances, so ground against the instance you're targeting. The file layout is in [`tests/fixtures/instance-config/`](../tests/fixtures/instance-config/).

To make one without Node, use now-data's exporter, `now-mcp-export --out DIR` ([details](../now-data/README.md#exporting-a-snapshot-for-now-config)), then point `INSTANCE_CONFIG_ROOT` at `DIR`.

## As a library

```python
from now_config import verbs

verbs.use_snapshot(r"C:\snapshots\my-instance")
verbs.now_field_exists("incident", "cmdb_ci")
# {'exists': True, 'internal_type': 'reference', 'reference': 'cmdb_ci', ..., 'inherited_from': 'task'}
```

## As an MCP server

```json
{ "mcpServers": { "now-config": { "command": "now-mcp-config", "env": { "INSTANCE_CONFIG_ROOT": "/path/to/instance-config" } } } }
```
