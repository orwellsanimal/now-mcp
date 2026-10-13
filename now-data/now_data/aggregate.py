"""GlideAggregate convenience layer (COUNT/SUM/AVG/MIN/MAX, optionally grouped).

Delegates to `pysnc.GlideAggregate`, the upstream contribution in
ServiceNow/PySNC#146. Released PySNC versions before it don't have it;
`verbs.now_aggregate` checks and raises a clear error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    # Type-only — pysnc is never imported at module load so the server can list
    # tools without it. `client.GlideAggregate(...)` is a method on the live
    # client, not this class; we only reference these names in annotations.
    from pysnc import ServiceNowClient
    from pysnc.aggregate import GlideAggregate


def _split_fields(csv: str) -> list[str]:
    return [f.strip() for f in csv.split(",") if f.strip()]


def _row_group_key(row: "GlideAggregate", group_fields: list[str], display_value: bool) -> str:
    parts = []
    for f in group_fields:
        v = row.get_display_value(f) if display_value else row.get_value(f)
        parts.append(v or "(empty)")
    return ", ".join(parts)


def sn_aggregate(
    client: ServiceNowClient,
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
    if not count and not any([sum_fields, avg_fields, min_fields, max_fields]):
        raise ValueError(
            "At least one aggregate required — set count=True or specify "
            "sum_fields, avg_fields, min_fields, or max_fields."
        )

    group_fields = _split_fields(group_by)
    field_aggs = [
        ("avg", avg_fields),
        ("sum", sum_fields),
        ("min", min_fields),
        ("max", max_fields),
    ]

    ga = client.GlideAggregate(table)
    if count:
        ga.add_aggregate("COUNT")
    for api_key, csv in field_aggs:
        for f in _split_fields(csv):
            ga.add_aggregate(api_key.upper(), f)
    if query:
        ga.add_encoded_query(query)
    for f in group_fields:
        ga.group_by(f)
    if group_fields and display_value:
        ga.set_display_value(True)
    ga.query()

    def parse_row(row: GlideAggregate) -> dict:
        parsed: dict = {}
        if count:
            try:
                parsed["count"] = int(row.get_aggregate("COUNT"))
            except (TypeError, ValueError):
                parsed["count"] = row.get_aggregate("COUNT")
        for api_key, csv in field_aggs:
            fields = _split_fields(csv)
            if fields:
                parsed[api_key] = {f: row.get_aggregate(api_key.upper(), f) for f in fields}
        return parsed

    if not group_fields:
        for row in ga:
            return parse_row(row)
        return {}

    grouped: dict[str, dict] = {}
    for row in ga:
        grouped[_row_group_key(row, group_fields, display_value)] = parse_row(row)
    return grouped
