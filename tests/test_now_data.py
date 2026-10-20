"""now-data verbs against an in-memory fake PySNC client (no network)."""

import json
from pathlib import Path

import pytest

from now_data import query_check, verbs

# What the fake instance's UI Meta API answers (now-mcp#4 checks every request against it).
STATE = [{"value": str(v), "label": l} for v, l in ((1, "New"), (2, "In Progress"), (6, "Resolved"), (7, "Closed"))]
GENERIC = {
    "sys_id": {"type": "GUID"}, "number": {"type": "string"}, "short_description": {"type": "string"},
    "name": {"type": "string"},
    "state": {"type": "choice", "internal_type": "integer", "choices": STATE},
    "priority": {"type": "choice", "internal_type": "integer", "choices": [{"value": "1", "label": "1 - Critical"}]},
    "active": {"type": "boolean", "internal_type": "boolean"},
    "assigned_to": {"type": "reference", "reference": "sys_user"},
    "assignment_group": {"type": "reference", "reference": "sys_user_group"},
    "reassignment_count": {"type": "integer", "internal_type": "integer"},
    # Journal fields read back empty; the entry lands in sys_journal_field (now-mcp#3).
    "work_notes": {"type": "journal_input", "internal_type": "journal_input"},
    "comments": {"type": "journal_input", "internal_type": "journal_input"},
    "due_date": {"type": "glide_date_time", "internal_type": "glide_date_time"},
}

# incident's columns as a PDI's UI Meta API answered them (read-only GET of /api/now/ui/meta/incident,
# 2026-10-19, now-mcp#18 review): work_notes / comments are journal_input, state is integer.
MEASURED = json.loads((Path(__file__).parent / "fixtures" / "ui_meta" / "incident.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _fresh_column_cache():
    query_check._COLUMNS.clear()
    yield
    query_check._COLUMNS.clear()


class FakeGR:
    """A PySNC GlideRecord over `client.db`. `client` decides what a write keeps (see FakeClient)."""

    def __init__(self, client, table):
        self.client, self.db, self.table = client, client.db, table
        self.fields, self.limit, self.encoded = None, None, None
        self._rec, self.updates, self.filters = None, {}, []

    def add_encoded_query(self, q):
        self.encoded = q

    def add_query(self, field, value):
        self.encoded = f"{field}={value}"
        self.filters.append((field, value))

    def query(self):
        c = self.client
        errors = {**c.query_errors, **(c.after_write_query_errors if c.written else {})}
        if self.table in errors:
            raise errors[self.table]
        self._rows = [r for r in self.db.get(self.table, {}).values()
                      if self.table not in self.client.unreadable
                      and all(str(r.get(f)) == str(v) for f, v in self.filters)]
        self._i = -1

    def next(self):
        self._i += 1
        if self._i < len(self._rows):
            self._rec = self._rows[self._i]
            return True
        return False

    def get_value(self, f):
        return self._rec.get(f)

    def serialize_all(self, display_value=False, fields=None):
        return [dict(r) for r in self._rows[: self.limit]]

    def get(self, sys_id):
        self._rec = self.db.get(self.table, {}).get(sys_id)
        return self._rec is not None

    def serialize(self, display_value=False, fields=None):
        return dict(self._rec)

    def set_value(self, f, v):
        self.updates[f] = v

    def _store(self, rec, sys_id):
        """Apply the set values to `rec` the way the fake instance's rules allow."""
        c = self.client
        for f, v in self.updates.items():
            if f in c.drop:  # a business rule that keeps the old value, and still answers 200
                continue
            # By name: how the fake INSTANCE stores it, apart from the column metadata the verb reads.
            if f in c.journal_fields:
                rec[f] = ""  # a journal field reads back empty...
                if c.journal_writes:  # ...and its entry goes to the journal
                    n = len(self.db.setdefault("sys_journal_field", {}))
                    self.db["sys_journal_field"][f"j{n}"] = {
                        "sys_id": f"j{n}", "element_id": sys_id, "element": f,
                        "value": str(v).replace("\n", "\r\n") + "\r\n", **c.journal_misroute}
                continue
            rec[f] = c.coerce.get(f, lambda x: x)(v)

    def update(self):
        if self.client.write_error is not None:
            raise self.client.write_error
        if self.client.update_returns is not REAL:
            return self.client.update_returns
        self._store(self._rec, self._rec["sys_id"])
        self.client.written = True
        return self._rec["sys_id"]

    def initialize(self):
        self._rec, self.updates = {}, {}

    def insert(self):
        if self.client.write_error is not None:
            raise self.client.write_error
        if self.client.insert_returns is not REAL:
            return self.client.insert_returns
        rows = self.db.setdefault(self.table, {})
        sys_id = f"new{len(rows)}"
        rows[sys_id] = {"sys_id": sys_id}
        self._store(rows[sys_id], sys_id)
        self.client.written = True
        return sys_id


REAL = object()  # update()/insert() answer as the instance would


class FakeClient:
    instance = "https://example.service-now.com"

    def __init__(self, db):
        self.db = db
        self.posts = []
        self.session = FakeSession(None)
        self.records = []  # every GlideRecord made, so a test can see what was touched
        # What the fake instance does with a write: `drop` fields keep their old value (a business
        # rule, and still a 200), `coerce` maps a value to what is stored, and `journal_fields` add a
        # sys_journal_field entry unless `journal_writes` is off; `journal_misroute` overrides the
        # entry's keys (it lands on another record or field). `write_error` is raised by
        # update()/insert(); `written` says one stored something.
        self.drop, self.coerce, self.journal_writes = set(), {}, True
        self.journal_fields, self.journal_misroute = {"work_notes", "comments"}, {}
        self.write_error, self.written = None, False
        # Reads: a table in `unreadable` queries as no rows (ACL-hidden); `query_errors` raise, and
        # `after_write_query_errors` raise only once something was written.
        self.unreadable, self.query_errors, self.after_write_query_errors = set(), {}, {}
        self.update_returns = self.insert_returns = REAL

    def GlideRecord(self, table):
        gr = FakeGR(self, table)
        self.records.append(gr)
        return gr


@pytest.fixture
def client(monkeypatch):
    db = {"incident": {f"id{i}": {"sys_id": f"id{i}", "state": "1", "short_description": f"x{i}",
                                  "work_notes": "", "comments": "", "due_date": ""} for i in range(5)}}
    c = FakeClient(db)
    monkeypatch.setattr(verbs, "get_client", lambda: c)
    return c


@pytest.fixture
def measured(client):
    """The fake instance answers UI Meta for incident with the measured columns (MEASURED)."""
    client.session.columns["incident"] = MEASURED
    return client


def test_query_truncates_and_clamps(client):
    r = verbs.now_query("incident", "state=1", limit=3)
    assert (r["count"], r["truncated"]) == (3, True)
    assert verbs.now_query("incident", limit=10)["truncated"] is False
    assert verbs.now_query("incident", limit=0)["count"] == 1  # clamped up to 1


def test_get_missing_raises_value_error(client):
    assert verbs.now_get("incident", "id1")["short_description"] == "x1"
    with pytest.raises(ValueError):
        verbs.now_get("incident", "nope")


# ── display_value="all" is a stated contract (now-mcp#19) ────────────────────
#
# servicenow-rio reads every field as {value, display_value}. Pinned through the real verbs and the
# installed pysnc over a canned transport, not FakeGR, whose serialize ignores display_value.

SID, USER, GROUP = "a" * 32, "b" * 32, "c" * 32
# One incident as the Table API answers it with sysparm_display_value=all and reference links
# excluded. assignment_group comes back with an empty label.
ROW = {"sys_id": {"value": SID, "display_value": SID},
       "state": {"value": "2", "display_value": "In Progress"},
       "assigned_to": {"value": USER, "display_value": "Beth Anglin"},
       "assignment_group": {"value": GROUP, "display_value": ""}}
FIELDS = "sys_id,state,assigned_to,assignment_group"


@pytest.fixture
def pysnc_instance(monkeypatch):
    """A real pysnc ServiceNowClient whose transport answers UI Meta and the Table API from ROW, and
    keeps every request it was sent."""
    import requests
    from pysnc import ServiceNowClient
    from requests.adapters import BaseAdapter

    sent = []

    class Canned(BaseAdapter):
        def send(self, request, **kwargs):
            sent.append(request)
            path = requests.utils.urlparse(request.url).path
            if path.startswith("/api/now/ui/meta/"):
                body = {"result": {"columns": GENERIC}}
            elif path == f"/api/now/table/incident/{SID}":
                body = {"result": ROW}
            else:
                body = {"result": [ROW]}
            r = requests.Response()
            r.status_code, r._content, r.encoding = 200, json.dumps(body).encode(), "utf-8"
            r.headers["Content-Type"] = "application/json"
            r.headers["X-Total-Count"] = "1"
            r.request, r.url = request, request.url
            return r

        def close(self):
            pass

    sn = ServiceNowClient("https://example.service-now.com", ("user", "pw"))
    sn.session.mount(sn.instance, Canned())
    monkeypatch.setattr(verbs, "get_client", lambda: sn)
    return sent


def _table_requests(sent):
    import requests
    return [requests.utils.urlparse(r.url) for r in sent if "/api/now/table/" in r.url]


def test_display_value_all_returns_value_and_label_per_field(pysnc_instance):
    both = {"sys_id": {"value": SID, "display_value": SID},
            "state": {"value": "2", "display_value": "In Progress"},
            "assigned_to": {"value": USER, "display_value": "Beth Anglin"},
            # No label from the instance: display_value is the value, not "".
            "assignment_group": {"value": GROUP, "display_value": GROUP}}
    assert verbs.now_query("incident", fields=FIELDS, display_value="all")["records"] == [both]
    assert verbs.now_get("incident", SID, fields=FIELDS, display_value="all") == both


def test_display_value_true_and_false_return_one_of_the_two(pysnc_instance):
    raw = {"sys_id": SID, "state": "2", "assigned_to": USER, "assignment_group": GROUP}
    labels = {"sys_id": SID, "state": "In Progress", "assigned_to": "Beth Anglin", "assignment_group": GROUP}
    assert verbs.now_query("incident", fields=FIELDS)["records"] == [raw]
    assert verbs.now_query("incident", fields=FIELDS, display_value=True)["records"] == [labels]
    assert verbs.now_get("incident", SID, fields=FIELDS, display_value=False) == raw
    assert verbs.now_get("incident", SID, fields=FIELDS, display_value=True) == labels


@pytest.mark.parametrize("display_value", [False, True, "all"])
def test_the_read_fetches_values_and_labels_whatever_is_asked(pysnc_instance, display_value):
    # One fetch serves all three: the verb sets it rather than relying on pysnc's default.
    from urllib.parse import parse_qs
    verbs.now_query("incident", fields=FIELDS, display_value=display_value)
    verbs.now_get("incident", SID, fields=FIELDS, display_value=display_value)
    reads = _table_requests(pysnc_instance)
    assert len(reads) == 2
    assert all(parse_qs(u.query)["sysparm_display_value"] == ["all"] for u in reads)


@pytest.mark.parametrize("bad", ["false", "true", "both", "", "ALL", 1, 0, None])
def test_any_other_display_value_is_refused_before_a_request(pysnc_instance, bad):
    # pysnc 1.2.1 serialises any string as both, so "false" would return pairs.
    with pytest.raises(ValueError, match="display_value must be"):
        verbs.now_query("incident", display_value=bad)
    with pytest.raises(ValueError, match="display_value must be"):
        verbs.now_get("incident", SID, display_value=bad)
    assert pysnc_instance == []


def test_update_previews_by_default_and_writes_only_when_confirmed(client):
    p = verbs.now_update("incident", "id1", {"state": "2", "short_description": "x1", "bogus": "y"})
    assert p["action"] == "preview"
    assert p["changes"] == [{"field": "state", "current": "1", "proposed": "2"}]
    assert p["no_ops"] == ["short_description"]
    assert p["unknown_fields"] == ["bogus"]
    assert client.db["incident"]["id1"]["state"] == "1"  # nothing written

    done = verbs.now_update("incident", "id1", {"state": "2", "short_description": "y"}, confirmed=True)
    assert done == {"confirmed": True, "action": "updated", "table": "incident", "sys_id": "id1",
                    "changed_fields": ["state", "short_description"], "not_applied": [],
                    "journal_unconfirmed": [], "no_ops": [], "unknown_fields": []}
    assert client.db["incident"]["id1"]["state"] == "2"


# ── now-mcp#18 review: a field that wasn't sent is not applied, never "updated" ──

def test_update_with_an_unknown_field_is_not_updated(client):
    done = verbs.now_update("incident", "id1", {"state": "2", "bogus": "y"}, confirmed=True)
    assert done["action"] == "partially_applied"
    assert done["changed_fields"] == ["state"] and done["unknown_fields"] == ["bogus"]
    assert done["not_applied"] == [{"field": "bogus", "requested": "y", "actual": None, "reason": "unknown_field"}]
    assert "Not sent: bogus" in done["note"]
    assert client.db["incident"]["id1"]["state"] == "2" and "bogus" not in client.db["incident"]["id1"]


@pytest.mark.parametrize("values, message", [
    ({"u_typo": "n"}, "nothing was written: incident record id1 has no field u_typo as this account reads it"),
    ({"work_note": "Called the user"}, "nothing was written: incident record id1 has no field work_note "),
    ({}, "nothing was written: no values were given"),
], ids=["typo", "work_note", "empty"])
def test_a_confirmed_update_with_no_field_on_the_record_writes_nothing(client, values, message):
    with pytest.raises(query_check.QueryRefused, match=message):
        verbs.now_update("incident", "id1", values, confirmed=True)
    assert client.written is False
    assert verbs.now_update("incident", "id1", values)["unknown_fields"] == sorted(values)  # the preview reports


def test_a_column_added_since_the_columns_were_cached_is_not_labelled_unknown(client, monkeypatch):
    verbs.now_update("incident", "id1", {"state": "2"})  # caches incident's columns
    monkeypatch.setitem(GENERIC, "u_added", {"type": "string"})  # a column added since; this account can't read it
    done = verbs.now_update("incident", "id1", {"state": "2", "u_added": "x", "u_typo": "y"}, confirmed=True)
    assert done["not_applied"] == [
        {"field": "u_added", "requested": "x", "actual": None, "reason": "unreadable_field"},
        {"field": "u_typo", "requested": "y", "actual": None, "reason": "unknown_field"},
    ]
    assert client.session.meta_reads.count("incident") == 2  # read again once before labelling


def test_a_field_hidden_by_a_read_acl_is_unreadable_and_not_sent(client):
    del client.db["incident"]["id1"]["work_notes"]  # a column of the table, missing from the record as read
    with pytest.raises(query_check.QueryRefused, match="has no field work_notes as this account reads it"):
        verbs.now_update("incident", "id1", {"work_notes": "note"}, confirmed=True)
    done = verbs.now_update("incident", "id1", {"work_notes": "note", "state": "2"}, confirmed=True)
    assert done["not_applied"] == [{"field": "work_notes", "requested": "note", "actual": None,
                                    "reason": "unreadable_field"}]
    assert done["action"] == "partially_applied" and done["changed_fields"] == ["state"]
    assert "sys_journal_field" not in client.db  # never sent


# ── now-mcp#3: a confirmed write is read back ────────────────────────────────

def test_update_a_field_that_does_not_take_is_not_applied(client):
    # A business rule keeps state (setAbortAction on that field's change), and the instance still answers 200.
    client.drop = {"state"}
    done = verbs.now_update("incident", "id1", {"state": "2", "short_description": "moved"}, confirmed=True)
    assert done["action"] == "partially_applied"
    assert done["changed_fields"] == ["short_description"]
    assert done["not_applied"] == [{"field": "state", "requested": "2", "actual": "1", "reason": "value_differs"}]
    assert "state" in done["note"] and "business rule" in done["note"]

    nothing = verbs.now_update("incident", "id2", {"state": "2"}, confirmed=True)
    assert nothing["action"] == "not_applied" and nothing["changed_fields"] == []
    assert nothing["not_applied"] == [{"field": "state", "requested": "2", "actual": "1", "reason": "value_differs"}]


def test_update_reads_back_by_sys_id_only_the_fields_it_wrote(client):
    verbs.now_update("incident", "id1", {"state": "2", "short_description": "y"}, confirmed=True)
    back = client.records[-1]
    assert back.filters == [("sys_id", "id1")]
    assert back.fields == ["sys_id", "state", "short_description"]


def test_update_a_no_op_that_a_rule_changes_is_not_applied_not_a_no_op(client):
    client.coerce = {"short_description": lambda v: "rewritten by a rule"}
    done = verbs.now_update("incident", "id1", {"state": "2", "short_description": "x1"}, confirmed=True)
    # short_description already held x1 and was not sent as a change; the rule changed it anyway.
    assert done["not_applied"] == [{"field": "short_description", "requested": "x1",
                                    "actual": "rewritten by a rule", "reason": "value_differs"}]
    assert done["no_ops"] == [] and done["changed_fields"] == ["state"]
    assert done["action"] == "partially_applied"


@pytest.mark.parametrize("answer", [None, "", "some-other-sys-id"])
def test_update_answered_without_its_sys_id_is_unconfirmed(client, answer):
    client.update_returns = answer  # a 200 whose answer lacks the sys_id: it may have landed
    with pytest.raises(verbs.WriteUnconfirmed, match=r"incident id1: the update may have been applied: "
                                                     r"the answer didn't carry the record's sys_id"):
        verbs.now_update("incident", "id1", {"state": "2"}, confirmed=True)


def test_update_that_cannot_be_read_back_raises_and_says_it_was_accepted(client):
    client.unreadable = {"incident"}  # get() finds it; the read-back query sees no row
    with pytest.raises(verbs.WriteUnconfirmed, match=r"incident id1: the update was accepted, but no row came "
                                                     r"back.*Check the record before retrying"):
        verbs.now_update("incident", "id1", {"state": "2"}, confirmed=True)


# ── now-mcp#18 review: a failure during the write says it may have landed ────

def _pysnc_raises(method, status, body=b'{"result": {"sys_id": "id1"}}', content_type="application/json"):
    """What the installed pysnc raises when the instance answers its PATCH (update) or POST (insert)
    with `status` and `body`: a real ServiceNowClient over a canned transport, so the exception is
    built by pysnc's own code (1.2.1 when this was written), not by hand (review, round 2)."""
    import requests
    from pysnc import ServiceNowClient
    from requests.adapters import BaseAdapter

    class Canned(BaseAdapter):
        def send(self, request, **kwargs):
            r = requests.Response()
            r.status_code, r._content, r.encoding = status, body, "utf-8"
            r.headers["Content-Type"] = content_type
            r.request, r.url = request, request.url
            return r

        def close(self):
            pass

    sn = ServiceNowClient("https://example.service-now.com", ("user", "pw"))
    sn.session.mount(sn.instance, Canned())  # replaces pysnc's own adapter at that prefix
    gr = sn.GlideRecord("incident")
    gr.initialize()
    if method == "update":
        gr.set_value("sys_id", "id1")
    gr.set_value("state", "2")
    try:
        getattr(gr, method)()
    except Exception as e:
        return e
    pytest.fail(f"pysnc's {method}() no longer raises for HTTP {status}: re-check verbs._write against it")


def _maybe_landed():
    """Write failures after which the write may have landed: (update's, insert's) exception."""
    import requests
    from pysnc import exceptions as px
    html = (b"<html>Your instance is hibernating</html>", "text/html")
    return {
        # Built by pysnc itself: a status it doesn't take as success, an error status it keeps no
        # code for, and an answer that isn't JSON.
        "2xx-not-expected": (_pysnc_raises("update", 202), _pysnc_raises("insert", 200)),
        "202": (_pysnc_raises("update", 202), _pysnc_raises("insert", 202)),
        "5xx": (_pysnc_raises("update", 500, b'{"error": {"message": "boom"}}'),
                _pysnc_raises("insert", 503, b'{"error": {"message": "Service Unavailable"}}')),
        "400": (_pysnc_raises("update", 400, b'{"error": {"message": "Bad Request"}}'),
                _pysnc_raises("insert", 400, b'{"error": {"message": "Bad Request"}}')),
        "empty-204": (_pysnc_raises("update", 204, b""), _pysnc_raises("insert", 204, b"")),
        "html-200": (_pysnc_raises("update", 200, *html), _pysnc_raises("insert", 201, *html)),
        # The transport, and what pysnc's GlideRecord raises reading the sys_id of an empty answer.
        "reset": (requests.ConnectionError("('Connection aborted.', ConnectionResetError(10054))"),) * 2,
        "chunked": (requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead"),) * 2,
        "empty-answer": (AttributeError("'GlideRecord' object has no attribute 'sys_id'"),) * 2,
        # A pysnc that keeps a 5xx's status, or none.
        "kept-503": (px.UpdateException({"error": "x"}, status_code=503),
                     px.InsertException({"error": "x"}, status_code=503)),
        "no-status": (px.UpdateException({"error": "x"}), px.InsertException({"error": "x"})),
    }


def _refused():
    """Answers that mean the instance refused, so nothing was written: (update's, insert's) exception."""
    from pysnc import exceptions as px
    error = b'{"error": {"message": "refused"}}'
    return {
        "401": (_pysnc_raises("update", 401, error), _pysnc_raises("insert", 401, error)),
        "403": (_pysnc_raises("update", 403, error), _pysnc_raises("insert", 403, error)),
        "404": (_pysnc_raises("update", 404, error), _pysnc_raises("insert", 404, error)),
        # A pysnc that keeps a 4xx's status on Update/InsertException.
        "kept-400": (px.UpdateException({"error": "x"}, status_code=400),
                     px.InsertException({"error": "x"}, status_code=422)),
    }


def test_pysnc_raises_update_and_insert_exceptions_only_for_statuses_below_400():
    # The premise of _write's 4xx rule (review, round 2): pysnc turns every status of 400 or more into
    # Auth/Role/NotFound/RequestException before update()/insert() build their own exception.
    from pysnc import exceptions as px
    assert isinstance(_pysnc_raises("update", 202), px.UpdateException)
    assert _pysnc_raises("update", 202).status_code == 202
    assert isinstance(_pysnc_raises("insert", 200), px.InsertException)
    for status in (400, 409, 422, 500, 503):
        e = _pysnc_raises("update", status, b'{"error": {"message": "x"}}')
        assert type(e) is px.RequestException, (status, type(e))
    assert type(_pysnc_raises("update", 403, b'{"error": {}}')) is px.RoleException


@pytest.mark.parametrize("case", list(_maybe_landed()))
def test_a_write_that_fails_once_it_may_have_been_sent_is_unconfirmed(client, case):
    on_update, on_insert = _maybe_landed()[case]
    client.write_error = on_update
    with pytest.raises(verbs.WriteUnconfirmed, match=r"incident id1: the update may have been applied: .*"
                                                     r"Check the record before retrying") as caught:
        verbs.now_update("incident", "id1", {"state": "2"}, confirmed=True)
    assert caught.value.__cause__ is on_update
    client.write_error = on_insert
    with pytest.raises(verbs.WriteUnconfirmed, match=r"incident: the insert may have been applied: .*"
                                                     r"A row may exist"):
        verbs.now_insert("incident", {"short_description": "x"}, confirmed=True)


@pytest.mark.parametrize("case", list(_refused()))
def test_a_write_the_instance_refused_is_raised_as_it_is(client, case):
    on_update, on_insert = _refused()[case]
    client.write_error = on_update
    with pytest.raises(type(on_update)) as caught:
        verbs.now_update("incident", "id1", {"state": "2"}, confirmed=True)
    assert caught.value is on_update and not isinstance(caught.value, verbs.WriteUnconfirmed)
    client.write_error = on_insert
    with pytest.raises(type(on_insert)) as caught:
        verbs.now_insert("incident", {"short_description": "x"}, confirmed=True)
    assert caught.value is on_insert


def test_an_answer_that_is_not_json_says_so_not_that_the_connection_failed(client):
    client.write_error = _pysnc_raises("update", 200, b"<html>hibernating</html>", "text/html")
    with pytest.raises(verbs.WriteUnconfirmed, match=r"answered with something that isn't JSON.*"
                                                     r"may or may not have landed") as caught:
        verbs.now_update("incident", "id1", {"state": "2"}, confirmed=True)
    assert "connection" not in str(caught.value)


def _connect_failures():
    import requests
    from urllib3.exceptions import MaxRetryError, NameResolutionError, NewConnectionError
    refused = NewConnectionError(None, "Failed to establish a new connection: [WinError 10061] refused")
    dns = NameResolutionError("example.service-now.com", None, OSError("getaddrinfo failed"))
    return {
        "refused": requests.ConnectionError(MaxRetryError(None, "/api/now/table/incident", refused)),
        "dns": requests.ConnectionError(MaxRetryError(None, "/api/now/table/incident", dns)),
        "connect-timeout": requests.ConnectTimeout("Connection to example.service-now.com timed out"),
    }


@pytest.mark.parametrize("case", list(_connect_failures()))
def test_a_connection_that_could_not_be_opened_is_worded_not_sent_but_stays_unconfirmed(client, case):
    # The last attempt never reached the instance, but pysnc retries and keeps only the last reason,
    # so "safe to retry" is more than the exception can back.
    client.write_error = _connect_failures()[case]
    with pytest.raises(verbs.WriteUnconfirmed, match=r"incident id1: the update was most likely not sent: the "
                                                     r"connection to the instance could not be opened.*"
                                                     r"can't be ruled out"):
        verbs.now_update("incident", "id1", {"state": "2"}, confirmed=True)
    with pytest.raises(verbs.WriteUnconfirmed, match=r"incident: the insert was most likely not sent"):
        verbs.now_insert("incident", {"short_description": "x"}, confirmed=True)


def test_a_connection_reset_after_sending_is_not_worded_not_sent(client):
    client.write_error = _maybe_landed()["reset"][0]
    with pytest.raises(verbs.WriteUnconfirmed, match=r"may have been applied: the connection broke after the "
                                                     r"request may have been sent"):
        verbs.now_update("incident", "id1", {"state": "2"}, confirmed=True)


# ── now-mcp#18 review, round 2: true/false fields take true or false ─────────

@pytest.mark.parametrize("value", ["1", "0", "yes", "TRUE", "", 1])
def test_a_true_false_field_given_anything_else_is_refused_before_the_write(client, value):
    with pytest.raises(query_check.QueryRefused, match=r"active is a true/false field, so give it true or false"):
        verbs.now_update("incident", "id1", {"active": value}, confirmed=True)
    with pytest.raises(query_check.QueryRefused, match=r"active is a true/false field"):
        verbs.now_insert("incident", {"active": value}, confirmed=True)
    assert client.written is False and client.records == []


@pytest.mark.parametrize("value", [True, False, "true", "false", " true "])
def test_a_true_false_field_takes_true_or_false(client, value):
    client.db["incident"]["id1"]["active"] = "false" if value in (True, "true", " true ") else "true"
    client.coerce = {"active": lambda v: str(v).strip().lower()}  # the instance stores JSON true as "true"
    done = verbs.now_update("incident", "id1", {"active": value}, confirmed=True)
    assert done["action"] == "updated" and done["changed_fields"] == ["active"]


# ── journal fields ───────────────────────────────────────────────────────────

def test_journal_field_confirmed_by_a_new_entry(client):
    # An identical older note is there too: it's the NEW entry that confirms.
    client.db["sys_journal_field"] = {"old": {"sys_id": "old", "element_id": "id1", "element": "work_notes",
                                              "value": "Called the user\nno answer"}}
    done = verbs.now_update("incident", "id1", {"work_notes": "Called the user\nno answer"}, confirmed=True)
    # The fake stores \r\n and a trailing newline, as the journal does; the match normalises both.
    assert done["action"] == "updated"
    assert done["changed_fields"] == ["work_notes"]
    assert done["not_applied"] == [] and done["journal_unconfirmed"] == [] and "note" not in done
    # It was never compared field-wise: work_notes reads back empty.
    assert client.db["incident"]["id1"]["work_notes"] == ""


def test_journal_field_not_confirmed_by_an_identical_old_entry(client):
    # The same note was written last week; this write leaves no new entry.
    client.db["sys_journal_field"] = {"old": {"sys_id": "old", "element_id": "id1", "element": "work_notes",
                                              "value": "Customer contacted"}}
    client.journal_writes = False
    done = verbs.now_update("incident", "id1", {"work_notes": "Customer contacted", "state": "2"}, confirmed=True)
    assert done["journal_unconfirmed"] == ["work_notes"]
    assert done["not_applied"] == []  # unconfirmed is not "failed": it may have landed
    assert done["changed_fields"] == ["state"]
    # "updated" means nothing was kept back, not that everything was confirmed (now-mcp#18 review).
    assert done["action"] == "updated"
    assert "could not be confirmed" in done["note"] and "duplicated" in done["note"]


@pytest.mark.parametrize("misroute", [{"element_id": "id2"}, {"element": "comments"}], ids=["record", "field"])
def test_a_new_entry_on_another_record_or_field_does_not_confirm(client, misroute):
    # The only new entry with the text lands AFTER the snapshot, on another record or field: without
    # the element_id / element filters it would confirm.
    client.journal_misroute = misroute
    done = verbs.now_update("incident", "id1", {"work_notes": "hi"}, confirmed=True)
    assert len(client.db["sys_journal_field"]) == 1  # it was written, just not where it counts
    assert done["journal_unconfirmed"] == ["work_notes"] and done["changed_fields"] == []


def test_a_journal_read_that_fails_after_the_write_is_unconfirmed_not_an_error(client):
    from pysnc import exceptions as px
    client.after_write_query_errors = {"sys_journal_field": px.RequestException({"error": "timed out"})}
    done = verbs.now_update("incident", "id1", {"work_notes": "note", "state": "2"}, confirmed=True)
    assert client.db["sys_journal_field"]  # the note landed
    assert done["journal_unconfirmed"] == ["work_notes"] and done["changed_fields"] == ["state"]


def test_unconfirmed_journal_with_a_field_not_applied_is_partially_applied(client):
    # Nothing confirmed landed, but the note may have: "not_applied" would invite retrying it.
    client.journal_writes, client.drop = False, {"state"}
    done = verbs.now_update("incident", "id1", {"work_notes": "note", "state": "2"}, confirmed=True)
    assert done["changed_fields"] == [] and done["journal_unconfirmed"] == ["work_notes"]
    assert done["action"] == "partially_applied"


def test_an_http_error_on_the_journal_snapshot_stops_the_write(client):
    from pysnc import exceptions as px
    client.query_errors = {"sys_journal_field": px.RoleException({"error": {"message": "forbidden"}})}  # a 403
    with pytest.raises(px.RoleException):
        verbs.now_update("incident", "id1", {"work_notes": "note", "state": "2"}, confirmed=True)
    assert client.written is False and client.db["incident"]["id1"]["state"] == "1"
    assert "sys_journal_field" not in client.db


def test_an_acl_filtered_journal_snapshot_writes_and_reports_unconfirmed(client):
    # A read ACL that filters rows answers 200 with none: nothing stops the write, and the note
    # can't be confirmed (now-mcp#18 review: the safe outcome, but not "nothing written").
    client.unreadable = {"sys_journal_field"}
    done = verbs.now_update("incident", "id1", {"work_notes": "note", "state": "2"}, confirmed=True)
    assert client.written is True and client.db["sys_journal_field"]  # the note landed
    assert done["journal_unconfirmed"] == ["work_notes"] and done["changed_fields"] == ["state"]
    assert done["action"] == "updated"


def test_journal_type_comes_from_the_column_type_not_the_name(client, monkeypatch):
    # A custom journal field is confirmed by the journal; a string field named like one is compared.
    monkeypatch.setitem(GENERIC, "u_notes", {"type": "journal_input", "internal_type": "journal_input"})
    monkeypatch.setitem(GENERIC, "u_work_notes", {"type": "string"})
    client.journal_fields = client.journal_fields | {"u_notes"}  # how the fake instance stores it
    client.db["incident"]["id1"].update({"u_notes": "", "u_work_notes": ""})
    client.drop = {"u_work_notes"}
    done = verbs.now_update("incident", "id1", {"u_notes": "a", "u_work_notes": "b"}, confirmed=True)
    assert done["changed_fields"] == ["u_notes"]  # read back empty, confirmed by its new entry
    assert done["journal_unconfirmed"] == []
    assert done["not_applied"] == [{"field": "u_work_notes", "requested": "b", "actual": "",
                                    "reason": "value_differs"}]


# ── the UI Meta columns measured on a PDI (now-mcp#18 review) ────────────────

def test_the_measured_columns_mark_journal_fields_by_type():
    assert verbs._is_journal(MEASURED["work_notes"]) and verbs._is_journal(MEASURED["comments"])
    for name in ("close_notes", "short_description", "state", "assignment_group", "opened_at"):
        assert not verbs._is_journal(MEASURED[name]), name  # close_notes is notes text, but a string
    assert MEASURED["state"]["internal_type"] == "integer"


def test_journal_confirmed_with_the_measured_columns(measured):
    # The verb reads the measured metadata; the fake instance journals by field name, so the two
    # no longer share an assumption.
    done = verbs.now_update("incident", "id1", {"work_notes": "Called", "comments": "Hi", "state": "2"},
                            confirmed=True)
    assert measured.session.meta_reads == ["incident"]
    assert done["action"] == "updated" and done["journal_unconfirmed"] == []
    assert done["changed_fields"] == ["work_notes", "comments", "state"]
    assert {e["element"] for e in measured.db["sys_journal_field"].values()} == {"work_notes", "comments"}


def test_close_notes_from_the_measured_columns_is_compared_not_journalled(measured):
    measured.db["incident"]["id1"]["close_notes"] = ""
    measured.drop = {"close_notes"}
    done = verbs.now_update("incident", "id1", {"close_notes": "Fixed"}, confirmed=True)
    assert done["not_applied"] == [{"field": "close_notes", "requested": "Fixed", "actual": "",
                                    "reason": "value_differs"}]
    assert "sys_journal_field" not in measured.db


@pytest.mark.parametrize("given", ["02", "2.0", " 2"])
def test_a_number_the_platform_normalises_is_applied(measured, given):
    measured.coerce = {"state": lambda v: str(int(float(v)))}  # state is integer (measured): stored as "2"
    done = verbs.now_update("incident", "id1", {"state": given}, confirmed=True)
    assert done["action"] == "updated" and done["not_applied"] == [] and done["changed_fields"] == ["state"]
    # ...and asking for it again is a no-op.
    assert verbs.now_update("incident", "id1", {"state": given})["no_ops"] == ["state"]


def test_a_number_on_a_text_column_is_compared_as_text(measured):
    measured.coerce = {"short_description": lambda v: str(int(v))}
    done = verbs.now_update("incident", "id1", {"short_description": "02"}, confirmed=True)
    assert done["not_applied"] == [{"field": "short_description", "requested": "02", "actual": "2",
                                    "reason": "value_differs"}]


def test_date_given_to_a_date_time_field_reads_back_at_midnight_and_is_equal(client):
    client.coerce = {"due_date": lambda v: f"{v} 00:00:00"}
    done = verbs.now_update("incident", "id1", {"due_date": "2026-10-20"}, confirmed=True)
    assert client.db["incident"]["id1"]["due_date"] == "2026-10-20 00:00:00"
    assert done["action"] == "updated" and done["changed_fields"] == ["due_date"] and done["not_applied"] == []
    # ...and asking for that date again is a no-op, not a change.
    assert verbs.now_update("incident", "id1", {"due_date": "2026-10-20"})["no_ops"] == ["due_date"]


_INTEGER, _DECIMAL, _TEXT = {"internal_type": "integer"}, {"internal_type": "decimal"}, {"internal_type": "string"}


@pytest.mark.parametrize("given, back, column, same", [
    ("2026-10-20", "2026-10-20 00:00:00", None, True),
    ("2026-10-20", "2026-10-20 09:30:00", None, False),
    (" x ", "x", None, True),
    (True, "true", None, True),
    (False, "false", None, True),
    (None, "", None, True),
    (2, "2", None, True),
    ("2", None, None, False),
    ("", None, None, True),
    ("02", "2", _INTEGER, True),
    ("1.0", "1", _INTEGER, True),
    ("2.5", "2", _INTEGER, False),
    ("1.50", "1.5", _DECIMAL, True),
    ("02", "2", _TEXT, False),
    ("02", "2", None, False),
    ("inf", "inf", _INTEGER, True),  # equal as text; never parsed
    ("1_0", "10", _INTEGER, False),  # Decimal() would take it; ServiceNow doesn't read it as 10
    ("", "0", _INTEGER, False),
])
def test_same_value(given, back, column, same):
    assert verbs._same_value(given, back, column) is same


# ── now_insert ───────────────────────────────────────────────────────────────

def test_insert_preview_touches_nothing(client):
    before = {t: dict(rows) for t, rows in client.db.items()}
    p = verbs.now_insert("label", {"name": "Router", "bogus": "x"})
    assert p == {"confirmed": False, "action": "preview", "table": "label",
                 "values": {"name": "Router", "bogus": "x"}, "unknown_fields": ["bogus"],
                 "note": p["note"]}
    assert "nothing inserted" in p["note"] and "refuses them" in p["note"]
    assert client.records == []  # no GlideRecord at all: only the field check ran
    assert client.db == before
    assert set(client.session.meta_reads) == {"label"}


def test_insert_preview_of_known_fields(client):
    p = verbs.now_insert("incident", {"short_description": "Router down", "state": "2"})
    assert p["unknown_fields"] == [] and p["note"].endswith("to insert.")
    assert client.records == []


def test_insert_confirmed_reads_back(client):
    done = verbs.now_insert("incident", {"short_description": "Router down", "state": "2"}, confirmed=True)
    sys_id = done["sys_id"]
    assert done == {"confirmed": True, "action": "inserted", "sys_id": sys_id, "table": "incident",
                    "differences": []}
    assert client.db["incident"][sys_id]["short_description"] == "Router down"
    back = client.records[-1]
    assert back.filters == [("sys_id", sys_id)]
    assert back.fields == ["sys_id", "short_description", "state"]


def test_insert_with_a_differing_read_back_names_the_sys_id(client):
    client.drop = {"state"}
    client.coerce = {"due_date": lambda v: f"{v} 00:00:00"}  # equal by the Date/Time rule
    done = verbs.now_insert("incident", {"short_description": "x", "state": "2", "due_date": "2026-10-20"},
                            confirmed=True)
    sys_id = done["sys_id"]
    assert sys_id in client.db["incident"]
    assert done["action"] == "inserted_with_differences"
    assert done["differences"] == [{"field": "state", "requested": "2", "actual": None}]
    assert sys_id in done["note"] and "Do not retry the insert" in done["note"]


def test_insert_of_a_number_the_platform_normalises_has_no_difference(measured):
    measured.coerce = {"state": lambda v: str(int(v))}
    done = verbs.now_insert("incident", {"short_description": "x", "state": "02"}, confirmed=True)
    assert done["action"] == "inserted" and done["differences"] == []


@pytest.mark.parametrize("answer", [None, ""])
def test_insert_answered_without_a_sys_id_says_a_row_may_exist(client, answer):
    client.insert_returns = answer  # a 201 whose answer a read ACL filtered
    with pytest.raises(verbs.WriteUnconfirmed, match=r"incident: the insert was answered without a sys_id .*"
                                                     r"a row may exist\. Do not retry the insert"):
        verbs.now_insert("incident", {"short_description": "x"}, confirmed=True)


def test_insert_whose_row_cannot_be_read_back_raises_naming_it(client):
    client.unreadable = {"incident"}  # inserted, but this account can't read it
    with pytest.raises(verbs.WriteUnconfirmed, match=r"incident new5 was created.*no row came back.*"
                                                     r"Do not retry the insert"):
        verbs.now_insert("incident", {"short_description": "x"}, confirmed=True)


def test_insert_whose_read_back_errors_raises_naming_it(client):
    client.after_write_query_errors = {"incident": KeyError("result")}
    with pytest.raises(verbs.WriteUnconfirmed, match=r"incident new5 was created.*reading it back failed "
                                                     r"\(KeyError"):
        verbs.now_insert("incident", {"short_description": "x"}, confirmed=True)


def test_insert_with_unknown_fields_is_refused_and_inserts_nothing(client):
    with pytest.raises(query_check.QueryRefused, match="nothing was inserted: incident has no field bogus"):
        verbs.now_insert("incident", {"short_description": "x", "bogus": "y"}, confirmed=True)
    assert client.records == [] and len(client.db["incident"]) == 5


def test_insert_unknown_field_is_reported_only_after_a_fresh_read_of_the_columns(client, monkeypatch):
    verbs.now_insert("incident", {"short_description": "x"})  # caches the columns
    monkeypatch.setitem(GENERIC, "u_added_since", {"type": "string"})
    p = verbs.now_insert("incident", {"u_added_since": "x"})
    assert p["unknown_fields"] == []
    assert client.session.meta_reads.count("incident") == 2


def test_insert_refuses_a_choice_label_like_update(client):
    with pytest.raises(query_check.QueryRefused, match="state stores '2' for 'In Progress'"):
        verbs.now_insert("incident", {"state": "In Progress"}, confirmed=True)
    assert client.records == []


class FakeResp:
    def __init__(self, status, body, reason="Bad Request", content_type="application/json;charset=UTF-8"):
        self.status_code, self._body, self.reason = status, body, reason
        self.headers = {"Content-Type": content_type} if content_type else {}

    def json(self):
        return self._body


class FakeSession:
    """Answers UI Meta reads with GENERIC columns; records Stats API GETs and answers them with a canned response."""

    def __init__(self, resp):
        self.resp, self.gets, self.meta_reads = resp, [], []
        self.columns = {}  # table -> columns answered instead of GENERIC

    def get(self, url, params=None, headers=None, timeout=None):
        if "/api/now/ui/meta/" in url:
            table = url.rsplit("/", 1)[1]
            self.meta_reads.append(table)
            # A copy, as a real response is.
            return FakeResp(200, {"result": {"columns": dict(self.columns.get(table, GENERIC))}})
        self.gets.append((url, dict(params or {})))
        self.timeout = timeout
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


@pytest.fixture
def stats(client, monkeypatch):
    def answer(status, body):
        client.session = FakeSession(FakeResp(status, body))
        return client.session
    return answer


def test_aggregate_needs_no_glideaggregate(client, stats):
    # FakeClient, like released PySNC (1.2.1), has no GlideAggregate attribute (now-mcp#5).
    assert not hasattr(client, "GlideAggregate")
    s = stats(200, {"result": {"stats": {"count": "59"}}})
    assert verbs.now_aggregate("incident", query="state=1") == {"count": 59}
    assert s.gets == [("https://example.service-now.com/api/now/stats/incident",
                       {"sysparm_count": "true", "sysparm_query": "state=1"})]


def test_aggregate_grouped_with_labels_and_field_stats(client, stats):
    s = stats(200, {"result": [
        {"stats": {"count": "3", "avg": {"reassignment_count": "1.5"}},
         "groupby_fields": [{"field": "state", "value": "1", "display_value": "New"}]},
        {"stats": {"count": "2", "avg": {"reassignment_count": "0"}},
         "groupby_fields": [{"field": "state", "value": "", "display_value": ""}]},
    ]})
    out = verbs.now_aggregate("incident", group_by="state", avg_fields="reassignment_count")
    assert out == {"New": {"count": 3, "avg": {"reassignment_count": "1.5"}},
                   "(empty)": {"count": 2, "avg": {"reassignment_count": "0"}}}
    assert s.gets[0][1] == {"sysparm_count": "true", "sysparm_avg_fields": "reassignment_count",
                            "sysparm_group_by": "state", "sysparm_display_value": "all"}


def test_aggregate_grouped_raw_values(client, stats):
    stats(200, {"result": [{"stats": {"count": "3"},
                            "groupby_fields": [{"field": "state", "value": "1", "display_value": "New"}]}]})
    assert verbs.now_aggregate("incident", group_by="state", display_value=False) == {"1": {"count": 3}}


def test_aggregate_needs_at_least_one_statistic(client, stats):
    s = stats(200, {"result": {}})
    with pytest.raises(ValueError, match="At least one aggregate"):
        verbs.now_aggregate("incident", count=False)
    assert s.gets == []


@pytest.mark.parametrize("status, exc", [(400, ValueError), (404, ValueError), (401, RuntimeError), (403, RuntimeError)])
def test_aggregate_http_errors_name_the_status(client, stats, status, exc):
    stats(status, {"error": {"message": "Invalid table incidents"}})
    with pytest.raises(exc, match=f"HTTP {status}.*Invalid table incidents"):
        verbs.now_aggregate("incidents")


def test_run_script_preview_never_touches_the_instance(monkeypatch):
    def boom():
        raise AssertionError("preview must not connect")

    monkeypatch.setattr(verbs, "get_client", boom)
    r = verbs.now_run_script("return 1;")
    assert r["action"] == "preview" and r["confirmed"] is False


def test_run_script_endpoint_missing_executes_nothing(client):
    r = verbs.now_run_script("return 1;", confirmed=True)
    assert r["action"] == "endpoint_missing"
    assert "servicenow-coder" not in r["note"]


def _grp(field, value, label, count):
    return {"stats": {"count": str(count)},
            "groupby_fields": [{"field": field, "value": value, "display_value": label}]}


def test_grouped_labels_that_collide_keep_every_count(client, stats):
    # Two people with one name, plus a reference with no label (live on dev442835: an
    # assignment_group whose record shows a blank name collided with "no group" and lost a count).
    stats(200, {"result": [
        _grp("assigned_to", "a1b2", "John Smith", 7),
        _grp("assigned_to", "c3d4", "John Smith", 3),
        _grp("assigned_to", "e5f6", "", 1),
        _grp("assigned_to", "", "", 5),
        _grp("assigned_to", "g7h8", "Ann Lee", 2),
    ]})
    out = verbs.now_aggregate("incident", group_by="assigned_to")
    assert out == {"John Smith [a1b2]": {"count": 7}, "John Smith [c3d4]": {"count": 3},
                   "(no label) [e5f6]": {"count": 1}, "(empty)": {"count": 5}, "Ann Lee": {"count": 2}}
    assert sum(v["count"] for v in out.values()) == 18


def test_grouped_by_two_fields_qualifies_only_the_colliding_rows(client, stats):
    def row(a, b, n):
        return {"stats": {"count": str(n)}, "groupby_fields": [
            {"field": "assigned_to", "value": a[0], "display_value": a[1]},
            {"field": "priority", "value": b[0], "display_value": b[1]}]}
    stats(200, {"result": [row(("a1", "Jo"), ("1", "1 - Critical"), 4),
                           row(("b2", "Jo"), ("1", "1 - Critical"), 6),
                           row(("a1", "Jo"), ("2", "2 - High"), 1)]})
    out = verbs.now_aggregate("incident", group_by="assigned_to,priority")
    assert out == {"Jo [a1], 1 - Critical [1]": {"count": 4}, "Jo [b2], 1 - Critical [1]": {"count": 6},
                   "Jo, 2 - High": {"count": 1}}


@pytest.mark.parametrize("table", ["../table/sys_user", "incident?sysparm_query=x&", "Incident", "", "inc ident", "incident/x"])
def test_a_table_that_is_not_a_plain_name_is_refused_before_any_request(client, stats, table):
    s = stats(200, {"result": {"stats": {"count": "1"}}})
    with pytest.raises(ValueError, match="is not a table name"):
        verbs.now_aggregate(table)
    with pytest.raises(ValueError, match="is not a table name"):
        verbs.now_query(table)
    with pytest.raises(ValueError, match="is not a table name"):
        verbs.now_get(table, "id1")
    with pytest.raises(ValueError, match="is not a table name"):
        verbs.now_update(table, "id1", {"state": "2"})
    assert s.gets == []


@pytest.mark.parametrize("table", ["incident", "u_custom_2", "x_1111454_fsmchgbr_link", "cmdb_ci_ip_router"])
def test_plain_table_names_pass(client, stats, table):
    stats(200, {"result": {"stats": {"count": "1"}}})
    assert verbs.now_aggregate(table) == {"count": 1}


def test_the_stats_call_has_a_timeout_and_names_it(client, stats):
    import requests
    s = stats(200, {"result": {"stats": {"count": "1"}}})
    verbs.now_aggregate("incident")
    assert s.timeout == (10, 60)
    client.session = FakeSession(requests.ReadTimeout("read timed out"))
    with pytest.raises(RuntimeError, match="did not answer within 60s"):
        verbs.now_aggregate("incident")


def test_a_200_that_is_not_json_says_the_instance_may_be_hibernating(client):
    client.session = FakeSession(FakeResp(200, None, content_type="text/html;charset=UTF-8"))
    with pytest.raises(RuntimeError, match="text/html.*not JSON.*hibernating"):
        verbs.now_aggregate("incident")


def test_an_unreachable_instance_is_named_not_a_raw_connection_error(client, stats):
    import requests
    stats(200, {"result": {"stats": {"count": "1"}}})
    client.session = FakeSession(requests.ConnectionError("Name or service not known"))
    with pytest.raises(RuntimeError, match="could not reach the instance.*SN_INSTANCE"):
        verbs.now_aggregate("incident")


class DownSession:
    """An instance that's down: every URL raises, UI Meta included (the path a real outage takes)."""

    def __init__(self, exc):
        self.exc, self.urls = exc, []

    def get(self, url, **kwargs):
        self.urls.append(url)
        raise self.exc


@pytest.mark.parametrize("verb", ["now_aggregate", "now_query"])
def test_an_unreachable_instance_is_named_when_the_field_check_fails_first(client, verb):
    import requests
    client.session = DownSession(requests.ConnectionError("Name or service not known"))
    with pytest.raises(query_check.FieldsUnreadable, match="could not reach the instance.*SN_INSTANCE.*Nothing was run"):
        getattr(verbs, verb)("incident", query="state=1")
    assert client.session.urls and all("/api/now/ui/meta/" in u for u in client.session.urls)


def test_a_ui_meta_timeout_is_not_called_unreachable(client):
    import requests
    client.session = DownSession(requests.ConnectTimeout("timed out"))  # both a Timeout and a ConnectionError
    with pytest.raises(query_check.FieldsUnreadable, match="could not read the fields.*ConnectTimeout") as caught:
        verbs.now_aggregate("incident", query="state=1")
    assert "could not reach" not in str(caught.value)


# SSLError and ProxyError are ConnectionErrors too. "Check SN_INSTANCE" is the wrong advice for
# them, and the exception text is the diagnosis (a TLS-inspecting proxy's self-signed chain), so
# it must survive (now-mcp#11).
_CERT = "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in certificate chain"


def _transport_cases():
    import requests
    return [
        (requests.exceptions.SSLError(_CERT), ["TLS connection", "CERTIFICATE_VERIFY_FAILED", "REQUESTS_CA_BUNDLE"]),
        (requests.exceptions.ProxyError("Unable to connect to proxy: 407 Proxy Authentication Required"),
         ["proxy failed", "407 Proxy Authentication Required", "HTTPS_PROXY"]),
        (requests.ConnectionError("Name or service not known"),
         ["could not reach the instance", "Name or service not known", "SN_INSTANCE"]),
    ]


@pytest.mark.parametrize("case", range(3), ids=["ssl", "proxy", "plain"])
def test_a_transport_failure_at_the_field_check_keeps_its_cause(client, case):
    exc, wanted = _transport_cases()[case]
    client.session = DownSession(exc)
    with pytest.raises(query_check.FieldsUnreadable) as caught:
        verbs.now_aggregate("incident", query="state=1")
    msg = str(caught.value)
    assert all(w in msg for w in wanted), msg
    assert ("SN_INSTANCE" in msg) == (case == 2), msg  # only a plain ConnectionError points at the name


@pytest.mark.parametrize("case", range(3), ids=["ssl", "proxy", "plain"])
def test_a_transport_failure_at_the_stats_call_keeps_its_cause(client, case):
    exc, wanted = _transport_cases()[case]
    client.session = FakeSession(exc)  # UI Meta answers; only the Stats call fails
    with pytest.raises(RuntimeError) as caught:
        verbs.now_aggregate("incident")
    msg = str(caught.value)
    assert msg.startswith("Stats API on incident: ") and all(w in msg for w in wanted), msg
    assert ("SN_INSTANCE" in msg) == (case == 2), msg
