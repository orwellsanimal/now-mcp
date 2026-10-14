# now-data

Live-instance ServiceNow verbs over [PySNC](https://github.com/ServiceNow/PySNC): query, get, aggregate, and **gated** writes.

Usable two ways:
- **as a library** (`from now_data import verbs`). It's pure Python: `pysnc` and `python-dotenv`, with no MCP SDK.
- **as an MCP stdio server** (`now-mcp-data`), which needs the `server` extra.

> **Makes live calls with the credentials you give it.** Point it at a non-production instance unless you mean otherwise.

## Verbs

| Verb | Notes |
|---|---|
| `now_query(table, query="", fields="", limit=50, display_value=False)` | Returns `{count, truncated, records}`. `limit` is capped at 1000 and `truncated` is set without a second count call |
| `now_get(table, sys_id, fields="", display_value=False)` | Raises `ValueError` on a missing sys_id |
| `now_aggregate(table, query="", group_by="", count=True, sum_fields="", …)` | COUNT/SUM/AVG/MIN/MAX via the Stats API (see below) |
| `now_update(table, sys_id, values, confirmed=False)` | **Dry run by default**: returns `changes` / `no_ops` / `unknown_fields` and writes nothing. Writes only with `confirmed=True`. Unknown fields are never written, because ServiceNow silently ignores writes to non-existent columns |
| `now_run_script(script, confirmed=False)` | **Gated** server-side JavaScript. The default echoes the script back. `confirmed=True` needs the instance-side endpoint described below |

## Install

```bash
# library only (pure Python)
pip install "now-mcp-data @ git+https://github.com/orwellsanimal/now-mcp#subdirectory=now-data"
# with the MCP server
pip install "now-mcp-data[server] @ git+https://github.com/orwellsanimal/now-mcp#subdirectory=now-data"
```

**`now_aggregate` and PySNC.** It calls `GET /api/now/stats/{table}` over PySNC's authenticated session, so it works on released PySNC. It doesn't need `GlideAggregate` (ServiceNow/PySNC#146), which calls the same endpoint.

**Counting.** `now_query`'s `count` is the number of records *returned*, capped by `limit`. Use `now_aggregate` for a total.

**Caveat:** like the Table API, the Stats API silently drops query terms it can't apply (an unknown field, or a choice label used as a value, e.g. `state=new`) and counts as if they weren't there. Checking queries first is now-mcp#4. Until then, use stored values (`state=1`) and check field names.

## Credentials

The verbs read `SN_INSTANCE`, `SN_USER` and `SN_PASSWORD` from the environment, or from the nearest `.env` found by walking up from the CWD. Values already in the environment win. Missing values raise `now_data.client.ConfigError`.

A caller with its own credential handling can pass them explicitly instead. These take precedence over the environment:

```python
from now_data import client
client.configure("dev123", user, password)   # bare name, host, or https URL
```

`SN_INSTANCE` and `configure()` both accept a bare instance name (`dev123`), a host, or a full URL.

The first call waits for a hibernating developer instance to wake up and return JSON. It uses Basic auth, so some instances return 401 over REST even with correct credentials. That happens when `glide.authenticate.basic_auth.restriction.enforce=true`, unless the user has the `snc_basic_auth_api_access` role.

## `now_run_script` endpoint

A `confirmed=True` run POSTs `{"script": "<function body>"}` to a Scripted REST operation that this package does **not** install. It looks for a `sys_ws_definition` with `service_id = now_data` and calls its `POST /run_script`. The operation should:
- require the `admin` role,
- run the body as a function,
- return `{success, result, result_type, error?, message?}`.

If the endpoint doesn't exist, the verb returns `action: "endpoint_missing"` and executes nothing.

## As a library

```python
from now_data import verbs

verbs.now_query("incident", "active=true^priority=1", fields="number,short_description", limit=5)
preview = verbs.now_update("incident", sys_id, {"state": "2"})          # writes nothing
verbs.now_update("incident", sys_id, {"state": "2"}, confirmed=True)     # writes
```
