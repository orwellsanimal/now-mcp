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

**Requests are checked before they run.** ServiceNow silently drops an encoded-query term it can't apply and answers as if the term weren't there: `nonsense_field=xyz` matches every row, and `state=new` matches none because `state` stores `1`. So `now_query`, `now_get`, `now_aggregate` and `now_update` check each request against the table's fields and choice lists, read once per table from the UI Meta API (`/api/now/ui/meta/<table>`). They raise `now_data.query_check.QueryRefused`, a `ValueError` whose message carries the fix, for:

- an unknown table, field or dot-walk step (in the query, `fields`, `group_by` or `*_fields`);
- spaces or quotes around an operator;
- a choice field given a label or an unknown value (`state stores '1' for 'new': use state=1`), including in `now_update` values, where ServiceNow would discard it and still report success;
- an operator ServiceNow doesn't apply (`state~1`, a bare field);
- text operators on a number-stored choice;
- a repeated operator (`active==true`);
- related-list conditions (`RLQUERY`), which aren't checked.

If the fields can't be read, the verb raises `FieldsUnreadable` (a `RuntimeError`) and runs nothing, so it fails closed. The user needs read access to the UI Meta API.

The rules and their measurements come from whitcortex (#2626, ADR-0134). What's accepted as applied (case-insensitive string choices, `state=01`) reflects that developer instance's collation, not a ServiceNow guarantee. Known limits: a misspelling that starts with a real operator (`stateLIKES`) passes; a retired choice (missing from UI Meta) is refused; catalog `variables.*` are refused.

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
