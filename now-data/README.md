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

If the fields can't be read, the verb raises `FieldsUnreadable` (a `RuntimeError`) and runs nothing, so it fails closed.

**Access to the UI Meta API.** It's an internal UI endpoint and isn't in ServiceNow's REST API reference, so no documented role grants it. As of 0.4.0, every read verb depends on it. An identity that can read tables but gets 401/403 from UI Meta will see every verb refuse with `FieldsUnreadable`, naming the status. Check an identity before relying on it. Use the full instance URL here (`https://dev123.service-now.com`): curl doesn't expand a bare name the way now-data does, so `dev123` prints `000`.

```bash
INSTANCE=https://dev123.service-now.com
curl -s -o /dev/null -w "%{http_code}\n" -u "$SN_USER:$SN_PASSWORD" \
  -H "Accept: application/json" "$INSTANCE/api/now/ui/meta/incident"
```

`200` means the checks work for that user. Verified: an admin-level developer user and whitcortex's integration identity both get `200`. For a locked-down integration user, ask the instance admin which ACL or role opens `/api/now/ui/meta`, and record the answer here.

The rules and their measurements come from whitcortex (#2626, ADR-0134). What's accepted as applied (case-insensitive string choices, `state=01`) reflects that developer instance's collation, not a ServiceNow guarantee. Known limits: a misspelling that starts with a real operator (`stateLIKES`) passes; a retired choice (missing from UI Meta) is refused; catalog `variables.*` are refused.

## Credentials

The verbs read `SN_INSTANCE`, `SN_USER` and `SN_PASSWORD` from the environment, or from the nearest `.env` found by walking up from the CWD. Values already in the environment win. Missing values raise `now_data.client.ConfigError`.

A caller with its own credential handling can pass them explicitly instead. These take precedence over the environment:

```python
from now_data import client
client.configure("dev123", user, password)   # bare name, host, or https URL
```

`SN_INSTANCE` and `configure()` both accept a bare instance name (`dev123`), a host, or a full URL.

The first call waits (up to 120 s) for a hibernating developer instance to wake up and return JSON. Answers that waiting can't fix raise `ConfigError` at once instead:

- **HTTP 401.** The password is wrong, or the user can't use Basic auth over REST. Some instances return 401 even with correct credentials: that happens when `glide.authenticate.basic_auth.restriction.enforce=true`, unless the user has the `snc_basic_auth_api_access` role.
- **A host that doesn't resolve.** Check `SN_INSTANCE`, or, when the request goes through a proxy (`HTTPS_PROXY`, or the Windows proxy settings), the proxy's host. The message names whichever one urllib was resolving.
- **A proxy that refuses the tunnel with a 4xx**, e.g. `407 Proxy Authentication Required`. Check `HTTPS_PROXY`, its credentials, and `NO_PROXY`. A 5xx from the proxy is still retried.
- **A certificate that doesn't verify**, e.g. behind a TLS-inspecting proxy. This first call uses Python's `urllib`, which trusts the Windows certificate store and `SSL_CERT_FILE`. The verbs use `requests`, which trusts certifi and `REQUESTS_CA_BUNDLE` instead, so a proxy CA may need to be in both.

A JSON 403 counts as awake: the instance answered, and the verbs report their own permissions.

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

## Exporting a snapshot for now-config

now-config answers from an instance-config snapshot. This writes one from the instance in `SN_INSTANCE`, with nothing but this package. You don't need Node or servicenow-coder's exporter.

```bat
now-mcp-export --out C:\snapshots\my-instance
set INSTANCE_CONFIG_ROOT=C:\snapshots\my-instance
```

`python -m now_data.export --out ...` runs the same thing. now-config then reads the snapshot from `INSTANCE_CONFIG_ROOT`.

```python
from now_data.export import export_snapshot
report = export_snapshot(r"C:\snapshots\my-instance")   # {written, skipped, warnings, ...}
```

- **What it writes.** It writes the files now-config reads, in the same shapes as servicenow-coder's `export-instance.js`:
  - `schema/` (tables, columns, choices);
  - `platform/` (properties, scopes);
  - `security/roles.json`;
  - `catalog/items.json`;
  - `instance.json`.

  `--only schema,platform,security,catalog` picks sections.
- **Schema is required.** If a schema table fails to read, or the user can read none of its rows, the export fails, because an empty schema would say "doesn't exist" to everything. Every other table is best-effort and stands alone: a failure skips that table's file and says why, and the rest of its section is still written (a login that can't read `sys_properties` still gets `scopes.json`). A table whose rows all exist but none is readable counts as a failure too, so a good earlier file is never replaced by an empty one. The CLI exits 1 when it wrote nothing at all (for example, `--only platform` with both of its tables refused), so a script can trust exit 0 to mean a snapshot is on disk. Progress and a successful report go to stdout. A failure, with its skips and warnings, goes to stderr.
- **All or nothing.** Everything is read before anything is written, so a failed export leaves the directory as it was. `instance.json` is removed before the writes and written last, so a run that dies while writing leaves no file claiming the directory holds one instance's snapshot.
- **Every page is checked.** PySNC's own paging raises only on a 401, so a 403, a 5xx or the wake page partway through would end with a short file and no error. This pages the Table API itself, by `sys_id`, and fails on any bad page, including one without `X-Total-Count`.
- **Rows hidden by ACLs are reported, not ignored.** For example, `sys_dictionary: read 170427 of 170762 rows`. Even an admin-level user sees this on a PDI. A snapshot can't answer about rows its user can't read.
- **A hidden page doesn't end the read.** ACLs remove rows after the database picks a page, so a page can come back empty while rows remain. The exporter steps over it with an offset inside the same `sys_id` query, and stops only when it has passed every remaining row.
- **Secrets.** Property values are redacted to `[REDACTED]` by the same three rules as servicenow-coder's `redact.js`: a password type, a credential prefix or PEM key, or a secret-looking name with an opaque value.
- **Re-exporting.** Files are replaced atomically. When the directory held another instance's export, every snapshot file this run didn't write (skipped, or left out by `--only`) is deleted. Files from servicenow-coder's Node exporter that this one doesn't write (`relationships.json`, ACLs, plugins, REST APIs) are left in place, with a warning. When it's the same instance, earlier files are kept. "Same" compares host and port, ignoring case, scheme, path and a default port, so `corp`, `corp.service-now.com:443` and `http://CORP.service-now.com/` all match.
- **Time.** On a PDI (170k dictionary rows) the full export took 7 min 49 s, nearly all of it `sys_dictionary`. A production instance's dictionary is usually larger.

A snapshot belongs to its instance: choice values differ between instances, so export from the one you'll ask about.
