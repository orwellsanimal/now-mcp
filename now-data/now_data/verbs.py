"""The now-data verbs as plain functions: no MCP import, safe under App Control.

Read verbs (`now_query`, `now_get`, `now_aggregate`) do not mutate the instance.
Write verbs (`now_update`, `now_run_script`) are gated by `confirmed`: the
default (`confirmed=False`) returns a dry-run preview and touches nothing; only
`confirmed=True` performs the mutation.

Credentials and the hibernation guard live in `client.py`; the aggregate layer
in `aggregate.py`. Import these functions to use the verbs as a library;
`server.py` registers the same functions as MCP tools.
"""

from __future__ import annotations

from typing import Any

from now_data.aggregate import sn_aggregate
from now_data.client import get_client

__all__ = ["now_query", "now_get", "now_aggregate", "now_update", "now_run_script"]

# Cap result-set size so a careless query can't flood agent context. Callers
# may raise `limit` up to MAX_LIMIT; anything higher is clamped (not an error).
DEFAULT_LIMIT = 50
MAX_LIMIT = 1000


def _split(csv: str | None) -> list[str]:
    if not csv:
        return []
    return [f.strip() for f in csv.split(",") if f.strip()]


def now_query(
    table: str,
    query: str = "",
    fields: str = "",
    limit: int = DEFAULT_LIMIT,
    display_value: bool = False,
) -> dict[str, Any]:
    """Query a ServiceNow table and return matching records.

    `query` is an encoded query string (e.g. `active=true^priority=1`).
    `fields` is a comma-separated allow-list; empty returns every field.
    `limit` defaults to 50 and is hard-capped at 1000 to protect agent context —
    the response reports `truncated: true` when the cap was hit.
    `display_value=True` returns human-readable labels for reference/choice
    fields instead of raw values/sys_ids.

    Returns `{count, truncated, records: [...]}`. `count` is the number of
    records **returned** (at most `limit`), not the table total: when
    `truncated` is true there are more matches and the total is unknown. Use
    `now_aggregate` to count.
    """
    effective_limit = max(1, min(limit, MAX_LIMIT))
    field_list = _split(fields)

    gr = get_client().GlideRecord(table)
    if query:
        gr.add_encoded_query(query)
    if field_list:
        gr.fields = field_list
    # Fetch one extra row to detect truncation without a second count call.
    gr.limit = effective_limit + 1
    gr.query()

    rows = gr.serialize_all(
        display_value=display_value,
        fields=field_list or None,
    )
    truncated = len(rows) > effective_limit
    rows = rows[:effective_limit]
    return {"count": len(rows), "truncated": truncated, "records": rows}


def now_get(
    table: str,
    sys_id: str,
    fields: str = "",
    display_value: bool = False,
) -> dict[str, Any]:
    """Fetch a single record by sys_id.

    `fields` is a comma-separated allow-list; empty returns every field.
    `display_value=True` returns human-readable labels for reference/choice
    fields. Raises ValueError if no record with that sys_id exists — treat that
    as a bad sys_id, not an empty result.
    """
    field_list = _split(fields)
    gr = get_client().GlideRecord(table)
    if not gr.get(sys_id):
        raise ValueError(
            f"No {table} record with sys_id {sys_id!r}. "
            "Check the sys_id, or confirm read access to the table."
        )
    return gr.serialize(display_value=display_value, fields=field_list or None)


def now_aggregate(
    table: str,
    query: str = "",
    group_by: str = "",
    count: bool = True,
    sum_fields: str = "",
    avg_fields: str = "",
    min_fields: str = "",
    max_fields: str = "",
    display_value: bool = True,
) -> dict[str, Any]:
    """Compute COUNT/SUM/AVG/MIN/MAX on a table via the Stats API (works on released PySNC).

    `query` filters (encoded query). `group_by` is a comma-separated field list;
    omit it for a single overall aggregate. `*_fields` are comma-separated
    fields for each statistic. At least one aggregate must be requested
    (`count=True` or one of the `*_fields`).

    Returns a flat dict for ungrouped queries, or `{group_key: {...}}` when
    grouped. `display_value=True` (default) labels group keys for
    reference/choice fields; set False for raw values/sys_ids.
    """
    client = get_client()
    return sn_aggregate(
        client,
        table,
        query=query,
        group_by=group_by,
        count=count,
        sum_fields=sum_fields,
        avg_fields=avg_fields,
        min_fields=min_fields,
        max_fields=max_fields,
        display_value=display_value,
    )


def now_update(
    table: str,
    sys_id: str,
    values: dict[str, Any],
    confirmed: bool = False,
) -> dict[str, Any]:
    """Update fields on a single record — GATED by `confirmed`.

    `values` maps field name → new value. With `confirmed=False` (the default)
    this is a **dry run**: it fetches the record and returns a preview of every
    field that would change (`current` vs `proposed`) without writing anything.
    Call again with `confirmed=True` to apply.

    Fields in `values` that don't exist on the record are reported under
    `unknown_fields` and are NOT written — ServiceNow silently no-ops a write to
    a non-existent column, so this surfaces typos instead of a false success
    (see the polymorphic-m2m gotcha).

    Raises ValueError if no record with that sys_id exists.
    """
    gr = get_client().GlideRecord(table)
    if not gr.get(sys_id):
        raise ValueError(
            f"No {table} record with sys_id {sys_id!r}. "
            "Check the sys_id, or confirm write access to the table."
        )

    existing = gr.serialize()  # raw current values, keyed by field
    known = {f: v for f, v in values.items() if f in existing}
    unknown = sorted(f for f in values if f not in existing)

    changes = [
        {"field": f, "current": existing.get(f), "proposed": v}
        for f, v in known.items()
        if str(existing.get(f)) != str(v)
    ]
    no_ops = sorted(f for f in known if f not in {c["field"] for c in changes})

    if not confirmed:
        return {
            "confirmed": False,
            "action": "preview",
            "table": table,
            "sys_id": sys_id,
            "changes": changes,
            "no_ops": no_ops,
            "unknown_fields": unknown,
            "note": "Dry run — nothing written. Call again with confirmed=true to apply.",
        }

    for f, v in known.items():
        gr.set_value(f, v)
    gr.update()
    return {
        "confirmed": True,
        "action": "updated",
        "table": table,
        "sys_id": sys_id,
        "changed_fields": [c["field"] for c in changes],
        "no_ops": no_ops,
        "unknown_fields": unknown,
    }


# Scripted REST endpoint that backs the confirmed execution path. It is not part
# of this package: it has to be installed on the instance (a global Scripted REST
# service with service_id `now_data` and a POST /run_script operation; see the
# README). `namespace` / `base_uri` are platform-computed, so we discover the
# live base_uri from sys_ws_definition rather than hardcoding a URL.
RUN_SCRIPT_SERVICE_ID = "now_data"
RUN_SCRIPT_PATH = "/run_script"


def _resolve_run_script_url() -> str | None:
    """Return the absolute URL of the run_script operation, or None if absent.

    Queries sys_ws_definition for our service and appends the operation path.
    Falls back to constructing the base URI from the namespace when the
    platform-computed base_uri is not (yet) populated.
    """
    client = get_client()
    definition = client.GlideRecord("sys_ws_definition")
    definition.add_query("service_id", RUN_SCRIPT_SERVICE_ID)
    definition.query()
    if not definition.next():
        return None
    base_uri = definition.get_value("base_uri")
    if not base_uri:
        namespace = definition.get_value("namespace") or "global"
        base_uri = f"/api/{namespace}/{RUN_SCRIPT_SERVICE_ID}"
    return client.instance.rstrip("/") + base_uri + RUN_SCRIPT_PATH


def now_run_script(script: str, confirmed: bool = False) -> dict[str, Any]:
    """Run server-side JavaScript on the instance — GATED.

    Highest blast radius in the family: a confirmed run executes arbitrary
    server-side code. Execution goes through a dedicated, versioned Scripted
    REST endpoint (`now_data` service, POST /run_script) rather than scraping the
    background-script processor. The endpoint is admin-gated and the
    snippet runs with the authenticated caller's roles.

    The `script` is treated as a **function body**: use `return <value>` to hand
    a JSON-serializable result back. `gs.info(...)` output goes to the system
    log, not the response.

    - `confirmed=False` (default): echoes the script back for review, executes
      nothing.
    - `confirmed=True`: POSTs the script to the endpoint and returns the
      structured result. If the endpoint is not deployed on this instance,
      returns `action: "endpoint_missing"` with import instructions — still
      nothing executed.
    """
    if not confirmed:
        return {
            "confirmed": False,
            "action": "preview",
            "script": script,
            "note": (
                "Dry run — nothing executed. Call again with confirmed=true to "
                "run this as the authenticated (admin) user."
            ),
        }

    try:
        url = _resolve_run_script_url()
    except Exception as exc:  # instance/credential errors surface as a clean status
        return {
            "confirmed": True,
            "action": "error",
            "error": type(exc).__name__,
            "message": str(exc),
        }

    if url is None:
        return {
            "confirmed": True,
            "action": "endpoint_missing",
            "note": (
                "The now_data run_script Scripted REST endpoint is not deployed "
                "on this instance, so nothing was executed. See the now-data "
                "README for what the endpoint must look like."
            ),
        }

    resp = get_client().session.post(
        url, json={"script": script}, headers={"Accept": "application/json"}
    )
    try:
        data = resp.json()
    except ValueError:
        return {
            "confirmed": True,
            "action": "error",
            "http_status": resp.status_code,
            "error": "non_json_response",
            "message": resp.text[:500],
        }

    # ServiceNow's Scripted REST API wraps the handler's return object under a
    # top-level "result" envelope, so the handler's {success, result, ...} lives
    # at data["result"], not data. Unwrap it before reading (fall back to data
    # itself in case a future transport returns the payload unwrapped).
    payload = data.get("result", data) if isinstance(data, dict) else {}

    # The endpoint wraps errors as {success: false, ...}; surface them verbatim
    # alongside the transport status so the caller sees both layers.
    return {
        "confirmed": True,
        "action": "executed" if payload.get("success") else "script_error",
        "http_status": resp.status_code,
        "success": payload.get("success"),
        "result": payload.get("result"),
        "result_type": payload.get("result_type"),
        "error": payload.get("error"),
        "message": payload.get("message"),
    }
