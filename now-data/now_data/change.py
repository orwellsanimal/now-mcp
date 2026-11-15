"""Change-request verbs for AI-agent accounts, off unless NOW_CHANGE_ENABLED is set (now-mcp#22).

An agent opens and runs its changes through these verbs, not raw Table API writes.
The instance's change guard still decides what is allowed: every write is asked of
the guard first (its `check` resource runs the same rules as its business rules,
without writing), and a refusal comes back with the guard's reasons. A refused
Table API write returns a bare 403, so this is how an agent learns why.

Feature flag: `NOW_CHANGE_ENABLED=1` (the environment or the nearest `.env`). Unset,
every verb raises `ChangeVerbsDisabled` and `change_server` registers no tools.

Write verbs are gated by `confirmed` like now-data's: `confirmed=False` (the default)
asks the guard and writes nothing. A confirmed write goes through `now_insert` /
`now_update`, so it is read back and reports what the record holds.

The guard's base path comes from NOW_CHANGE_GUARD_URI (e.g. `/api/x_acme_chg/guard`),
or else from `sys_ws_definition` (a Scripted REST API named "Agent Change Guard",
service_id `guard`), which an ordinary agent account may not read. The change models
are found in `chg_model` by name, so nothing here names one instance. Like `now_data.verbs`, this imports nothing from
`mcp`.
"""

from __future__ import annotations

import os
import re
from typing import Any

from dotenv import find_dotenv, load_dotenv

from now_data import verbs
from now_data.client import get_client

__all__ = [
    "now_change_policy",
    "now_change_get",
    "now_change_preview",
    "now_change_create_standard",
    "now_change_create_normal",
    "now_change_move",
]

FLAG = "NOW_CHANGE_ENABLED"
GUARD_NAME = "Agent Change Guard"
GUARD_SERVICE_ID = "guard"
# The guard's base path, for accounts that can't read sys_ws_definition.
GUARD_URI = "NOW_CHANGE_GUARD_URI"

# change_request.state values on the stock change models.
STATES = {
    "new": "-5",
    "assess": "-4",
    "authorize": "-3",
    "scheduled": "-2",
    "implement": "-1",
    "review": "0",
    "closed": "3",
    "canceled": "4",
}
_STATE_NAMES = {v: k for k, v in STATES.items()}

# What now_change_get reads, and what a write may name. The guard reads the rest itself.
CHANGE_FIELDS = [
    "number", "short_description", "type", "chg_model", "state", "approval", "cmdb_ci",
    "assignment_group", "opened_by", "start_date", "end_date", "std_change_producer_version",
    "justification", "implementation_plan", "backout_plan", "test_plan", "close_code", "close_notes",
]
_SYS_ID = re.compile(r"[0-9a-f]{32}")


class ChangeVerbsDisabled(RuntimeError):
    """The change verbs are off: set NOW_CHANGE_ENABLED=1 to turn them on."""


class GuardMissing(RuntimeError):
    """The instance has no change guard API, so nothing can be checked or written."""


def enabled() -> bool:
    """True when NOW_CHANGE_ENABLED is 1 / true / yes (the environment wins over `.env`)."""
    load_dotenv(find_dotenv(usecwd=True))
    return os.getenv(FLAG, "").strip().lower() in ("1", "true", "yes")


def _require_enabled() -> None:
    if not enabled():
        raise ChangeVerbsDisabled(
            f"The change verbs are off. Set {FLAG}=1 (environment or .env) to turn them on; "
            "the instance's change guard still decides what each write may do."
        )


# Base paths found through sys_ws_definition, per instance: client.configure() can
# point the verbs at another instance, whose guard may sit under another scope.
_discovered: dict[str, str] = {}


def _guard_url(client: Any) -> str:
    """The guard API's absolute base URL.

    NOW_CHANGE_GUARD_URI (e.g. `/api/x_acme_chg/guard`) wins. Without it the URI is
    read from sys_ws_definition, which an ordinary agent account (itil) may not read:
    measured on a PDI, 2026-11-15, an itil account got 403 on it.
    """
    load_dotenv(find_dotenv(usecwd=True))
    given = os.getenv(GUARD_URI, "").strip()
    if given:
        if not re.fullmatch(r"/api/[a-z0-9_]+/[a-z0-9_]+", given):
            raise ValueError(f"{GUARD_URI}={given!r} is not a REST base path like /api/x_acme_chg/guard.")
        base = given
    else:
        base = _discovered.get(client.instance)
        if base is None:
            base = _discovered[client.instance] = _discover_guard(client)
    return client.instance.rstrip("/") + base


def _discover_guard(client: Any) -> str:
    from pysnc import exceptions as px

    d = client.GlideRecord("sys_ws_definition")
    d.add_query("name", GUARD_NAME)
    d.add_query("service_id", GUARD_SERVICE_ID)
    try:
        d.query()
    except px.RoleException:
        raise GuardMissing(
            f"This account can't read sys_ws_definition to find the change guard. Set {GUARD_URI} to its "
            "base path (an admin reads it from the Scripted REST API's Base API path, e.g. "
            "/api/x_acme_chg/guard). Nothing was checked or written."
        ) from None
    if not d.next() or not d.get_value("base_uri"):
        raise GuardMissing(
            f"No Scripted REST API named {GUARD_NAME!r} (service_id {GUARD_SERVICE_ID!r}) on "
            f"{client.instance}: the change guard isn't installed, so nothing was checked or written."
        )
    return d.get_value("base_uri")


def _guard(method: str, path: str, body: dict | None = None) -> dict[str, Any]:
    """Call the guard as the configured account and return its JSON `result`."""
    client = get_client()
    r = client.session.request(
        method, _guard_url(client) + path, json=body,
        headers={"Accept": "application/json"}, timeout=90,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"The change guard answered HTTP {r.status_code} to {method} {path}: {r.text[:300]}")
    return r.json().get("result", {})


def _check(values: dict[str, Any], sys_id: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {"values": values}
    if sys_id:
        body["sys_id"] = sys_id
    out = _guard("POST", "/check", body)
    return {"governed": bool(out.get("governed")), "allowed": bool(out.get("allowed")),
            "reasons": list(out.get("reasons") or [])}


def _one(table: str, query: str, fields: str) -> dict[str, Any] | None:
    rows = verbs.now_query(table, query, fields=fields, limit=1)["records"]
    return rows[0] if rows else None


def _change_id(change: str) -> str:
    """A change's sys_id, from its sys_id or its number (CHG...)."""
    change = (change or "").strip()
    if _SYS_ID.fullmatch(change):
        return change
    row = _one("change_request", f"number={change}", "sys_id") if re.fullmatch(r"[A-Z]+[0-9]+", change) else None
    if not row:
        raise ValueError(f"No change_request {change!r}: give its number (CHG0000123) or its sys_id.")
    return row["sys_id"]


def _model(name: str) -> str:
    row = _one("chg_model", f"name={name}^active=true", "sys_id")
    if not row:
        raise ValueError(f"No active change model named {name!r} on this instance.")
    return row["sys_id"]


def _refused(values: dict[str, Any], check: dict[str, Any], what: str) -> dict[str, Any]:
    return {
        "confirmed": True, "action": "refused", "values": values, "reasons": check["reasons"],
        "note": f"Nothing was written: the change guard refuses {what}. Fix what the reasons name and try again.",
    }


def _detail(e: Exception) -> str:
    """The instance's own words from a pysnc RestException (its first arg is the JSON answer)."""
    body = e.args[0] if e.args else None
    if isinstance(body, dict):
        err = body.get("error") or {}
        return str(err.get("detail") or err.get("message") or body)
    return str(e)


def _write(values: dict[str, Any], confirmed: bool, sys_id: str = "") -> dict[str, Any]:
    """Ask the guard, then (confirmed) insert or update. Shared by every write verb."""
    what = "this update" if sys_id else "this change"
    check = _check(values, sys_id)
    if not confirmed:
        return {
            "confirmed": False, "action": "preview", "values": values, **check,
            "note": ("Dry run, nothing written. The guard allows it: call again with confirmed=true."
                     if check["allowed"] else
                     "Dry run, nothing written. The guard would refuse it; see reasons."),
        }
    if not check["allowed"]:
        return _refused(values, check, what)
    from pysnc import exceptions as px

    try:
        if sys_id:
            out = verbs.now_update("change_request", sys_id, values, confirmed=True)
        else:
            out = verbs.now_insert("change_request", values, confirmed=True)
    except px.RoleException as e:
        # A 403: a business rule aborted the write, so nothing was written. Either the
        # guard (something changed since the check: a flow moved the change, an admin
        # edited the policy) or a stock rule, such as the change model's state
        # transition conditions, which the guard doesn't check.
        again = _check(values, sys_id)
        if again["reasons"]:
            return _refused(values, again, what)
        return {
            "confirmed": True, "action": "refused_by_instance", "values": values,
            "detail": _detail(e),
            "note": ("Nothing was written: the guard allows it, but the instance refused it. The detail "
                     "names the rule. For a state change, the change model's transition conditions apply "
                     "(e.g. leaving New needs assignment_group)."),
        }
    new_id = out.get("sys_id") or sys_id
    row = _one("change_request", f"sys_id={new_id}", "number,state")
    out["number"] = row and row.get("number")
    out["state"] = row and _STATE_NAMES.get(str(row.get("state")), row.get("state"))
    return out


def now_change_policy() -> dict[str, Any]:
    """What the configured account may do with changes.

    Returns the guard's answer: `governed` (False means the guard doesn't apply to
    this account), `active`, `allow_normal`, `allowed_cis` and `allowed_templates`
    (each `{sys_id, name, ...}`). Read this first: a Standard change needs one of the
    templates, and every change needs one of the CIs.
    """
    _require_enabled()
    return _guard("GET", "/policy")


def now_change_get(change: str) -> dict[str, Any]:
    """One change and its approvals, by number (CHG0000123) or sys_id.

    `state` is given by name (new, assess, authorize, scheduled, implement, review,
    closed, canceled) as well as `state_value`. `approvals` lists each approval row
    (`approver`, `state`, `created`), so an agent can see what it is waiting for.
    """
    _require_enabled()
    sys_id = _change_id(change)
    rec = verbs.now_get("change_request", sys_id, fields=",".join(["sys_id", *CHANGE_FIELDS]), display_value="all")
    state = (rec.get("state") or {}).get("value", "")
    rows = verbs.now_query(
        "sysapproval_approver", f"sysapproval={sys_id}^ORDERBYsys_created_on",
        fields="approver,state,sys_created_on", limit=100, display_value="all",
    )["records"]
    return {
        "sys_id": sys_id,
        "number": (rec.get("number") or {}).get("value"),
        "state": _STATE_NAMES.get(str(state), state),
        "state_value": state,
        "fields": rec,
        "approvals": [
            {"approver": (r.get("approver") or {}).get("display_value"),
             "state": (r.get("state") or {}).get("value"),
             "created": (r.get("sys_created_on") or {}).get("value")}
            for r in rows
        ],
    }


def now_change_preview(values: dict[str, Any], change: str = "") -> dict[str, Any]:
    """Would the guard allow this write? Writes nothing.

    `values` are change_request fields; with `change` (number or sys_id) they overlay
    that change, without it they describe a new one. Returns `{governed, allowed,
    reasons}`.
    """
    _require_enabled()
    return _check(values, _change_id(change) if change else "")


def now_change_create_standard(
    template: str,
    cmdb_ci: str,
    short_description: str = "",
    assignment_group: str = "",
    confirmed: bool = False,
) -> dict[str, Any]:
    """Open a Standard change from a pre-approved template — GATED by `confirmed`.

    `template` is a `std_change_record_producer` sys_id or exact name, from
    `now_change_policy()["allowed_templates"]`; `cmdb_ci` is a sys_id from
    `allowed_cis`. The guard fills the description and the implementation, backout
    and test plans from the template, so they can't be given here.
    `short_description` defaults to the template's. Give `assignment_group` (a
    sys_user_group sys_id): the stock change models need one to leave New.

    The change opens in New. Set its planned window and move it on with
    `now_change_move`. `confirmed=False` (the default) asks the guard and returns
    `{allowed, reasons}` without writing.
    """
    _require_enabled()
    key = template.strip()
    query = f"sys_id={key}" if _SYS_ID.fullmatch(key) else f"name={key}"
    producer = _one("std_change_record_producer", query + "^active=true",
                    "sys_id,name,current_version,short_description")
    if not producer:
        raise ValueError(f"No active standard change template {template!r}.")
    if not producer.get("current_version"):
        raise ValueError(f"Standard change template {producer['name']!r} has no current version.")
    values = {
        "type": "standard",
        "chg_model": _model("Standard"),
        "std_change_producer_version": producer["current_version"],
        "cmdb_ci": cmdb_ci,
        "short_description": short_description or producer.get("short_description") or producer["name"],
    }
    if assignment_group:
        values["assignment_group"] = assignment_group
    return _write(values, confirmed)


def now_change_create_normal(
    cmdb_ci: str,
    short_description: str,
    justification: str,
    implementation_plan: str,
    backout_plan: str,
    test_plan: str,
    assignment_group: str = "",
    confirmed: bool = False,
) -> dict[str, Any]:
    """Open a Normal change, which a person approves — GATED by `confirmed`.

    The policy must allow Normal changes (`allow_normal`), and every plan is required.
    Give `assignment_group` (a sys_user_group sys_id): the stock change models need
    one to leave New, and its members are asked to approve at Assess.
    It opens in New. Set the planned window and move it to Assess with
    `now_change_move`; the change's flows then ask for approval, and only a person's
    approval moves it on. `confirmed=False` (the default) asks the guard and writes
    nothing.
    """
    _require_enabled()
    values = {
        "type": "normal",
        "chg_model": _model("Normal"),
        "cmdb_ci": cmdb_ci,
        "short_description": short_description,
        "justification": justification,
        "implementation_plan": implementation_plan,
        "backout_plan": backout_plan,
        "test_plan": test_plan,
    }
    if assignment_group:
        values["assignment_group"] = assignment_group
    return _write(values, confirmed)


def now_change_move(
    change: str,
    to: str,
    start_date: str = "",
    end_date: str = "",
    close_code: str = "",
    close_notes: str = "",
    confirmed: bool = False,
) -> dict[str, Any]:
    """Move a change to another state — GATED by `confirmed`.

    `change` is a number or sys_id; `to` is one of new, assess, authorize,
    scheduled, implement, review, closed, canceled. `start_date` / `end_date`
    (`YYYY-MM-DD HH:MM:SS`, UTC) set the planned window. It's needed to leave New and
    fixed after. Closing needs `close_code` (e.g. successful) and `close_notes`.

    The guard decides: a Normal change goes past Authorize only once a person has
    approved it, and Implement starts only inside the window. `confirmed=False` (the
    default) asks the guard and writes nothing.
    """
    _require_enabled()
    state = STATES.get((to or "").strip().lower())
    if state is None:
        raise ValueError(f"Unknown state {to!r}: use one of {', '.join(STATES)}.")
    values: dict[str, Any] = {"state": state}
    for name, value in (("start_date", start_date), ("end_date", end_date),
                        ("close_code", close_code), ("close_notes", close_notes)):
        if value:
            values[name] = value
    return _write(values, confirmed, _change_id(change))
