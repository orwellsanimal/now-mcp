"""Change-request verbs for AI-agent accounts, off unless NOW_CHANGE_ENABLED is set (now-mcp#22).

An agent opens and runs its changes through these verbs, not raw Table API writes.
The instance's change guard still decides what is allowed: every write is asked of
the guard first (its `check` resource runs the same rules as its business rules,
without writing), and a refusal comes back with the guard's reasons. A refused
Table API write returns a bare 403, so this is how an agent learns why.

Feature flag: `NOW_CHANGE_ENABLED=1` in the process environment (never a `.env`
file). Unset, every verb raises `ChangeVerbsDisabled` and `change_server` registers
no tools.

Write verbs are gated by `confirmed` like now-data's: `confirmed=False` (the default)
asks the guard and writes nothing. A confirmed write goes through `now_insert` /
`now_update`, so it is read back and reports what the record holds. An account the
guard doesn't govern gets no writes: the guard would allow it anything.

The guard's base path comes from NOW_CHANGE_GUARD_URI (e.g. `/api/x_acme_chg/guard`,
process environment), or else from `sys_ws_definition` (a Scripted REST API named
"Agent Change Guard", service_id `guard`), which an ordinary agent account may not
read. The change models are found in `chg_model` by name, so nothing here names one
instance. Like `now_data.verbs`, this imports nothing from `mcp`.

AGENT_GUIDE is the happy path and the rules for an agent using these verbs. The MCP
server sends it as its instructions, `now_change_guide()` returns it, and the
now-data README carries the same text (a test keeps them equal).
"""

from __future__ import annotations

import os
import re
from typing import Any

from now_data import verbs
from now_data.client import get_client

__all__ = [
    "now_change_guide",
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

AGENT_GUIDE = """\
# Working with changes through now-change

The instance's change guard decides what this account may do. These tools ask it
before every write and pass its reasons back. Every write is a dry run unless
confirmed=true: read the dry run's `allowed` and `reasons` before confirming.

## Before anything

1. Call now_change_policy. `governed` must be true and `active` true. If not, stop:
   an admin has to give this account a policy, and no write will be allowed.
2. Note `allowed_cis` (every change needs one), `allowed_templates` (Standard
   changes) and `allow_normal` (whether you may open Normal changes).
3. Choose an assignment group you are NOT a member of. The stock change models need
   one to leave New, and on a Normal change its members are asked to approve.
4. Unsure whether something is allowed? now_change_preview(values, change) asks the
   guard about any write without making it.

## Happy path: Standard change (pre-approved template, no approval wait)

1. now_change_create_standard(template, cmdb_ci, assignment_group=...): dry run,
   then again with confirmed=true. Keep the returned `number`. The guard fills the
   plans from the template, so you don't give any.
2. now_change_move(number, "scheduled", start_date=..., end_date=...): the planned
   window, "YYYY-MM-DD HH:MM:SS" in UTC. It must cover the whole job and can't be
   changed after the change leaves New.
3. When the window has opened: now_change_move(number, "implement"). Then do the work,
   following the template's implementation plan.
4. now_change_move(number, "review"), then now_change_move(number, "closed",
   close_code=..., close_notes=...). close_code is successful, successful_issues or
   unsuccessful. close_notes says what was done and what was checked.

## Happy path: Normal change (a person approves)

1. now_change_create_normal(cmdb_ci, short_description, justification,
   implementation_plan, backout_plan, test_plan, assignment_group=...): dry run,
   then confirmed=true. Every plan is required. Write them for the person approving.
2. now_change_move(number, "assess", start_date=..., end_date=...). The change's
   flows now ask the assignment group to approve, then CAB. They move the change to
   Authorize and then Scheduled once people approve. You don't move it past Assess.
3. Wait. Check with now_change_get(number): `state` and `approvals` show what it is
   waiting for. Check now and then; don't poll in a tight loop.
4. Once `state` is scheduled and the window has opened, carry on as from step 3 of
   the Standard path.

## When something goes wrong

- action "refused": the guard said no. `reasons` say why. Fix what they name. A
  repeat of the same call gets the same answer.
- action "refused_by_instance": the guard allowed it, but the instance didn't.
  `note` says which kind: a business rule (often a change-model transition
  condition, e.g. no assignment group), which you can fix; or no access, which you
  can't: stop and ask a person.
- A write that raised WriteUnconfirmed, or came back with number None: it may
  have happened. Check with now_change_get, or look for your change, before trying
  again; a blind retry can open a second change.
- The work failed: back it out with the backout plan, then close with
  close_code=unsuccessful and say what happened in close_notes.
- The window passed before you could start, or the change was rejected: cancel it
  (now_change_move(number, "canceled")) and open a new one if the work is still needed.

## Never

- Never write change_request, sysapproval_approver or sysapproval_group with the
  raw Table API or a script (now_update, now_insert, now_run_script). Use these
  tools. The guard holds those writes to the same rules, but its refusals come
  back as a bare 403 with no reasons, and nothing checks the write first.
- Never approve, reject or edit an approval, and never make yourself an approver.
- Never open an Emergency change. It is always refused.
- Never try to edit a Standard change's plans, swap its template, change its CI or
  move its window after New, reopen a Canceled or Closed change, or delete a change.
- Never start Implement outside the planned window.
- Never confirm a write you haven't seen the dry run of.
"""

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

# What now_change_get reads. The write verbs build their own values.
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
    """True when NOW_CHANGE_ENABLED is 1 / true / yes in the process environment.

    Only the environment counts, never a `.env` file: a `.env` at or above the working
    directory (often a repository an agent has cloned) must not switch writes on
    against credentials it didn't supply. Set it in the MCP server's `env` block.
    """
    return os.environ.get(FLAG, "").strip().lower() in ("1", "true", "yes")


def _require_enabled() -> None:
    if not enabled():
        raise ChangeVerbsDisabled(
            f"The change verbs are off. Set {FLAG}=1 in the process environment (e.g. the MCP "
            "server's env block; a .env file doesn't count) to turn them on. The instance's change "
            "guard still decides what each write may do."
        )


# Base paths found through sys_ws_definition, per instance: client.configure() can
# point the verbs at another instance, whose guard may sit under another scope.
_discovered: dict[str, str] = {}


def _guard_url(client: Any) -> str:
    """The guard API's absolute base URL.

    NOW_CHANGE_GUARD_URI (e.g. `/api/x_acme_chg/guard`, process environment only, like
    the flag) wins, for whichever instance is configured: a caller that switches to an
    instance whose guard sits under another scope must change or unset it. Without it
    the URI is read from sys_ws_definition, which an ordinary agent account (itil) may
    not read: measured on a PDI, 2026-11-15, an itil account got 403 on it.
    """
    given = os.environ.get(GUARD_URI, "").strip()
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
    # `is True`, not bool(): a string "false" from a misbehaving endpoint must not allow a write.
    return {"governed": out.get("governed") is True, "allowed": out.get("allowed") is True,
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


NOT_GOVERNED = ("this account is not governed by the change guard, so nothing would hold it to a "
                "policy: an admin gives it a policy row (or identity_type ai_agent) first")


def _instance_refusal(values: dict[str, Any], e: Exception) -> dict[str, Any]:
    """A 403 the guard doesn't explain: a stock business rule, or no access at all."""
    detail = _detail(e)
    if "aborted by Business Rule" in detail:
        note = ("Nothing was written: the guard allows it, but a business rule on the instance aborted "
                "it; detail names the rule. For a state change that is usually the change model's "
                "transition conditions (e.g. leaving New needs assignment_group): fix that and try again.")
    else:
        note = ("Nothing was written: the instance refused it without a business rule (see detail), "
                "most likely an ACL. This account lacks access, and a retry won't help: ask an admin.")
    return {"confirmed": True, "action": "refused_by_instance", "values": values, "detail": detail, "note": note}


def _write(values: dict[str, Any], confirmed: bool, sys_id: str = "") -> dict[str, Any]:
    """Ask the guard, then (confirmed) insert or update. Shared by every write verb."""
    what = "this update" if sys_id else "this change"
    check = _check(values, sys_id)
    if not check["governed"]:
        # The guard allows everything for an account it doesn't govern. These verbs are
        # for agent accounts, so they don't write without the guard holding them.
        check = {**check, "allowed": False, "reasons": [NOT_GOVERNED, *check["reasons"]]}
    if not confirmed:
        return {
            "confirmed": False, "action": "preview", "values": values, **check,
            "note": ("Dry run, nothing written. The guard allows it: call again with confirmed=true."
                     if check["allowed"] else
                     "Dry run, nothing written. It would be refused; see reasons."),
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
        # A 403, so nothing was written: the guard (something changed since the check: a
        # flow moved the change, an admin edited the policy), a stock business rule such
        # as the change model's transition conditions, or an ACL.
        try:
            again = _check(values, sys_id)
        except Exception:  # the 403's own detail says more than a failed re-check would
            return _instance_refusal(values, e)
        if not again["allowed"]:
            return _refused(values, again, what)
        return _instance_refusal(values, e)
    # From here on the write happened, so every outcome names the record: a raise here
    # would read as "nothing happened" and invite a retry that opens a second change.
    new_id = out.get("sys_id") or sys_id
    try:
        row = _one("change_request", f"sys_id={new_id}", "number,state")
    except Exception as e:
        out["number"] = out["state"] = None
        lost = (f"The write happened (change_request {new_id}), but looking up its number and "
                f"state failed ({type(e).__name__}: {e}). Don't repeat the write: read it with "
                "now_change_get.")
        out["note"] = f"{out['note']} {lost}" if out.get("note") else lost
        return out
    out["number"] = row and row.get("number")
    out["state"] = row and _STATE_NAMES.get(str(row.get("state")), row.get("state"))
    return out


def now_change_guide() -> str:
    """How to use these tools: the happy paths, what to do when a write is refused, and
    what never to do. Read it once before the first change. It doesn't touch the instance.
    """
    _require_enabled()
    return AGENT_GUIDE


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
    check = _check(values, _change_id(change) if change else "")
    if not check["governed"]:  # the same answer the write verbs give
        check = {**check, "allowed": False, "reasons": [NOT_GOVERNED, *check["reasons"]]}
    return check


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
    # A name goes into an encoded query: ^ would add terms (^OR picks another template,
    # ^NQ drops active=true off the first clause), so it's refused, not escaped.
    if re.search(r"[\^\r\n]", key) or not key:
        raise ValueError(f"{template!r} is not a template name: use its sys_id or exact name, without ^.")
    query = f"sys_id={key}" if _SYS_ID.fullmatch(key) else f"name={key}"
    # The oldest first, so two templates with one name resolve the same way every time.
    producer = _one("std_change_record_producer", query + "^active=true^ORDERBYsys_created_on",
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
