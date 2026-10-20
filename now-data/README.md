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
| `now_update(table, sys_id, values, confirmed=False)` | **Dry run by default**: returns `changes` / `no_ops` / `unknown_fields` and writes nothing. Writes only with `confirmed=True`, then **reads back** (see below). Unknown fields are never written, because ServiceNow silently ignores writes to non-existent columns |
| `now_insert(table, values, confirmed=False)` | **Dry run by default**: returns `values` / `unknown_fields` and inserts nothing. `confirmed=True` inserts one row, reads it back and returns its `sys_id`. A confirmed insert with an unknown field is refused |
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

**Requests are checked before they run.** ServiceNow silently drops an encoded-query term it can't apply and answers as if the term weren't there: `nonsense_field=xyz` matches every row, and `state=new` matches none because `state` stores `1`. So `now_query`, `now_get`, `now_aggregate`, `now_update` and `now_insert` check each request against the table's fields and choice lists, read once per table from the UI Meta API (`/api/now/ui/meta/<table>`). They raise `now_data.query_check.QueryRefused`, a `ValueError` whose message carries the fix, for:

- an unknown table, field or dot-walk step (in the query, `fields`, `group_by` or `*_fields`);
- spaces or quotes around an operator;
- a choice field given a label or an unknown value (`state stores '1' for 'new': use state=1`), including in `now_update` and `now_insert` values, where ServiceNow would discard it and still report success;
- an operator ServiceNow doesn't apply (`state~1`, a bare field);
- text operators on a number-stored choice;
- a repeated operator (`active==true`);
- a true/false field written with anything but `true` or `false` (`"1"`, `"yes"`, `"TRUE"`), in `now_update` and `now_insert` values, since what the platform stores for those hasn't been measured;
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

**Writes are read back.** ServiceNow can answer a write with success and keep another value: a business rule calls `setAbortAction`, a data policy or an ACL strips the field. So a confirmed write re-reads the record by `sys_id` and compares each value's stored raw value (a reference as a sys_id, a choice as its stored value) with what was sent.

What counts as equal:
- A date given to a Date/Time field that reads back at `00:00:00`.
- Surrounding whitespace.
- On a number column (UI Meta `internal_type` integer, longint, decimal or float), the same number written differently: `state="02"` stored as `2`.

Known limit: give a reference field a sys_id, because a display name that the instance resolves reads back as a sys_id and is reported as not applied. (A true/false field takes only `true` or `false`; anything else is refused before the write.)

- **`now_update`** returns `{confirmed, action, table, sys_id, changed_fields, not_applied, journal_unconfirmed, no_ops, unknown_fields}`, plus a `note` when `not_applied` or `journal_unconfirmed` isn't empty.
  - `changed_fields` lists the changes that **landed**.
  - `not_applied` lists `{field, requested, actual, reason}` for everything asked for that the record doesn't hold. `reason` is one of:
    - `"value_differs"`: written, but the record holds `actual`.
    - `"unknown_field"`: missing from the record as this account reads it, and not a column in the table's UI Meta columns either (read again once before labelling), so it wasn't sent.
    - `"unreadable_field"`: missing from the record as this account reads it, but a column in UI Meta, so most likely hidden by a field ACL. It wasn't sent.

    The unsent fields are in `unknown_fields` too, with `actual: null`. A confirmed update in which **no** field is on the record (a misspelt `work_note`, say) is refused with `QueryRefused` and writes nothing.
  - `action` is `"updated"` only when `not_applied` is empty. Otherwise it's `"partially_applied"` when something landed or may have, and `"not_applied"` when nothing did.
  - Journal fields (type `journal_input` or `journal`, e.g. `work_notes`, `comments`) read back empty, so each is confirmed by a **new** `sys_journal_field` entry with its text instead. The type comes from the UI Meta columns: on a PDI on 2026-10-19, incident's `work_notes` and `comments` read `internal_type` and `type` `journal_input` (`tests/fixtures/ui_meta/incident.json`). A Table API `get()` of an incident on the same PDI that day returned both keys with the value `""` to an account that can read them, so they are fields of the record as read and read back empty. The entries that already match are read before the write, so an identical older note doesn't count. A journal field that can't be confirmed goes in `journal_unconfirmed`, not `not_applied`: the note may have landed, and a retry would duplicate it.
    - If reading the journal before the write fails with an HTTP error (pysnc raises on a 403), the update raises and writes nothing.
    - A read ACL that filters the rows answers 200 with none, so the update goes ahead and the note comes back in `journal_unconfirmed`.
  - **`"updated"` means nothing was kept back, not that everything was confirmed.** It can come with a non-empty `journal_unconfirmed`, so read `journal_unconfirmed` whatever the `action`.
- **`now_insert`** returns `{confirmed, action, sys_id, table, differences}`, plus a `note` when there are differences.
  - `action` is `"inserted"`, or `"inserted_with_differences"` with `differences` as `{field, requested, actual}`.
  - Journal fields aren't confirmed through the journal on insert, so a work note given to `now_insert` is listed as a difference. Use `now_update` to add one.

**A write that can't be confirmed raises `now_data.verbs.WriteUnconfirmed`** (a `RuntimeError`). It's raised once the write may have reached the instance:
- the instance answered with an error status that pysnc 1.2.1 doesn't keep (it raises a bare `RequestException` for a 400 and a 5xx alike, so both are treated as "may have landed");
- the instance answered with a status pysnc doesn't take as success: a PATCH answered 202, or a POST answered 200 or 202 (pysnc's `UpdateException` / `InsertException`). Only a 4xx carried by those counts as refused;
- the answer wasn't JSON (a hibernating instance's HTML page, an empty 204);
- the connection broke after the request may have been sent. A connection that couldn't be opened at all is worded "most likely not sent", but it's still unconfirmed: pysnc retries a failed attempt, and an earlier one that reached the instance can't be ruled out;
- the answer had no sys_id (for an insert, a read ACL can hide it from the answer);
- the record can't be read back. For an insert, the message names the new sys_id.

Check the record before retrying: a blind retry can duplicate a row or a journal entry. A 401, 403 or 404 answer means the instance refused the write, so nothing was written. pysnc's `AuthenticationException`, `RoleException` or `NotFoundException` is raised as it is.

> **Changed in 0.6.0.** `changed_fields` used to be the pre-write diff, reported without a read-back. It now lists only what landed. `not_applied` and `journal_unconfirmed` are new, `action` is no longer always `"updated"`, and an unknown field makes it `"partially_applied"` or `"not_applied"`. Code that read `changed_fields` as "sent" should check `action` and `journal_unconfirmed`.

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
verbs.now_update("incident", sys_id, {"state": "2"}, confirmed=True)     # writes, then reads back
verbs.now_insert("label", {"name": "Router"})                            # inserts nothing
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
