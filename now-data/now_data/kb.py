"""Knowledge article verbs: create a draft, then publish it the way the instance does
(servicenow-coder#282).

`now_kb_create_article` is `now_insert` on kb_knowledge with the lookups around it. It finds the
knowledge base by title or sys_id and the category by its path in that KB, and it refuses a name
that matches nothing or more than one record. The instance's insert rules make every new article
a draft ("Set article in draft state on insert").

`now_kb_publish_article` runs the platform's own publish, not a field write. The Publish UI
action calls `new global.KnowledgeUIAction().publish(gr)`. That checks
`KBKnowledge().canPublish(gr)` (a draft, and this account may contribute) and starts the KB's
publish flow (`kb_knowledge_base.kb_publish_flow`):
- "Knowledge - Instant Publish" publishes at once;
- "Knowledge - Approval Publish" moves the article to Review and asks for approval.

A Table API write of workflow_state=published would skip the approval and everything else the
flow does, versioning included. No REST API publishes (the Knowledge Management API is
read-only), so the verb makes that same call through `now_run_script`. That needs the admin-only
`now_data` run_script endpoint (see the README).

The outcome is read back from the article, not taken from the script's answer. An outcome that
can't be told apart from "the flow is still running" is reported as `unconfirmed`, never as
`not_published`, which would invite a retry. The script also refuses to start a second publish
while a flow for the article is still running, so a retry can't double it. Measured on
dev442835, 2026-11-21 (servicenow-coder#282).

Like `now_data.verbs`, this imports nothing from `mcp`.
"""

from __future__ import annotations

import re
import time
from typing import Any

from now_data import verbs

__all__ = ["now_kb_create_article", "now_kb_publish_article"]

ARTICLE_TABLE = "kb_knowledge"
_SYS_ID = re.compile(r"[0-9a-f]{32}")
_NUMBER = re.compile(r"[A-Z]+[0-9]+")

# Fields the create verb sets from its own arguments.
_ARGUMENT_FIELDS = {"kb_knowledge_base": "knowledge_base", "kb_category": "category",
                    "short_description": "short_description", "text": "text"}
# Fields the instance owns. The state and version come from the insert rules and the publish flow.
# The number comes from the number counter: a caller-chosen one could repeat another article's.
_INSTANCE_FIELDS = {"workflow_state", "published", "retired", "version", "latest", "base_version",
                    "sys_class_name", "scheduled_publish_date", "number"}

# What a KB's publish flow does to a draft, by the stock flows' names.
_FLOW_OUTCOME = {
    "Knowledge - Instant Publish": "publish it at once",
    "Knowledge - Approval Publish": "move it to Review and ask for approval; it publishes once approved",
}
# KnowledgeUIAction().publish on a kb_version 3 KB with no publish flow does nothing at all.
_NO_FLOW = ("this knowledge base has no publish flow (kb_publish_flow), and on a version 3 KB the "
            "platform's publish does nothing without one: an admin sets the KB's publish flow first")

# sys_flow_context states of a flow that hasn't finished (its sys_choice list, dev442835). The
# platform's own KBFlow.checkIfActiveFlowContextExists looks only for WAITING (an approval).
_RUNNING_FLOW_STATES = ("QUEUED", "IN_PROGRESS", "WAITING", "CONTINUE_SYNC", "PAUSED", "PAUSED_IN_DEBUG")

# How long a confirmed publish waits for the flow to move the article out of draft.
SETTLE_SECONDS = 15.0
_POLL_SECONDS = 1.5
_sleep, _clock = time.sleep, time.monotonic  # patched by the tests

_CATEGORY_BATCH = 100  # parent ids per query, so a wide tree doesn't build an overlong URL
_MAX_DEPTH = 20

# Run by `now_run_script`. The only value formatted in is a sys_id that `_checked_id` matched
# against _SYS_ID, so nothing else reaches the script. ES5: it runs in Rhino.
_PUBLISH_SCRIPT = """\
var gr = new GlideRecord('kb_knowledge');
if (!gr.get('%s')) return {found: false};
var before = gr.getValue('workflow_state');
var flows = new GlideRecord('sys_flow_context');
flows.addQuery('source_record', gr.getUniqueValue());
flows.addQuery('state', 'IN', '%s');
flows.setLimit(1);
flows.query();
if (flows.next()) return {found: true, before: before, in_flight: flows.getValue('name') + ''};
var can = !!new global.KBKnowledge().canPublish(gr);
var ran = can ? new global.KnowledgeUIAction().publish(gr) : false;
return {found: true, before: before, can_publish: can, publish_returned: ran === true};
"""


def _checked_id(value: str, what: str) -> str:
    """`value` if it is a sys_id. Every sys_id that goes into a query or the script passes here,
    including ones read from the instance, so nothing else can be formatted in."""
    if not _SYS_ID.fullmatch(value or ""):
        raise ValueError(f"{what} {value!r} is not a sys_id; nothing was run.")
    return value


def _raw(row: dict, field: str) -> str:
    """A field's stored value from a display_value="all" row."""
    v = row.get(field)
    v = v.get("value") if isinstance(v, dict) else v
    return "" if v is None else str(v)


def _label(row: dict, field: str) -> str:
    v = row.get(field)
    v = (v.get("display_value") or v.get("value")) if isinstance(v, dict) else v
    return "" if v is None else str(v)


def _kb_summary(row: dict) -> dict[str, Any]:
    flow = _label(row, "kb_publish_flow") if _raw(row, "kb_publish_flow") else ""
    return {"sys_id": _raw(row, "sys_id"), "title": _raw(row, "title"),
            "kb_version": _raw(row, "kb_version"), "publish_flow": flow or None}


_KB_FIELDS = "sys_id,title,kb_version,kb_publish_flow"


def _listing(names: list[str], cap: int = 40) -> str:
    names = sorted(set(names), key=str.casefold)
    more = f", and {len(names) - cap} more" if len(names) > cap else ""
    return "; ".join(names[:cap]) + more


def _knowledge_base(kb: str) -> dict[str, Any]:
    """The active knowledge base `kb` names, by sys_id or title. A title is matched exactly, then
    without case, and is never put in a query: a title can hold `^`, which would extend it."""
    kb = (kb or "").strip()
    if not kb:
        raise ValueError("knowledge_base is required: give the knowledge base's title or sys_id.")
    if _SYS_ID.fullmatch(kb):
        rows = verbs.now_query("kb_knowledge_base", f"sys_id={kb}^active=true", fields=_KB_FIELDS,
                               limit=1, display_value="all")["records"]
        if not rows:
            raise ValueError(f"No active knowledge base with sys_id {kb}.")
        return _kb_summary(rows[0])
    res = verbs.now_query("kb_knowledge_base", "active=true^ORDERBYtitle", fields=_KB_FIELDS,
                          limit=verbs.MAX_LIMIT, display_value="all")
    if res["truncated"]:
        # A title found among the first 1000 may still be repeated past them.
        raise ValueError("This instance has more than 1000 active knowledge bases, too many to find one "
                         "by title: give its sys_id.")
    rows = res["records"]
    matches = [r for r in rows if _raw(r, "title") == kb] or [
        r for r in rows if _raw(r, "title").casefold() == kb.casefold()]
    if not matches:
        raise ValueError(f"No active knowledge base titled {kb!r}. Active knowledge bases: "
                         f"{_listing([_raw(r, 'title') for r in rows])}.")
    if len(matches) > 1:
        raise ValueError(f"{len(matches)} active knowledge bases are titled {kb!r}: "
                         f"{', '.join(_raw(r, 'sys_id') for r in matches)}. Give the sys_id.")
    return _kb_summary(matches[0])


def _categories(kb_id: str) -> list[dict[str, Any]]:
    """Every active category in the KB, each with its path of labels from the KB down.

    kb_category is a tree: a top-level category's parent is the KB (parent_table
    kb_knowledge_base), a nested one's is a category. parent_id is a document_id, so a row is
    kept only when parent_table is the table its parent was found in.
    """
    nodes: list[dict[str, Any]] = []
    paths: dict[str, tuple[str, ...]] = {_checked_id(kb_id, "knowledge base"): ()}
    frontier, parent_table, depth = [kb_id], "kb_knowledge_base", 0
    while frontier:
        depth += 1
        found = []
        for i in range(0, len(frontier), _CATEGORY_BATCH):
            ids = ",".join(frontier[i:i + _CATEGORY_BATCH])
            res = verbs.now_query("kb_category", f"active=true^parent_table={parent_table}^parent_idIN{ids}",
                                  fields="sys_id,label,parent_id,parent_table", limit=verbs.MAX_LIMIT)
            if res["truncated"]:
                raise ValueError("This knowledge base has more than 1000 categories under one level, "
                                 "too many to search by name: give the category's sys_id.")
            found += [r for r in res["records"] if r.get("parent_table") == parent_table
                      and r.get("parent_id") in paths and r.get("sys_id") not in paths
                      and _SYS_ID.fullmatch(str(r.get("sys_id") or ""))]
        if found and depth > _MAX_DEPTH:  # a level past the limit exists; an empty one is fine
            raise ValueError(f"This knowledge base's categories are nested more than {_MAX_DEPTH} deep: "
                             "give the category's sys_id.")
        frontier = []
        for r in found:
            paths[r["sys_id"]] = paths[r["parent_id"]] + (str(r.get("label") or ""),)
            nodes.append({"sys_id": r["sys_id"], "path": paths[r["sys_id"]]})
            frontier.append(r["sys_id"])
        parent_table = "kb_category"
    return nodes


def _norm(text: str) -> str:
    return re.sub(r"\s*/\s*", "/", " ".join(text.split())).casefold()


def _path(node: dict[str, Any]) -> str:
    return " / ".join(node["path"])


def _category(kb: dict[str, Any], category: str) -> dict[str, Any]:
    """The one active category of `kb` that `category` names, by sys_id, full path
    ("Operating Systems / Windows") or label. A path and a label are matched together: "Windows"
    names both a top-level Windows and "Operating Systems / Windows", so it is refused rather than
    resolved to either."""
    nodes = _categories(kb["sys_id"])
    if _SYS_ID.fullmatch(category):
        matches = [n for n in nodes if n["sys_id"] == category]
        if not matches:
            raise ValueError(f"{category} is not an active category of the knowledge base {kb['title']!r}.")
    else:
        want = _norm(category)
        matches = [n for n in nodes if _norm(_path(n)) == want or _norm(n["path"][-1]) == want]
    if not matches:
        known = _listing([_path(n) for n in nodes]) if nodes else "none"
        raise ValueError(f"The knowledge base {kb['title']!r} has no active category {category!r}. "
                         f"Its categories: {known}.")
    if len(matches) > 1:
        listed = "; ".join(f"{_path(n)} ({n['sys_id']})" for n in matches)
        raise ValueError(f"{len(matches)} categories in {kb['title']!r} match {category!r}: "
                         f"{listed}. Give the full path or the sys_id.")
    return {"sys_id": matches[0]["sys_id"], "path": _path(matches[0])}


def _check_fields(fields: dict[str, Any]) -> None:
    problems = []
    for f in fields:
        if f in _ARGUMENT_FIELDS:
            problems.append(f"{f} (give it as the {_ARGUMENT_FIELDS[f]} argument)")
        elif f in _INSTANCE_FIELDS:
            problems.append(f"{f} (the instance sets it: a new article is a draft with its own number, "
                            "and now_kb_publish_article publishes it)")
        elif f.startswith("sys_"):
            problems.append(f"{f} (a system field)")
    if problems:
        raise ValueError("Not taken in fields: " + "; ".join(problems) + ". Nothing was written.")


def now_kb_create_article(
    knowledge_base: str,
    short_description: str,
    text: str,
    category: str = "",
    fields: dict[str, Any] | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Create a knowledge article as a draft. GATED by `confirmed`.

    `knowledge_base` is the KB's title or sys_id. `category` (optional) is a category of that KB:
    its full path ("Operating Systems / Windows"), its label, or its sys_id. A path or label that
    matches more than one category is refused, listing them. `text` is the article body as HTML.
    `fields` sets other kb_knowledge fields, e.g. `{"meta": "router, wan", "valid_to":
    "2027-12-31"}`. It can't set the number, state or version, which the instance owns.

    A name that matches nothing, or more than one record, raises ValueError naming what it found.
    Nothing is written then.

    `confirmed=False` (the default) is a dry run. It returns the resolved `knowledge_base` and
    `category`, the `values` that would be inserted, any `unknown_fields`, and the KB's
    `publish_flow`, which says what publishing will do. Call again with `confirmed=True` to
    insert. The insert goes through `now_insert`, so the row is read back: `action` is
    "inserted", or "inserted_with_differences" with `differences`, for example when the
    instance's HTML sanitiser rewrote `text`. A confirmed result adds the article's `number`,
    `workflow_state` (expect "draft") and `version`. Publish it with
    `now_kb_publish_article(number)`.

    Creates a plain kb_knowledge article. Template article classes (kb_template_*, such as Known
    Error articles) aren't covered yet.
    """
    extra = dict(fields or {})
    _check_fields(extra)
    if not (short_description or "").strip():
        raise ValueError("short_description is required: it is the article's title.")
    if not (text or "").strip():
        raise ValueError("text is required: it is the article's body, as HTML.")
    kb = _knowledge_base(knowledge_base)
    cat = _category(kb, category.strip()) if (category or "").strip() else None

    values: dict[str, Any] = {"kb_knowledge_base": kb["sys_id"], "short_description": short_description,
                              "text": text}
    if cat:
        values["kb_category"] = cat["sys_id"]
    values.update(extra)

    out = verbs.now_insert(ARTICLE_TABLE, values, confirmed=confirmed)
    out["knowledge_base"] = {"sys_id": kb["sys_id"], "title": kb["title"]}
    out["category"] = cat
    out["publish_flow"] = kb["publish_flow"]
    if not confirmed:
        return out

    try:
        row = verbs.now_get(ARTICLE_TABLE, out["sys_id"], fields="number,workflow_state,version",
                            display_value="all")
    except Exception as e:
        out["number"] = out["workflow_state"] = out["version"] = None
        lost = (f"The article was created ({ARTICLE_TABLE} {out['sys_id']}), but reading its number "
                f"failed ({type(e).__name__}: {e}). Don't create it again: read it with now_get.")
        out["note"] = f"{out['note']} {lost}" if out.get("note") else lost
        return out
    # version is a reference to kb_version: its label is "0.01", its value a sys_id.
    out["number"], out["workflow_state"], out["version"] = (
        _raw(row, "number"), _raw(row, "workflow_state"), _label(row, "version") or None)
    if out["workflow_state"] != "draft":
        odd = (f"It is {out['workflow_state']!r}, not a draft: a rule on the instance set that. "
               "Check it before publishing.")
        out["note"] = f"{out['note']} {odd}" if out.get("note") else odd
    return out


_ARTICLE_FIELDS = "sys_id,number,short_description,workflow_state,kb_knowledge_base,version,latest"
_NUMBER_ROWS = 20


def _article_summary(row: dict) -> dict[str, Any]:
    return {"sys_id": _raw(row, "sys_id"), "number": _raw(row, "number"),
            "short_description": _raw(row, "short_description"),
            "workflow_state": _raw(row, "workflow_state"), "version": _label(row, "version"),
            "knowledge_base": _label(row, "kb_knowledge_base")}


def _article(article: str) -> dict:
    """The kb_knowledge row `article` names, by sys_id or number.

    Numbers aren't unique: an article's versions share one, and imported demo data reuses them
    in other KBs (9 numbers did on dev442835). When every row is in one KB, they are taken as one
    article's versions and the row marked latest is used. Rows in more than one KB, or several
    latest ones, are refused with every match listed: a latest flag says nothing about which of
    two unrelated articles was meant.
    """
    article = (article or "").strip()
    if _SYS_ID.fullmatch(article):
        query = f"sys_id={article}"
    elif _NUMBER.fullmatch(article.upper()):
        query = f"number={article.upper()}"
    else:
        raise ValueError(f"{article!r} is not an article: give its number (KB0010001) or its sys_id.")
    res = verbs.now_query(ARTICLE_TABLE, query, fields=_ARTICLE_FIELDS, limit=_NUMBER_ROWS,
                          display_value="all")
    rows = res["records"]
    if res["truncated"]:
        raise ValueError(f"{article} is the number of more than {_NUMBER_ROWS} articles: give the sys_id.")
    if len(rows) > 1 and len({_raw(r, "kb_knowledge_base") for r in rows}) == 1:
        rows = [r for r in rows if _raw(r, "latest") == "true"] or rows
    if not rows:
        raise ValueError(f"No knowledge article {article!r}. Check it, or this account's read access.")
    if len(rows) > 1:
        listed = "; ".join(
            f"{a['sys_id']} ({a['knowledge_base']}, {a['workflow_state']}, v{a['version'] or '-'}: "
            f"{a['short_description']})" for a in map(_article_summary, rows))
        raise ValueError(f"{article} is the number of {len(rows)} articles: {listed}. Give the sys_id.")
    return rows[0]


def _state(sys_id: str) -> str:
    return str(verbs.now_get(ARTICLE_TABLE, sys_id, fields="workflow_state").get("workflow_state") or "")


def _settle(sys_id: str, wait: bool) -> str:
    """The article's state once it has left draft, or after SETTLE_SECONDS (the flow can finish
    after the script returns). Without `wait`, a single read."""
    state = _state(sys_id)
    deadline = _clock() + SETTLE_SECONDS
    while wait and state == "draft" and _clock() < deadline:
        _sleep(_POLL_SECONDS)
        state = _state(sys_id)
    return state


def _running_flow(sys_id: str) -> str | None:
    """The name of an unfinished flow on the article, "" if none, or None if this account can't
    tell (sys_flow_context unreadable). A dry-run hint only: the publish script checks again."""
    try:
        rows = verbs.now_query("sys_flow_context",
                               f"source_record={sys_id}^stateIN{','.join(_RUNNING_FLOW_STATES)}",
                               fields="name,state", limit=1)["records"]
    except Exception:
        return None
    return str(rows[0].get("name") or "a flow") if rows else ""


_OUTCOMES = {
    "published": ("published", "Published."),
    "review": ("in_review", "Sent for approval: the KB's publish flow moved it to Review, and it "
                            "publishes once approved. Nothing more to do here."),
    "scheduled_publish": ("scheduled", "Scheduled: it publishes on its scheduled publish date."),
}


def now_kb_publish_article(article: str, confirmed: bool = False) -> dict[str, Any]:
    """Publish a draft knowledge article through its knowledge base's publish flow. GATED by
    `confirmed`.

    `article` is the article's number (KB0010001) or sys_id. A number shared by articles in more
    than one KB, or by several current ones, raises ValueError listing them: give the sys_id then.

    This runs the platform's own publish (`KnowledgeUIAction().publish`, as the Publish button
    does). That starts the KB's publish flow, so an approval flow asks for approval, and no field
    is written directly. It runs through `now_run_script`, which needs the admin-only `now_data`
    run_script endpoint on the instance.

    `confirmed=False` (the default) is a dry run, and nothing runs on the instance. It returns
    the `article`, its `knowledge_base`, the `publish_flow`, the `expected` outcome and
    `publishable`. Only a draft can be published, the KB needs a publish flow, no publish flow may
    be running on the article, and the endpoint must exist. Call again with `confirmed=True`.

    A confirmed publish reads the article back, waiting up to 15 s for the flow. `action` is:
    - "published";
    - "in_review": an approval flow is waiting for an approver;
    - "scheduled";
    - "unconfirmed": the publish may have started but the article is still a draft, or it
      couldn't be read back. The flow may still be running. Check with now_get before anything
      else. Calling again is safe: the script won't start a second publish while a flow is running.
    - "not_published": nothing was published, and `reason` says why (this account may not publish
      to the KB, a publish flow is already running, the script couldn't read the article);
    - "refused": not a draft, or the KB has no publish flow. Nothing ran.
    - "endpoint_missing": nothing ran.
    - "unexpected_state": the article is now in a state no publish leads to.

    `script` carries the instance script's own answer.
    """
    row = _article(article)
    summary = _article_summary(row)
    sys_id, state = _checked_id(summary["sys_id"], "the article's sys_id"), summary["workflow_state"]
    kb_id = _raw(row, "kb_knowledge_base")
    kb_rows = verbs.now_query("kb_knowledge_base", f"sys_id={kb_id}", fields=_KB_FIELDS, limit=1,
                              display_value="all")["records"] if _SYS_ID.fullmatch(kb_id) else []
    kb = _kb_summary(kb_rows[0]) if kb_rows else None
    flow = kb and kb["publish_flow"]
    base = {"article": summary, "knowledge_base": kb and {"sys_id": kb["sys_id"], "title": kb["title"]},
            "publish_flow": flow}

    refusal = None
    if state != "draft":
        refusal = (f"only a draft can be published, and this article is {state!r}. A published "
                   "article is changed by checking it out (a new version) in the UI")
    elif kb is None:
        refusal = "its knowledge base can't be read by this account"
    elif kb["kb_version"] == "3" and not flow:
        refusal = _NO_FLOW
    expected = (_FLOW_OUTCOME.get(flow, f"start the flow {flow!r}, which decides what happens") if flow
                else "publish it at once (a version 2 KB, with no flow)")

    if not confirmed:
        why = refusal
        if why is None:
            running = _running_flow(sys_id)
            if running:
                why = f"a publish flow ({running}) is already running on it: wait for it, then check the article"
            elif verbs._resolve_run_script_url() is None:
                why = ("the now_data run_script endpoint isn't on this instance, and publishing runs "
                       "through it (see the now-data README)")
        return {"confirmed": False, "action": "preview", **base, "publishable": why is None,
                "expected": None if refusal else expected,
                "note": (f"Dry run, nothing ran. It can't be published: {why}." if why else
                         f"Dry run, nothing ran. Publishing would {expected}. Call again "
                         "with confirmed=true to publish.")}
    if refusal:
        return {"confirmed": True, "action": "refused", **base, "reason": refusal,
                "note": f"Nothing ran: {refusal}."}
    if verbs._resolve_run_script_url() is None:
        missing = ("the now_data run_script endpoint isn't on this instance, and publishing runs "
                   "through it (see the now-data README)")
        return {"confirmed": True, "action": "endpoint_missing", **base, "reason": missing,
                "note": f"Nothing ran: {missing}."}

    try:
        ran = verbs.now_run_script(_PUBLISH_SCRIPT % (sys_id, ",".join(_RUNNING_FLOW_STATES)),
                                   confirmed=True)
    except Exception as e:  # the call may have reached the instance: the read-back decides
        ran = {"action": "error", "error": type(e).__name__, "message": str(e)}
    answer = ran.get("result") if ran.get("action") == "executed" else None
    answer = answer if isinstance(answer, dict) else {}
    # The script's own word that it published nothing: then there's nothing to wait for.
    nothing_ran = (answer.get("found") is False or answer.get("can_publish") is False
                   or bool(answer.get("in_flight")))

    try:
        after = _settle(sys_id, wait=not nothing_ran)
    except Exception as e:
        return {"confirmed": True, "action": "unconfirmed", **base, "script": ran,
                "note": (f"The publish call was made, but reading the article back failed "
                         f"({type(e).__name__}: {e}). Read it with now_get before trying again.")}
    out = {"confirmed": True, **base, "workflow_state": after, "script": ran}
    # The article as it is now (state, version), not as it was before the publish.
    try:
        rows = verbs.now_query(ARTICLE_TABLE, f"sys_id={sys_id}", fields=_ARTICLE_FIELDS, limit=1,
                               display_value="all")["records"]
        if rows:
            out["article"] = _article_summary(rows[0])
    except Exception:
        pass  # the state read above is the receipt; this only refreshes the summary
    if after in _OUTCOMES:
        out["action"], out["note"] = _OUTCOMES[after]
        return out
    if after != "draft":
        out["action"] = "unexpected_state"
        out["note"] = f"The article is now {after!r}, which isn't a publish outcome. Check it."
        return out

    if nothing_ran:
        if answer.get("found") is False:
            reason = ("the instance script couldn't read the article (its read access differs from "
                      "the Table API's)")
        elif answer.get("in_flight"):
            reason = (f"a publish flow ({answer['in_flight']}) is already running on it, so no second "
                      "one was started: wait for it, then check the article")
        else:
            reason = ("the platform refused it (KBKnowledge.canPublish is false): this account can't "
                      "publish to this knowledge base, or the article isn't publishable")
        out["action"], out["reason"] = "not_published", reason
        out["note"] = f"Not published: {reason}."
        return out

    if ran.get("action") != "executed":
        why = (f"the publish call didn't answer cleanly ({ran.get('action')}: "
               f"{ran.get('error') or ''} {ran.get('message') or ''})").replace("  ", " ").replace(" )", ")")
    else:
        why = "the publish call ran"
    out["action"] = "unconfirmed"
    out["reason"] = (f"{why}, and the article was still a draft after {SETTLE_SECONDS:g} s. The flow may "
                     "still be running, or it never started")
    out["note"] = (f"Unconfirmed: {out['reason']}. Check it with now_get before anything else. Calling "
                   "again is safe: no second publish starts while a flow is running.")
    return out
