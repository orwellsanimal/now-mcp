"""Refuse a request ServiceNow would silently get wrong, before it reaches the instance.

ServiceNow drops an encoded-query term it cannot apply and answers as if the
term were absent. Neither the Table API nor the Stats API reports it:
`nonsense_field=xyz` matches every row, and `state=new` matches none because
`state` stores `1`. The same goes for an unknown `group_by` field (one empty
group holding every row), and for a choice field written with a label.

So every field a request names is checked against the table's columns from the
UI Meta API (`/api/now/ui/meta/<table>`: inherited columns and choice lists in
one read), and a choice field must be compared with values it stores. What
can't be checked is refused: an unchecked figure is the confident wrong number
this exists to stop.

Ported from whitcortex (#2623 / PR #2626, ADR-0134 there), where each rule was
measured against a developer instance before it was written. Case-insensitive
string choices and numeric equality (`state=01`) describe that instance's
database collation (hosted, MySQL-family), not a ServiceNow guarantee.

Known limits (as in ADR-0134): operators match as prefixes, so a misspelling
that starts with a real operator (`stateLIKES`) passes; a retired choice
(absent from the UI Meta list) is refused though rows may hold it; catalog
`variables.*` aren't columns and are refused; operators outside `= != IN NOT IN
< <= > >=` and the text ones aren't value-checked.
"""

from __future__ import annotations

import re
from typing import Any

# (connect, read) seconds; PySNC's session sets no timeout.
TIMEOUT = (10, 60)


class QueryRefused(ValueError):
    """The request names something ServiceNow would silently drop or misread."""


class FieldsUnreadable(RuntimeError):
    """The table's fields couldn't be read, so the request can't be checked."""


_COLUMNS: dict[tuple[str, str], dict] = {}

# Encoded-query terms that are not "field<operator>value".
_SKIP_TERMS = {"", "EQ"}
_FIELD_ONLY_PREFIXES = ("ORDERBYDESC", "ORDERBY", "GROUPBY")
# ServiceNow field names are lowercase; matching only lowercase is what separates the
# field from an operator written straight after it ('stateIN1,2').
_FIELD_RE = re.compile(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*")
_TEXT_SEARCH = "123TEXTQUERY321"
# '^^' is a literal caret inside a value, not a separator (measured: LIKEnetwork^^active=true
# counted 0, LIKEnetwork^active=true counted 2).
_TERM_SPLIT = re.compile(r"(?<!\^)\^(?!\^)")
# Operators whose values are checked on a choice field.
_VALUE_OPERATORS = ("NOT IN", "IN", "!=", ">=", "<=", "=", ">", "<")
_TEXT_OPERATORS = ("NOT LIKE", "LIKE", "STARTSWITH", "ENDSWITH")
# Every operator ServiceNow APPLIES; a term with anything else is dropped silently
# (measured: active=true^state~1, ^stateEQUALS1 and a bare ^active all counted the same
# as active=true). Each of these was measured to filter (or to error).
_KNOWN_OPERATORS = tuple(sorted(_VALUE_OPERATORS + _TEXT_OPERATORS + (
    "ISEMPTY", "ISNOTEMPTY", "ANYTHING", "EMPTYSTRING", "BETWEEN", "ON", "NOTON",
    "RELATIVEGT", "RELATIVELT", "RELATIVEGE", "RELATIVELE", "RELATIVEEE", "MORETHAN",
    "LESSTHAN", "SAMEAS", "NSAMEAS", "GT_FIELD", "LT_FIELD", "GT_OR_EQUALS_FIELD",
    "LT_OR_EQUALS_FIELD", "DYNAMIC", "DATEPART", "INSTANCEOF", "NOT INSTANCEOF",
    "MATCH_PAT", "MATCH_RGX"), key=len, reverse=True))  # longest first: NOT INSTANCEOF before NOT IN
_RLQUERY = re.compile(r"(?:^|(?<!\^)\^)(?:NQ|OR)?(?:END)?RLQUERY")
# The number forms ServiceNow matches numerically (measured: 01, 1.0, +1, 1e0 all = 1;
# 1_0 and inf matched nothing). Python's float() takes more, so it is not the test.
_PLAIN_NUMBER = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
# Operators whose value is @-separated parts (ON / BETWEEN / RELATIVE...): matched as a
# prefix, a bare one is a misspelling (measured: stateONE counted the same as no term).
_AT_OPERATORS = ("ON", "NOTON", "BETWEEN", "DATEPART", "RELATIVEGT", "RELATIVELT",
                 "RELATIVEGE", "RELATIVELE", "RELATIVEEE", "MORETHAN", "LESSTHAN")
# Column types that cannot hold free text, so a value starting with = < > ! there is a
# repeated operator (priority==1, active==true: 0 rows). On a text field it can be the
# real value (short_descriptionLIKE< is applied, measured), so text is not checked.
_NOT_TEXT_TYPES = {"boolean", "integer", "longint", "decimal", "float", "glide_date_time",
                   "glide_date", "glide_duration", "due_date", "reference", "GUID",
                   "document_id", "domain_id"}


def table_columns(client: Any, table: str) -> dict:
    """Columns of `table` keyed by name, from /api/now/ui/meta (cached per instance and table)."""
    key = (client.instance, table)
    if key in _COLUMNS:
        return _COLUMNS[key]
    url = f"{client.instance.rstrip('/')}/api/now/ui/meta/{table}"
    try:
        resp = client.session.get(url, headers={"Accept": "application/json"}, timeout=TIMEOUT)
    except Exception as e:  # a timeout or a dropped connection: can't check, so don't count
        raise FieldsUnreadable(
            f"could not read the fields of {table!r} to check the request ({type(e).__name__}: {e}); "
            "nothing was run."
        ) from None
    if resp.status_code >= 400:
        raise FieldsUnreadable(
            f"could not read the fields of {table!r} to check the request (HTTP {resp.status_code} "
            f"from {url}: the ServiceNow user needs read access to the UI Meta API); nothing was run."
        )
    if "application/json" not in resp.headers.get("Content-Type", ""):
        raise FieldsUnreadable(
            f"reading the fields of {table!r} returned {resp.headers.get('Content-Type') or 'no content type'}, "
            "not JSON; the instance may be hibernating. Nothing was run."
        )
    columns = (resp.json().get("result") or {}).get("columns") or {}
    if not columns:
        # An unknown table answers 200 with no columns.
        raise QueryRefused(f"{table!r} is not a table on this ServiceNow instance.")
    _COLUMNS[key] = columns
    return columns


def _field_problem(client: Any, table: str, field: str) -> tuple[str, dict | None]:
    """(problem or "", column meta) for a possibly dot-walked field name."""
    current, column = table, None
    parts = field.split(".")
    for i, name in enumerate(parts):
        column = table_columns(client, current).get(name)
        if column is None:
            return f"'{name}' is not a field of {current}", None
        if i < len(parts) - 1:
            current = column.get("reference") or ""
            if not current:
                return f"'{name}' on {table} is not a reference, so '{field}' cannot dot-walk", None
    return "", column


def _value_problem(field: str, column: dict, operator: str, value: str, write: bool = False) -> str:
    """Problem with a choice field compared to (or, with `write`, set to) something it doesn't store, else ""."""
    choices = column.get("choices") or []
    if not choices or value.startswith("javascript:"):
        return ""
    numeric = column.get("internal_type") == "integer"
    as_number = {}
    if numeric:  # state=01 and state=1.0 match state=1 (measured)
        for c in choices:
            try:
                as_number[float(c.get("value", ""))] = str(c.get("value"))
            except ValueError:
                pass
    listed = ", ".join(f"{c.get('value')} ({c.get('label')})" for c in choices if c.get("value"))
    if operator in _TEXT_OPERATORS:
        # Measured: stateLIKEnew and stateLIKE1 both count 0; categoryLIKEhard counts 10.
        return (f"{field} stores numbers ({listed}), so {operator} matches nothing on it: "
                f"compare it with = or IN" if numeric else "")
    # ServiceNow trims the value and matches string choices case-insensitively (measured).
    stored = {str(c.get("value", "")).lower(): str(c.get("value", "")) for c in choices}
    by_label = {str(c.get("label", "")).strip().lower(): str(c.get("value", "")) for c in choices}
    values = value.split(",") if operator in ("IN", "NOT IN") else [value]
    fixed, labels = [], []
    for v in (v.strip() for v in values):
        if write and not v and "" not in stored:
            # In a query an empty value means "is empty"; as a write it blanks a field whose
            # choice list has no empty entry, which is rarely meant.
            return (f"{field} has no empty choice, so setting it to '' would blank it; its values "
                    f"are {listed}")
        number = float(v) if numeric and _PLAIN_NUMBER.fullmatch(v) else None
        if not v or v.lower() in stored or (number is not None and (
                number in as_number or operator in (">", ">=", "<", "<="))):
            fixed.append(v)  # an empty value is "is empty", which ServiceNow applies
            continue
        unquoted = v.strip("'\"")
        if unquoted != v and unquoted.lower() in stored:
            return (f"quotes are part of the value in an encoded query, so {field}{operator}{v} "
                    f"matches nothing: use {field}{operator}{unquoted}")
        label = unquoted.replace("_", " ").lower()
        if label not in by_label:
            return f"'{v}' is not a stored value of {field}; its values are {listed}"
        fixed.append(by_label[label])
        labels.append(f"'{by_label[label]}' for '{v}'")
    if not labels:
        return ""
    return f"{field} stores {', '.join(labels)}: use {field}{operator}{','.join(dict.fromkeys(fixed))}"


def _term_problem(client: Any, table: str, term: str) -> str:
    if term in _SKIP_TERMS:
        return ""
    term = term.removeprefix("NQ")
    for prefix in _FIELD_ONLY_PREFIXES:  # before "OR": ORDERBY starts with it
        if term.startswith(prefix):
            return _field_problem(client, table, term[len(prefix):])[0]
    term = term.removeprefix("OR")
    if term.startswith(_TEXT_SEARCH):
        return ""
    m = _FIELD_RE.match(term)
    if not m:
        return f"cannot read a field name at the start of '{term}'"
    field, rest = m.group(0), term[m.end():]
    if rest[:1].isspace() or rest[:1] in ("'", '"'):
        return (f"'{term}' has spaces or quotes around the operator; an encoded query is "
                f"written field=value with neither, e.g. state=1^active=true")
    problem, column = _field_problem(client, table, field)
    if problem:
        return problem
    operator = next((op for op in _KNOWN_OPERATORS if rest.startswith(op)), None)
    if operator is None:
        return (f"'{term}' has no known operator after {field} ({rest[:12] or 'nothing'}); "
                f"ServiceNow drops a term it cannot apply")
    value = rest[len(operator):]
    if operator in _AT_OPERATORS and "@" not in value:
        return f"'{term}' is not a valid {operator} condition (its value has no @ parts)"
    not_text = column.get("internal_type") in _NOT_TEXT_TYPES or bool(column.get("choices"))
    if not_text and value[:1] in ("=", "<", ">", "!"):
        # active==true is applied as active equals '=true': it matches nothing (measured).
        return f"'{term}' repeats its operator; write {field}{operator}{value.lstrip('=<>!')}"
    if operator in _VALUE_OPERATORS or operator in _TEXT_OPERATORS:
        return _value_problem(field, column, operator, value)
    return ""


def _problems(client: Any, table: str, query: str, fields: dict[str, list[str]],
              values: dict[str, Any]) -> list[str]:
    columns = table_columns(client, table)  # an unknown table refuses by name, not as a bare 400
    if _RLQUERY.search(query or ""):
        # Its inner terms belong to another table; checking them here would be wrong.
        return ["related-list conditions (RLQUERY...ENDRLQUERY) are not checked, so they "
                "are refused: run it without them"]
    problems = []
    for term in _TERM_SPLIT.split(query or ""):
        problem = _term_problem(client, table, term)
        if problem:
            problems.append(problem)
    for label, names in fields.items():
        for name in names:
            problem = _field_problem(client, table, name)[0]
            if problem:
                problems.append(f"{label}: {problem}")
    for name, value in values.items():
        column = columns.get(name)
        if column is not None and value is not None:
            problem = _value_problem(name, column, "=", str(value), write=True)
            if problem:
                problems.append(f"values: {problem}")
    return problems


def check(client: Any, table: str, query: str = "", fields: dict[str, list[str]] | None = None,
          values: dict[str, Any] | None = None) -> None:
    """Raise QueryRefused if ServiceNow would silently drop or misread part of this request.

    `query` is an encoded query. `fields` maps a label for the message (e.g.
    "group_by") to field names that must exist. `values` are field -> value pairs
    to be written; a choice field must get a value it stores. Unknown fields in
    `values` are left to the caller (now_update reports them itself).

    Raises FieldsUnreadable when the table's fields can't be read: fail closed.
    """
    fields, values = fields or {}, values or {}
    problems = _problems(client, table, query, fields, values)
    if problems:
        # The cache may predate a field or choice added since: re-read once before refusing.
        for key in [k for k in _COLUMNS if k[0] == client.instance]:
            _COLUMNS.pop(key, None)  # pop: a concurrent call may have dropped it already
        problems = _problems(client, table, query, fields, values)
    if problems:
        raise QueryRefused(
            "this request would give a wrong or unchecked result, so nothing was run: "
            + "; ".join(problems) + "."
        )
