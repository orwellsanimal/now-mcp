"""Checks on names the verbs put into a request before they reach the instance."""

from __future__ import annotations

import re

# ServiceNow table names: lowercase letters, digits and underscores (u_..., x_<scope>_... included).
_TABLE = re.compile(r"[a-z0-9_]+")


def check_table(table: str) -> str:
    """Return `table` if it is a plain ServiceNow table name, else raise ValueError.

    The Stats call builds its URL path from the table, so a value like
    `../table/sys_user` or `incident?sysparm_query=...` would change which endpoint
    is called, or carry a query past any check on the `query` argument.
    """
    if not isinstance(table, str) or not _TABLE.fullmatch(table):
        raise ValueError(
            f"{table!r} is not a table name: use the table's name, e.g. incident or "
            "x_acme_widget (lowercase letters, digits and underscores)."
        )
    return table
