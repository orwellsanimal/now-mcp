"""The now-data verbs as plain functions: no MCP import, safe under App Control.

Read verbs (`now_query`, `now_get`, `now_aggregate`) do not mutate the instance.
Write verbs (`now_update`, `now_insert`, `now_run_script`) are gated by
`confirmed`: the default (`confirmed=False`) returns a dry-run preview and
touches nothing; only `confirmed=True` performs the mutation. A confirmed
update or insert is read back, and reports what the record holds, not what was
sent (now-mcp#3).

Credentials and the hibernation guard live in `client.py`; the aggregate layer
in `aggregate.py`. Import these functions to use the verbs as a library;
`server.py` registers the same functions as MCP tools.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Callable

from now_data.aggregate import sn_aggregate
from now_data.client import get_client
from now_data.names import check_table
from now_data.query_check import QueryRefused, table_columns, unknown_fields
from now_data.query_check import check as check_request

__all__ = ["now_query", "now_get", "now_aggregate", "now_update", "now_insert", "now_run_script"]

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

    client = get_client()
    check_request(client, check_table(table), query=query, fields={"fields": field_list})
    gr = client.GlideRecord(table)
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
    client = get_client()
    check_request(client, check_table(table), fields={"fields": field_list})
    gr = client.GlideRecord(table)
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
    check_request(client, check_table(table), query=query, fields={
        "group_by": _split(group_by), "sum_fields": _split(sum_fields), "avg_fields": _split(avg_fields),
        "min_fields": _split(min_fields), "max_fields": _split(max_fields)})
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


# ── Writes are read back (now-mcp#3) ─────────────────────────────────────────
#
# ServiceNow can answer a write with success and keep something else: a business
# rule's setAbortAction, a data policy, an ACL that strips a field. whitcortex hit
# exactly that (#2611, #2627), so a confirmed write re-reads what it wrote and says
# what the record holds. Ported from whitcortex tools/servicenow.py.

# Column types whose value isn't kept on the record: a write appends a
# sys_journal_field row and the field reads back empty (work_notes and comments are
# journal_input in sys_dictionary). Told apart by the UI Meta column type that
# query_check already reads, not by name, so a custom journal field counts too.
#
# Measured on a PDI's UI Meta API (2026-10-19, now-mcp#18 review): incident's
# work_notes and comments have internal_type and type "journal_input"
# (tests/fixtures/ui_meta/incident.json).
_JOURNAL_TYPES = {"journal", "journal_input"}

# Column types whose stored value is a number: "02" given to state (integer) is
# stored as "2", which is the value asked for, not a different one.
_NUMBER_TYPES = {"integer", "longint", "decimal", "float"}
# The number forms compared as numbers; Decimal() alone also takes "inf" and "1_0".
_PLAIN_NUMBER = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


class WriteUnconfirmed(RuntimeError):
    """A write the instance may have applied, which this couldn't confirm.

    Raised once a write may have reached the instance: the connection dropped or the
    instance answered with an error pysnc doesn't give the status of, the answer had
    no sys_id, or the record couldn't be read back. Check the record before retrying:
    a blind retry can duplicate a row or a journal entry. (Not in `__all__`, which
    server.py registers as tools.)
    """


def _write(call: Callable[[], Any], what: str, advice: str) -> Any:
    """Run gr.update() / gr.insert(), keeping "refused" apart from "may have landed".

    pysnc 1.2.1 (client._validate_response) turns an HTTP 401 / 403 / 404 into
    AuthenticationException / RoleException / NotFoundException: the instance
    answered and refused, so nothing was written. Those aren't caught here and
    propagate as pysnc raised them. Every other error status, a 400 and a 5xx
    alike, becomes a bare RequestException without the status, and a connection
    dropped after the request went out is a requests exception: either way the
    write may have landed. Insert/UpdateException carry their status, so only a
    5xx of theirs counts. An answer with an empty result makes pysnc raise
    AttributeError reading its sys_id (now-mcp#18 review).
    """
    import requests  # PySNC's dependencies, imported here so verbs loads without them
    from pysnc import exceptions as px

    try:
        return call()
    except px.RestException as e:
        if e.status_code is not None and e.status_code < 500:
            raise
        # RestException puts itself in its own args; the last one is "<status> - <body>".
        cause, why = e, f"the instance answered HTTP {e.status_code} ({e.args[-1] if e.args else e})"
    except px.RequestException as e:
        cause, why = e, (f"the instance answered with an error, and pysnc doesn't say whether "
                         f"it was a 4xx (refused) or a 5xx ({e})")
    except requests.RequestException as e:
        cause, why = e, f"the connection failed after the request may have been sent ({type(e).__name__}: {e})"
    except AttributeError as e:
        cause, why = e, f"the answer had no sys_id ({e})"
    raise WriteUnconfirmed(f"{what} may have been applied: {why}. {advice}") from cause


def _is_journal(column: dict | None) -> bool:
    column = column or {}
    return column.get("internal_type") in _JOURNAL_TYPES or column.get("type") in _JOURNAL_TYPES


def _as_stored(value: Any) -> str:
    """`value` written the way the record stores it, for comparing with a read-back."""
    if value is None:
        return ""  # a null write clears the field
    if isinstance(value, bool):  # JSON true from an MCP client; the record holds 'true'
        return "true" if value else "false"
    return str(value).strip()


def _same_value(given: Any, back: Any, column: dict | None = None) -> bool:
    """The stored raw value is what was asked for.

    A date given to a Date/Time field reads back with a midnight time (task.due_date
    is Date/Time, measured on dev442835 2026-10-13; whitcortex PR #2650 review), and
    surrounding whitespace is the platform's to trim. Ported from whitcortex
    `_same_value`. Raw values, not display: a reference compares as a sys_id and a
    choice as its stored value (now-mcp#3). On a number column (`column`'s
    internal_type) the values compare as numbers, so `state="02"` stored as "2"
    is equal (now-mcp#18 review).
    """
    given, back = _as_stored(given), "" if back is None else str(back).strip()
    if back == given or back == f"{given} 00:00:00":
        return True
    if ((column or {}).get("internal_type") in _NUMBER_TYPES
            and _PLAIN_NUMBER.fullmatch(given) and _PLAIN_NUMBER.fullmatch(back)):
        return Decimal(given) == Decimal(back)
    return False


def _read_back(client: Any, table: str, sys_id: str, fields: list[str]) -> tuple[Any, str]:
    """(record, "") re-read by sys_id with `fields` as raw values, or (None, why not).

    A query, not get(): whitcortex measured real pysnc raising KeyError('result') on a
    missing record's 404 body rather than returning False (#2609). Never raises: by
    the time this runs the write was accepted, so the caller must say so, not let a
    bare error read as "nothing happened" (whitcortex PR #2650 review).
    """
    try:
        gr = client.GlideRecord(table)
        gr.fields = ["sys_id", *fields]
        gr.add_query("sys_id", sys_id)
        gr.query()
        if gr.next():
            return gr, ""
        return None, "no row came back when it was read again (it may not be readable by this account)"
    except Exception as e:
        return None, f"reading it back failed ({type(e).__name__}: {e})"


def _journal_text(text: Any) -> str:
    return ("" if text is None else str(text)).replace("\r\n", "\n").strip()


def _journal_entries(client: Any, sys_id: str, element: str, text: str) -> set[str]:
    """sys_ids of the record's `element` journal entries whose text is `text`.

    A journal field has no value to read back, so the journal is the receipt.
    Ported from whitcortex `_journal_entries` (#2609).
    """
    gr = client.GlideRecord("sys_journal_field")
    gr.fields = ["sys_id", "value"]
    gr.add_query("element_id", sys_id)
    gr.add_query("element", element)
    gr.query()
    wanted = _journal_text(text)
    found = set()
    while gr.next():
        if _journal_text(gr.get_value("value")) == wanted:
            found.add(str(gr.get_value("sys_id")))
    return found


def _journal_landed(client: Any, sys_id: str, element: str, text: str, before: set[str]) -> bool:
    """A NEW entry with `text` is in the journal: one that wasn't there before the write.

    Unconfirmed is not "failed": the journal may just be unreadable to this account,
    and the note landed (whitcortex `_write_journal`, #2609). So a read that fails
    after the write counts as unconfirmed too, rather than an exception that invites
    retrying the note.
    """
    try:
        return bool(_journal_entries(client, sys_id, element, text) - before)
    except Exception:
        return False


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

    A choice field must be given a value it stores, not its label: ServiceNow
    silently discards an out-of-list choice value and still reports success, so
    `state="In Progress"` is refused with `use state=2`. Raises
    `query_check.QueryRefused` (a ValueError) for that, and ValueError if no
    record with that sys_id exists.

    A confirmed update is **read back** (now-mcp#3). ServiceNow can report
    success and keep another value, so each requested field's stored raw value is
    compared with what was sent. `changed_fields` lists the changes that landed.
    `not_applied` lists `{field, requested, actual, reason}` for everything asked
    for that the record doesn't hold: `reason` is `"value_differs"` (written, but
    the record holds `actual`), `"unknown_field"` (not a field of the table, so
    not sent) or `"unreadable_field"` (a field of the table that isn't on the
    record as this account reads it, so not sent). `unknown_fields` still lists
    the unsent ones too. A confirmed update in which no field is on the record
    is refused with QueryRefused and writes nothing.

    `action` is `"updated"` only when `not_applied` is empty, else
    `"partially_applied"` (something landed, or may have) or `"not_applied"`.

    Journal fields (`work_notes`, `comments`) read back empty, so each is
    confirmed by a new `sys_journal_field` entry with its text instead. One that
    can't be confirmed goes in `journal_unconfirmed`, not `not_applied`: the note
    may have landed, and calling it failed invites a duplicate. So `"updated"`
    means nothing was kept back, not that everything was confirmed: read
    `journal_unconfirmed` whatever the `action`. A `note` explains `not_applied`
    or `journal_unconfirmed` when either isn't empty.

    Raises `WriteUnconfirmed` (a RuntimeError) when the write may have reached
    the instance but can't be confirmed: the connection failed or the instance
    answered with an error pysnc gives no status for, `update()` didn't return the
    record's sys_id, or the record can't be read back. Check the record before
    retrying. A 401 / 403 / 404 answer is raised as pysnc raised it: refused,
    nothing written.
    """
    client = get_client()
    check_request(client, check_table(table), values=values)
    gr = client.GlideRecord(table)
    if not gr.get(sys_id):
        raise ValueError(
            f"No {table} record with sys_id {sys_id!r}. "
            "Check the sys_id, or confirm write access to the table."
        )

    existing = gr.serialize()  # raw current values, keyed by field
    known = {f: v for f, v in values.items() if f in existing}
    unknown = sorted(f for f in values if f not in existing)
    columns = table_columns(client, table)  # read, and cached, by the check above

    # The same rule as the read-back, so a field the read-back calls unchanged
    # (a date on a Date/Time field, `true` on a boolean) isn't previewed as a change.
    changes = [
        {"field": f, "current": existing.get(f), "proposed": v}
        for f, v in known.items()
        if not _same_value(v, existing.get(f), columns.get(f))
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

    # A field missing from the record as read is a misspelling, or one this account
    # can't read (a field ACL): either way it isn't sent. With nothing left to send,
    # "updated" would report a write that never happened (now-mcp#18 review).
    unsent = {f: "unreadable_field" if f in columns else "unknown_field" for f in unknown}
    if not known:
        if not unknown:
            raise QueryRefused("nothing was written: no values were given.")
        noun, them = ("field", "it") if len(unknown) == 1 else ("fields", "them")
        raise QueryRefused(
            f"nothing was written: {table} record {sys_id} has no {noun} {', '.join(unknown)} as "
            f"this account reads it (a misspelling, or a field it can't read). Correct or remove "
            f"{them} and call again."
        )

    record_id = str(existing.get("sys_id") or sys_id)
    journal = {f for f in known if _is_journal(columns.get(f))}
    # Entries that already carry the text are snapshotted BEFORE the write, and a match
    # afterwards must be a new one: an identical older note ("customer contacted")
    # would otherwise confirm a write that never happened (whitcortex PR #2610 review).
    # Read first on purpose: an HTTP error on this read (pysnc raises on a 403) stops
    # the update before anything is sent (PR #2610 re-review). A read ACL that filters
    # rows answers 200 with none, so that write goes ahead and its note comes back as
    # journal_unconfirmed (now-mcp#18 review).
    journal_texts = {c["field"]: c["proposed"] for c in changes if c["field"] in journal}
    before = {f: _journal_entries(client, record_id, f, t) for f, t in journal_texts.items()}

    for f, v in known.items():
        gr.set_value(f, v)
    what = f"{table} {record_id}: the update"
    saved = _write(gr.update, what, "Check the record before retrying, or a journal entry may be duplicated.")
    # update() returns the sys_id it saved (whitcortex `_save`, #2609). It returns
    # something else only after a 200 whose answer lacks it, so that's unconfirmed,
    # not "not saved".
    if not saved or str(saved) != record_id:
        raise WriteUnconfirmed(
            f"{what} may have been applied: the answer didn't carry the record's sys_id "
            f"(update() returned {saved!r}). Check the record before retrying."
        )

    compare = [f for f in known if f not in journal]
    back = None
    if compare:
        back, problem = _read_back(client, table, record_id, compare)
        if back is None:
            raise WriteUnconfirmed(
                f"{table} {record_id}: the update was accepted, but {problem}, so what it "
                "changed is unconfirmed. Check the record before retrying."
            )
    not_applied = []
    for f, v in values.items():
        if f in unsent:
            not_applied.append({"field": f, "requested": v, "actual": None, "reason": unsent[f]})
        elif f in compare and not _same_value(v, back.get_value(f), columns.get(f)):
            not_applied.append({"field": f, "requested": v, "actual": back.get_value(f),
                                "reason": "value_differs"})
    journal_unconfirmed = [
        f for f, t in journal_texts.items() if not _journal_landed(client, record_id, f, t, before[f])
    ]
    missed = {d["field"] for d in not_applied} | set(journal_unconfirmed)
    changed_fields = [c["field"] for c in changes if c["field"] not in missed]

    if not not_applied:
        action = "updated"
    elif changed_fields or journal_unconfirmed:
        # An unconfirmed note may have landed, so "not_applied" (nothing landed) would
        # be a claim this can't make, and would invite retrying the note with the rest.
        action = "partially_applied"
    else:
        action = "not_applied"

    result = {
        "confirmed": True,
        "action": action,
        "table": table,
        "sys_id": sys_id,
        "changed_fields": changed_fields,
        "not_applied": not_applied,
        "journal_unconfirmed": journal_unconfirmed,
        # A field that held the value before the write but not after is in not_applied.
        "no_ops": [f for f in no_ops if f not in missed],
        "unknown_fields": unknown,
    }
    notes = []
    differs = [d["field"] for d in not_applied if d["reason"] == "value_differs"]
    if differs:
        notes.append(
            "The instance accepted the update but holds another value for " + ", ".join(differs)
            + " (see `actual`): a business rule, data policy or ACL can do that without an error."
        )
    if unsent:
        notes.append(
            "Not sent: " + ", ".join(unsent) + " (not on the record as this account reads it: "
            "a misspelling, or a field it can't read)."
        )
    if journal_unconfirmed:
        notes.append(
            f"A new {' / '.join(journal_unconfirmed)} entry with that text could not be confirmed "
            "in the record's journal (it may not be readable by this account). Check the record "
            "before retrying, or the note may be duplicated."
        )
    if notes:
        result["note"] = " ".join(notes)
    return result


def now_insert(
    table: str,
    values: dict[str, Any],
    confirmed: bool = False,
) -> dict[str, Any]:
    """Insert one record — GATED by `confirmed`.

    `values` maps field name → value. With `confirmed=False` (the default) this
    is a **dry run**: it checks `values` against the table's fields and returns a
    preview (`values`, `unknown_fields`) without touching the instance otherwise.
    Call again with `confirmed=True` to insert.

    Fields that aren't columns of the table are reported under `unknown_fields`
    in the preview, and a confirmed insert that names one is refused with
    `query_check.QueryRefused` and inserts nothing. ServiceNow silently drops a
    value for a column that doesn't exist; an update can simply be run again
    without it, but a second insert is a second row. Choice fields are checked as
    in `now_update`.

    A confirmed insert reads every value back and returns the new `sys_id` with
    `action: "inserted"`, or `"inserted_with_differences"` and `differences`
    (`{field, requested, actual}`) when the row holds something else (a business
    rule can override a value on insert). Number columns compare as numbers.
    Journal fields aren't confirmed through the journal here (they read back
    empty and show as differences): add a work note with `now_update`.

    Raises `WriteUnconfirmed` (a RuntimeError) once a row may exist: the
    connection failed or the instance answered with an error pysnc gives no status
    for, the answer had no sys_id (a read ACL can hide it), or the new row can't be
    read back, the last naming its sys_id. Don't retry the insert without checking
    the table: a second insert is a second row. A 401 / 403 / 404 answer is raised
    as pysnc raised it: refused, nothing inserted.
    """
    client = get_client()
    table = check_table(table)
    check_request(client, table, values=values)
    unknown = unknown_fields(client, table, values)

    if not confirmed:
        return {
            "confirmed": False,
            "action": "preview",
            "table": table,
            "values": values,
            "unknown_fields": unknown,
            "note": (
                "Dry run — nothing inserted. Call again with confirmed=true to insert"
                + (", after removing or correcting unknown_fields: a confirmed insert "
                   "refuses them." if unknown else ".")
            ),
        }
    if unknown:
        noun, them = ("field", "it") if len(unknown) == 1 else ("fields", "them")
        raise QueryRefused(
            f"nothing was inserted: {table} has no {noun} {', '.join(unknown)}, and ServiceNow "
            f"would create the row without {them}. Correct or remove {them} and call again."
        )

    columns = table_columns(client, table)  # read, and cached, by the checks above
    gr = client.GlideRecord(table)
    gr.initialize()
    for f, v in values.items():
        gr.set_value(f, v)
    created = _write(gr.insert, f"{table}: the insert",
                     "A row may exist: check the table before retrying, or it may be created twice.")
    if not created:
        # A 201 whose answer has no sys_id: create allowed, read filtered by an ACL. The
        # row may well exist, so this is not "not inserted" (now-mcp#18 review).
        raise WriteUnconfirmed(
            f"{table}: the insert was answered without a sys_id (insert() returned {created!r}), "
            "so a row may exist. Do not retry the insert; check the table first."
        )
    sys_id = str(created)

    # From here on the row exists, so every outcome names it: a message that reads as
    # "nothing happened" invites a retry that duplicates it (whitcortex `_insert_checked`,
    # #2611, PR #2650 review).
    back, problem = _read_back(client, table, sys_id, list(values))
    if back is None:
        raise WriteUnconfirmed(
            f"{table} {sys_id} was created (insert() returned its sys_id), but {problem}. "
            "Do not retry the insert; check the record."
        )
    differences = [
        {"field": f, "requested": v, "actual": back.get_value(f)}
        for f, v in values.items()
        if not _same_value(v, back.get_value(f), columns.get(f))
    ]
    result = {
        "confirmed": True,
        "action": "inserted_with_differences" if differences else "inserted",
        "sys_id": sys_id,
        "table": table,
        "differences": differences,
    }
    if differences:
        result["note"] = (
            f"Created as {table} {sys_id}, but it holds another value for "
            + ", ".join(d["field"] for d in differences)
            + " (see `actual`): a business rule or data policy can override a value on insert. "
            "Do not retry the insert, which would create a second row; check the record."
        )
    return result


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
