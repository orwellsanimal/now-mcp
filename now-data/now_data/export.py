"""Export an instance-config snapshot for now-config, in pure Python (now-mcp#14).

servicenow-coder's `node instance-config/scripts/export-instance.js` was the only way to
make a snapshot, and a machine without Node (rio's laptop) couldn't. This writes the
files `now_config.loader` reads, in the same layout and shapes:

    schema/tables.json  schema/columns.json  schema/choices.json     (required)
    platform/properties.json  platform/scopes.json                   (best-effort)
    security/roles.json                                              (best-effort)
    catalog/items.json                                               (best-effort)
    instance.json

Point `INSTANCE_CONFIG_ROOT` at the output directory. A snapshot belongs to the
instance it came from: choice values differ between instances, so export from the
instance you will ask about.

Rows are paged over the Table API on PySNC's authenticated session, not with
GlideRecord: PySNC's paging raises only on a 401, so a 403, a 5xx or the wake page
partway through would end the loop and write a short file without a word. Here every
page is checked, and an error stops the export.

Usage:  now-mcp-export --out DIR [--only schema,platform,security,catalog]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from now_data.aggregate import _error_detail
from now_data.client import get_client
from now_data.redact import redact_secret_value
from now_data.transport import connection_problem

if TYPE_CHECKING:
    from pysnc import ServiceNowClient

PAGE = 1000
# (connect, read) seconds per page. A cold sys_dictionary page can take a while.
TIMEOUT = (10, 120)
SECTIONS = ("schema", "platform", "security", "catalog")
REQUIRED = "schema"


class ExportError(RuntimeError):
    """A table couldn't be read completely; nothing was written for its section."""


def _get_page(client: "ServiceNowClient", table: str, params: dict[str, str], where: str
              ) -> tuple[list[dict], int | None]:
    import requests  # PySNC's own dependency; imported here so module load stays light

    url = f"{client.instance.rstrip('/')}/api/now/table/{table}"
    try:
        resp = client.session.get(url, params=params, headers={"Accept": "application/json"}, timeout=TIMEOUT)
    except requests.Timeout as e:
        raise ExportError(f"{table}: no answer within {TIMEOUT[1]}s {where} ({type(e).__name__}).") from None
    except requests.ConnectionError as e:
        raise ExportError(f"{table}: {connection_problem(e)}.") from None
    if resp.status_code >= 400:
        raise ExportError(f"{table}: HTTP {resp.status_code} {where}: {_error_detail(resp) or resp.reason}.")
    if "application/json" not in resp.headers.get("Content-Type", ""):
        raise ExportError(
            f"{table}: answered {resp.headers.get('Content-Type') or 'no content type'}, not JSON, {where}; "
            "the instance may be hibernating. Wake it and export again."
        )
    total = resp.headers.get("X-Total-Count")
    return resp.json().get("result", []), int(total) if total and total.isdigit() else None


def fetch_all(client: "ServiceNowClient", table: str, query: str, fields: list[str], display: bool = False
              ) -> tuple[list[dict], int | None]:
    """Every row of `table` matching `query`.

    A cell is the raw value, or {value, display_value} when `display` is set (about 4x
    slower on sys_dictionary, so only the tables that keep a label ask for it).

    Pages by keyset (`sys_id>last`, ordered by sys_id), not by offset: an offset page
    gets slower the deeper it is (sys_dictionary on a PDI: 2.7 s at 0, 14 s at 160k),
    and rows inserted mid-export can't shift a page. Returns (rows, total), where total
    is the instance's count before ACLs; fewer rows than that means ACLs hid some, and
    the caller reports it. A page that ACLs shortened is not the last page: only an
    empty page ends the loop.
    """
    base = {
        "sysparm_fields": ",".join(dict.fromkeys(["sys_id", *fields])),
        "sysparm_display_value": "all" if display else "false",
        "sysparm_exclude_reference_link": "true",
        "sysparm_limit": str(PAGE),
        "sysparm_offset": "0",
    }
    rows: list[dict] = []
    total: int | None = None
    last = ""
    while True:
        terms = [t for t in (query, f"sys_id>{last}" if last else "") if t]
        params = {**base, "sysparm_query": "^".join([*terms, "ORDERBYsys_id"])}
        page, page_total = _get_page(client, table, params, f"after sys_id {last!r}" if last else "on the first page")
        if total is None:
            total = page_total
        if not page:
            break
        rows.extend(page)
        nxt = _v(page[-1], "sys_id")
        if not nxt or nxt <= last:  # can't advance: stop loudly rather than loop or skip
            raise ExportError(f"{table}: paging stalled after sys_id {last!r} (the page's last sys_id was {nxt!r}).")
        last = nxt
    return rows, total


def _v(row: dict, field: str) -> str:
    cell = row.get(field)
    if isinstance(cell, dict):
        cell = cell.get("value")
    return "" if cell is None else str(cell)


def _d(row: dict, field: str) -> str:
    cell = row.get(field)
    if isinstance(cell, dict):
        cell = cell.get("display_value")
    return "" if cell is None else str(cell)


def _int(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        return 0


def _doc(source: str, description: str, key: str, data: Any) -> dict:
    return {"_source": source, "_description": description, key: data}


# Each export reads one table and returns {relative path: document}. The shapes match
# servicenow-coder's export-instance.js / export-runtime.py. Where the Node exporter
# kept a display value (a scope's name, a property's type label) so does this.

def _tables(rows: list[dict]) -> dict:
    tables = {}
    for r in rows:
        tables[_v(r, "name")] = {
            "sys_id": _v(r, "sys_id"),
            "label": _v(r, "label"),
            "super_class": _v(r, "super_class"),
            "scope": _v(r, "sys_scope"),
            "is_extendable": _v(r, "is_extendable") == "true",
            "number_prefix": _v(r, "number_ref"),
            "access": _v(r, "access"),
        }
    tables.pop("", None)
    return {"schema/tables.json": _doc("sys_db_object", "Tables on the instance.", "tables", tables)}


def _columns(rows: list[dict]) -> dict:
    columns: dict[str, list] = {}
    for r in rows:
        if not _v(r, "name") or not _v(r, "element"):
            continue
        columns.setdefault(_v(r, "name"), []).append({
            "element": _v(r, "element"),
            "column_label": _v(r, "column_label"),
            "internal_type": _v(r, "internal_type"),
            "max_length": _int(_v(r, "max_length")),
            "mandatory": _v(r, "mandatory") == "true",
            "read_only": _v(r, "read_only") == "true",
            "active": _v(r, "active") != "false",
            "default_value": _v(r, "default_value"),
            "reference": _v(r, "reference"),
            "scope": _v(r, "sys_scope"),
        })
    return {"schema/columns.json": _doc("sys_dictionary", "Columns/fields per table.", "columns", columns)}


def _choices(rows: list[dict]) -> dict:
    choices: dict[str, list] = {}
    for r in rows:
        choices.setdefault(f"{_v(r, 'name')}.{_v(r, 'element')}", []).append({
            "value": _v(r, "value"),
            "label": _v(r, "label"),
            "sequence": _int(_v(r, "sequence")),
            "inactive": _v(r, "inactive") == "true",
        })
    return {"schema/choices.json": _doc("sys_choice", "Choice/dropdown values per table.field.", "choices", choices)}


def _properties(rows: list[dict]) -> dict:
    properties = {}
    for r in rows:
        name = _v(r, "name")
        if not name:
            continue
        properties[name] = {
            "sys_id": _v(r, "sys_id"),
            # Both forms of the type: a label that reads differently mustn't let a password through.
            "value": redact_secret_value(name, _v(r, "value"), _v(r, "type"), _d(r, "type")),
            "description": _v(r, "description"),
            "type": _d(r, "type") or _v(r, "type") or "string",
            "scope": _d(r, "sys_scope"),
        }
    return {"platform/properties.json": _doc("sys_properties", "System properties.", "properties", properties)}


def _scopes(rows: list[dict]) -> dict:
    scopes = {}
    for r in rows:
        key = _v(r, "scope") or _v(r, "name")
        if not key:
            continue
        scopes[key] = {
            "sys_id": _v(r, "sys_id"),
            "name": _v(r, "name"),
            "scope": _v(r, "scope"),
            "version": _v(r, "version"),
            "vendor": _v(r, "vendor"),
            "active": _v(r, "active") != "false",
        }
    return {"platform/scopes.json": _doc("sys_scope", "Application scopes on the instance.", "scopes", scopes)}


def _roles(rows: list[dict]) -> dict:
    roles = {}
    for r in rows:
        name = _v(r, "name")
        if not name:
            continue
        roles[name] = {
            "sys_id": _v(r, "sys_id"),
            "name": name,
            "description": _v(r, "description"),
            "elevated_privilege": _v(r, "elevated_privilege") == "true",
            "scope": _d(r, "sys_scope"),
        }
    return {"security/roles.json": _doc("sys_user_role", "Roles defined on the instance.", "roles", roles)}


def _items(rows: list[dict]) -> dict:
    items = {}
    for r in rows:
        items[_v(r, "sys_id")] = {
            "name": _v(r, "name"),
            "short_description": _v(r, "short_description"),
            "active": _v(r, "active") == "true",
            "category": _v(r, "category"),
            "price": _v(r, "price"),
            "recurring_price": _v(r, "recurring_price"),
            "flow_designer_flow": _v(r, "flow_designer_flow"),
        }
    items.pop("", None)
    return {"catalog/items.json": _doc("sc_cat_item", "Service Catalog items on the instance.", "items", items)}


# The tables whose output keeps a display value (a scope's name, a property's type label).
DISPLAY_TABLES = frozenset({"sys_properties", "sys_user_role"})

# section -> [(table, encoded query, fields, build)]
PLAN: dict[str, list[tuple[str, str, list[str], Callable[[list[dict]], dict]]]] = {
    "schema": [
        ("sys_db_object", "",
         ["sys_id", "name", "label", "super_class", "sys_scope", "is_extendable", "number_ref", "access"], _tables),
        ("sys_dictionary", "internal_type!=collection^elementISNOTEMPTY",
         ["name", "element", "column_label", "internal_type", "max_length", "mandatory", "read_only", "active",
          "default_value", "reference", "sys_scope"], _columns),
        ("sys_choice", "inactive=false", ["name", "element", "value", "label", "sequence", "inactive"], _choices),
    ],
    "platform": [
        ("sys_properties", "", ["sys_id", "name", "value", "description", "type", "sys_scope"], _properties),
        ("sys_scope", "", ["sys_id", "name", "scope", "version", "vendor", "active"], _scopes),
    ],
    "security": [
        ("sys_user_role", "", ["sys_id", "name", "description", "elevated_privilege", "sys_scope"], _roles),
    ],
    "catalog": [
        ("sc_cat_item", "",
         ["sys_id", "name", "short_description", "active", "category", "price", "recurring_price",
          "flow_designer_flow"], _items),
    ],
}


def _write_json(path: Path, doc: dict) -> None:
    """Write via a temp file and rename, so a crash never leaves half a file in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, indent=4, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _previous_instance(out: Path) -> str | None:
    try:
        return json.loads((out / "instance.json").read_text(encoding="utf-8")).get("instance_url")
    except (OSError, ValueError, AttributeError):
        return None


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("now-mcp-data")
    except Exception:
        return "unknown"


def export_snapshot(
    out_dir: str | os.PathLike,
    only: Iterable[str] | None = None,
    client: "ServiceNowClient | None" = None,
    log: Callable[[str], None] = lambda _msg: None,
) -> dict[str, Any]:
    """Export a snapshot of the connected instance into `out_dir`.

    `only` picks sections (default: all of schema, platform, security, catalog). The
    schema section is the one now-config can't work without, so an error there raises
    ExportError; an error in any other section skips that section and is reported.

    A skipped section's file from an earlier export is kept if that export was of the
    same instance, and removed if it was of another: a missing file errors clearly,
    a file from the wrong instance answers wrongly.

    Returns {instance, out_dir, written: {path: entries}, skipped: {section: reason},
    warnings: [...]}.
    """
    sections = list(SECTIONS) if only is None else [s.strip() for s in only if s.strip()]
    unknown = [s for s in sections if s not in SECTIONS]
    if unknown:
        raise ValueError(f"unknown section(s) {unknown}; choose from {list(SECTIONS)}")
    out = Path(out_dir).expanduser().resolve()
    if client is None:
        client = get_client()
    instance = client.instance.rstrip("/")
    previous = _previous_instance(out)
    report: dict[str, Any] = {"instance": instance, "out_dir": str(out), "written": {}, "skipped": {},
                              "warnings": []}

    for section in sections:
        docs: dict[str, dict] = {}
        try:
            for table, query, fields, build in PLAN[section]:
                log(f"  {table}...")
                rows, total = fetch_all(client, table, query, fields, display=table in DISPLAY_TABLES)
                if total is not None and len(rows) < total:
                    report["warnings"].append(
                        f"{table}: read {len(rows)} of {total} rows; read ACLs hid the rest, so the snapshot "
                        "will not know about them."
                    )
                elif not rows:
                    if section == REQUIRED:  # an empty schema would answer "doesn't exist" to everything
                        raise ExportError(f"{table}: 0 rows readable. The user needs read access to it.")
                    report["warnings"].append(f"{table}: 0 rows. Usually a read ACL, not an empty table.")
                docs.update(build(rows))
        except ExportError as e:
            if section == REQUIRED:
                raise
            report["skipped"][section] = str(e)
            for _table, _q, _f, build in PLAN[section]:
                for rel in build([]):
                    stale = out / rel
                    if stale.exists() and previous != instance:
                        stale.unlink()
                        report["warnings"].append(f"removed {rel}: it came from {previous or 'an unknown instance'}.")
            continue
        exported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for rel, doc in docs.items():
            doc["_exported_at"] = exported_at
            _write_json(out / rel, doc)
            key = next(k for k in doc if not k.startswith("_"))
            report["written"][rel] = len(doc[key])

    if report["written"]:
        _write_json(out / "instance.json", {
            "instance_url": instance,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "profile": "exported",
            "exporter": f"now-mcp-data {_version()}",
        })
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="now-mcp-export",
        description="Export an instance-config snapshot for now-config from the instance in SN_INSTANCE.",
    )
    parser.add_argument("--out", required=True, help="output directory; point INSTANCE_CONFIG_ROOT at it")
    parser.add_argument("--only", default="", help=f"comma-separated sections (default all: {','.join(SECTIONS)})")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    only = args.only.split(",") if args.only else None
    try:
        report = export_snapshot(args.out, only=only, log=print)
    except (ExportError, ValueError, RuntimeError) as e:
        print(f"export failed: {e}", file=sys.stderr)
        return 1
    print(f"Exported {report['instance']} to {report['out_dir']}")
    for rel, n in report["written"].items():
        print(f"  wrote {rel} ({n} entries)")
    for section, reason in report["skipped"].items():
        print(f"  SKIPPED {section}: {reason}")
    for w in report["warnings"]:
        print(f"  warning: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
