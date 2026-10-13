"""The now-config verbs as plain functions: no MCP import, safe under App Control.

Read-only lookups over an instance-config snapshot (see `loader.py`). Lookups
that are intrinsically existence-checks (`now_field_exists`, `now_role_exists`)
return `{exists: bool, ...}`; absolute lookups raise `ValueError` on miss so a
caller sees a typo as an error, not as "found but empty."

Import these directly to use the verbs as a library; `server.py` registers the
same functions as MCP tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from now_config.loader import Snapshot

__all__ = [
    "use_snapshot",
    "now_table_columns",
    "now_field_exists",
    "now_choice_values",
    "now_role_exists",
    "now_scope_info",
    "now_catalog_item",
    "now_property",
]
_snapshot: Snapshot | None = None


def use_snapshot(root: str | Path | None = None) -> None:
    """Point the verbs at a snapshot directory, or reset to auto-discovery.

    `root` is the directory that holds `schema/tables.json` (the
    `instance-config/` folder itself). `None` drops the cached snapshot so the
    next call resolves it again (`INSTANCE_CONFIG_ROOT`, then the CWD walk).
    """
    global _snapshot
    _snapshot = Snapshot(Path(root)) if root is not None else None


def _snap() -> Snapshot:
    global _snapshot
    if _snapshot is None:
        _snapshot = Snapshot()
    return _snapshot


def now_table_columns(
    table: str,
    include_inherited: bool = False,
) -> list[dict[str, Any]]:
    """Return the column dictionary for a ServiceNow table.

    Reads from the snapshot's `schema/columns.json`. Each row has
    `element`, `column_label`, `internal_type`, `max_length`, `mandatory`,
    `read_only`, `active`, `default_value`, `reference`, `scope`.

    Direct columns only by default. Pass `include_inherited=True` to walk the
    `super_class` chain and merge in columns from every ancestor table; each
    inherited row carries an `_inherited_from: <table>` key so the source is
    explicit.

    Raises ValueError if the table is not in the snapshot — treat that as a
    typo, not an empty result.
    """
    snap = _snap()
    cols = snap.columns()
    if table not in cols and table not in snap.tables():
        raise ValueError(
            f"Table {table!r} not in instance-config snapshot. "
            "Check the spelling or re-export instance-config."
        )
    out: list[dict[str, Any]] = list(cols.get(table, []))
    if include_inherited:
        seen: set[str] = {row.get("element", "") for row in out}
        for ancestor in snap.ancestor_tables(table):
            for row in cols.get(ancestor, []):
                element = row.get("element", "")
                if element in seen:
                    continue
                seen.add(element)
                out.append({**row, "_inherited_from": ancestor})
    return out


def now_field_exists(table: str, field: str) -> dict[str, Any]:
    """Check whether a field exists on a table, walking the `super_class` chain.

    Returns `{exists: True, internal_type, reference, mandatory, read_only,
    column_label, inherited_from?}` if found. `inherited_from` is the ancestor
    table name when the field comes from inheritance, or omitted when it's
    defined directly on the table.

    Returns `{exists: False, reason}` if not found. `reason` distinguishes
    "table missing" from "field missing on a known table" so the agent can
    decide whether the table name itself is wrong.
    """
    snap = _snap()
    cols = snap.columns()
    tables = snap.tables()
    if table not in cols and table not in tables:
        return {"exists": False, "reason": f"Table {table!r} not in snapshot"}

    chain = [table] + snap.ancestor_tables(table)
    for source in chain:
        for row in cols.get(source, []):
            if row.get("element") == field:
                result: dict[str, Any] = {
                    "exists": True,
                    "internal_type": row.get("internal_type"),
                    "reference": row.get("reference") or None,
                    "mandatory": row.get("mandatory", False),
                    "read_only": row.get("read_only", False),
                    "column_label": row.get("column_label"),
                }
                if source != table:
                    result["inherited_from"] = source
                return result
    return {
        "exists": False,
        "reason": (
            f"Field {field!r} not defined on {table!r} "
            f"or any ancestor in {chain[1:] if len(chain) > 1 else 'no parents'}"
        ),
    }


def now_choice_values(
    table: str,
    field: str,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    """Return valid choice `{value, label}` pairs for a `<table>.<field>` choice list.

    Active-only by default. Pass `include_inactive=True` to include retired
    choices (carries an extra `inactive` key per row).

    Walks the `super_class` chain: if `<table>.<field>` isn't in the snapshot
    directly, looks for the field on each ancestor. The response includes
    `_inherited_from: <ancestor>` in each row when the choice list is inherited.

    Raises ValueError if no ancestor in the chain defines the choice list.
    """
    snap = _snap()
    choices = snap.choices()
    chain = [table] + snap.ancestor_tables(table)
    matched_source: str | None = None
    rows: list[dict[str, Any]] = []
    for source in chain:
        key = f"{source}.{field}"
        if key in choices:
            matched_source = source
            rows = choices[key]
            break
    if matched_source is None:
        raise ValueError(
            f"No choice list for {table}.{field!r} (or any ancestor in {chain[1:] or 'no parents'}) "
            "in instance-config snapshot. Either the field has no choices, or the snapshot is stale."
        )
    out: list[dict[str, Any]] = []
    inherited = matched_source != table
    for row in rows:
        inactive = bool(row.get("inactive", False))
        if inactive and not include_inactive:
            continue
        entry: dict[str, Any] = {"value": row.get("value"), "label": row.get("label")}
        if include_inactive:
            entry["inactive"] = inactive
        if inherited:
            entry["_inherited_from"] = matched_source
        out.append(entry)
    return out


def now_role_exists(role: str) -> dict[str, Any]:
    """Check whether a role exists, returning its description if so.

    Returns `{exists: True, name, description, elevated_privilege, scope}`
    or `{exists: False}`.
    """
    roles = _snap().roles()
    rec = roles.get(role)
    if rec is None:
        return {"exists": False}
    return {
        "exists": True,
        "name": rec.get("name", role),
        "description": rec.get("description", ""),
        "elevated_privilege": rec.get("elevated_privilege", False),
        "scope": rec.get("scope", ""),
    }


def now_scope_info(scope: str) -> dict[str, Any]:
    """Look up a scope by its scope identifier (e.g. `sn_customerservice`).

    Returns `{sys_id, name, scope, version, vendor, active}`.

    Raises ValueError if the scope is not in the snapshot — likely a typo or a
    scope that isn't installed on the snapshotted instance.
    """
    scopes = _snap().scopes()
    rec = scopes.get(scope)
    if rec is None:
        raise ValueError(
            f"Scope {scope!r} not in instance-config snapshot. "
            "If you expect it to be installed, re-export instance-config."
        )
    return {
        "sys_id": rec.get("sys_id"),
        "name": rec.get("name"),
        "scope": rec.get("scope", scope),
        "version": rec.get("version"),
        "vendor": rec.get("vendor", ""),
        "active": rec.get("active", True),
    }


def now_catalog_item(sys_id_or_name: str) -> dict[str, Any]:
    """Look up a catalog item by sys_id or by exact name match.

    sys_id lookup is O(1); name lookup is a linear scan (~hundreds of items).
    Returns `{sys_id, name, short_description, active, category, price,
    recurring_price, flow_designer_flow}`.

    Raises ValueError if not found.
    """
    items = _snap().catalog_items()
    rec = items.get(sys_id_or_name)
    sys_id = sys_id_or_name
    if rec is None:
        for sid, candidate in items.items():
            if candidate.get("name") == sys_id_or_name:
                rec = candidate
                sys_id = sid
                break
    if rec is None:
        raise ValueError(
            f"No catalog item with sys_id or name {sys_id_or_name!r} in snapshot."
        )
    return {
        "sys_id": sys_id,
        "name": rec.get("name"),
        "short_description": rec.get("short_description", ""),
        "active": rec.get("active", True),
        "category": rec.get("category", ""),
        "price": rec.get("price", ""),
        "recurring_price": rec.get("recurring_price", ""),
        "flow_designer_flow": rec.get("flow_designer_flow", ""),
    }


def now_property(name: str) -> dict[str, Any]:
    """Look up a sys_properties record by name.

    Returns `{sys_id, name, value, description, type, scope}`.

    Raises ValueError if the property is not in the snapshot — useful for
    verifying that a property the agent wants to read actually exists before
    referencing it in generated code.
    """
    props = _snap().properties()
    rec = props.get(name)
    if rec is None:
        raise ValueError(
            f"Property {name!r} not in instance-config snapshot. "
            "If you expect it to be set, re-export instance-config."
        )
    return {
        "sys_id": rec.get("sys_id"),
        "name": name,
        "value": rec.get("value", ""),
        "description": rec.get("description", ""),
        "type": rec.get("type", ""),
        "scope": rec.get("scope", ""),
    }
