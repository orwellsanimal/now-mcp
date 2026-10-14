"""COUNT/SUM/AVG/MIN/MAX on a table, optionally grouped, via the Stats API.

Calls `GET /api/now/stats/{table}` over the authenticated PySNC session, so it
works on released PySNC. That endpoint is what PySNC's GlideAggregate (the fork
in ServiceNow/PySNC#146) calls too; the response is the same, so this needs
neither the fork nor a PySNC release that ships it (now-mcp#5).

The Stats API silently drops query terms it can't apply (an unknown field, a
choice label used as a value) and counts as if they weren't there. Checking the
query first is now-mcp#4.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Union

if TYPE_CHECKING:
    # Type-only: pysnc is never imported at module load so the server can list
    # tools without it.
    from pysnc import ServiceNowClient

_FIELD_AGGS = ("avg", "sum", "min", "max")


def _split_fields(csv: str) -> list[str]:
    return [f.strip() for f in csv.split(",") if f.strip()]


def stats_params(
    query: str,
    group_fields: list[str],
    count: bool,
    field_aggs: dict[str, list[str]],
    display_value: bool,
) -> dict[str, str]:
    """The `sysparm_*` parameters for one Stats API call."""
    params: dict[str, str] = {}
    if count:
        params["sysparm_count"] = "true"
    for agg, fields in field_aggs.items():
        if fields:
            params[f"sysparm_{agg}_fields"] = ",".join(fields)
    if group_fields:
        params["sysparm_group_by"] = ",".join(group_fields)
        if display_value:
            params["sysparm_display_value"] = "all"
    if query:
        params["sysparm_query"] = query
    return params


def _error_detail(resp: Any) -> str:
    try:
        err = resp.json().get("error") or {}
        return str(err.get("message") or err.get("detail") or "").strip()
    except Exception:
        return ""


def _get_stats(client: "ServiceNowClient", table: str, params: dict[str, str]) -> Any:
    url = f"{client.instance.rstrip('/')}/api/now/stats/{table}"
    resp = client.session.get(url, params=params, headers={"Accept": "application/json"})
    if resp.status_code in (401, 403):
        raise RuntimeError(
            f"Stats API refused the request (HTTP {resp.status_code}) for {table}: "
            f"{_error_detail(resp) or 'check the user and its roles'}."
        )
    if resp.status_code >= 400:
        # 400: a malformed request; 404: no such table. Both are the caller's to fix.
        raise ValueError(
            f"Stats API error (HTTP {resp.status_code}) for {table}: "
            f"{_error_detail(resp) or resp.reason}."
        )
    result = resp.json().get("result", {})
    # Ungrouped: {stats: {...}}. Grouped: [{stats: {...}, groupby_fields: [...]}, ...].
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return [result]
    return []


def _parse_stats(stats: dict, count: bool, field_aggs: dict[str, list[str]]) -> dict:
    parsed: dict = {}
    if count:
        raw = stats.get("count")
        try:
            parsed["count"] = int(raw)
        except (TypeError, ValueError):
            parsed["count"] = raw
    for agg, fields in field_aggs.items():
        if fields:
            nested = stats.get(agg) or {}
            parsed[agg] = {f: nested.get(f) for f in fields}
    return parsed


def _group_key(row: dict, group_fields: list[str], display_value: bool) -> str:
    by_field = {gf.get("field"): gf for gf in row.get("groupby_fields", [])}
    parts = []
    for f in group_fields:
        gf = by_field.get(f, {})
        v = gf.get("display_value", gf.get("value")) if display_value else gf.get("value")
        parts.append(v or "(empty)")
    return ", ".join(parts)


def sn_aggregate(
    client: "ServiceNowClient",
    table: str,
    query: str = "",
    group_by: str = "",
    count: bool = True,
    sum_fields: str = "",
    avg_fields: str = "",
    min_fields: str = "",
    max_fields: str = "",
    display_value: bool = True,
) -> Union[dict, dict[str, dict]]:
    """Compute count/sum/avg/min/max on a table, optionally grouped.

    Returns a flat dict for ungrouped queries, or `{group_key: {...}}` for
    grouped queries. At least one aggregate must be requested.
    """
    field_aggs = {
        "avg": _split_fields(avg_fields),
        "sum": _split_fields(sum_fields),
        "min": _split_fields(min_fields),
        "max": _split_fields(max_fields),
    }
    if not count and not any(field_aggs.values()):
        raise ValueError(
            "At least one aggregate required — set count=True or specify "
            "sum_fields, avg_fields, min_fields, or max_fields."
        )
    group_fields = _split_fields(group_by)
    rows = _get_stats(client, table, stats_params(query, group_fields, count, field_aggs, display_value))

    if not group_fields:
        return _parse_stats(rows[0].get("stats", {}), count, field_aggs) if rows else {}
    return {
        _group_key(row, group_fields, display_value): _parse_stats(row.get("stats", {}), count, field_aggs)
        for row in rows
    }
