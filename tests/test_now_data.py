"""now-data verbs against an in-memory fake PySNC client (no network)."""

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
        if self.table in self.client.query_errors:
            raise self.client.query_errors[self.table]
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
            if GENERIC.get(f, {}).get("internal_type") == "journal_input":
                rec[f] = ""  # a journal field reads back empty...
                if c.journal_writes:  # ...and its entry goes to the journal
                    n = len(self.db.setdefault("sys_journal_field", {}))
                    self.db["sys_journal_field"][f"j{n}"] = {
                        "sys_id": f"j{n}", "element_id": sys_id, "element": f,
                        "value": str(v).replace("\n", "\r\n") + "\r\n"}
                continue
            rec[f] = c.coerce.get(f, lambda x: x)(v)

    def update(self):
        if self.client.update_returns is not REAL:
            return self.client.update_returns
        self._store(self._rec, self._rec["sys_id"])
        return self._rec["sys_id"]

    def initialize(self):
        self._rec, self.updates = {}, {}

    def insert(self):
        if self.client.insert_returns is not REAL:
            return self.client.insert_returns
        rows = self.db.setdefault(self.table, {})
        sys_id = f"new{len(rows)}"
        rows[sys_id] = {"sys_id": sys_id}
        self._store(rows[sys_id], sys_id)
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
        # rule, and still a 200), `coerce` maps a value to what is stored, and journal fields add a
        # sys_journal_field entry unless `journal_writes` is off.
        self.drop, self.coerce, self.journal_writes = set(), {}, True
        # Reads: a table in `unreadable` queries as no rows (ACL-hidden); `query_errors` raise.
        self.unreadable, self.query_errors = set(), {}
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


def test_query_truncates_and_clamps(client):
    r = verbs.now_query("incident", "state=1", limit=3)
    assert (r["count"], r["truncated"]) == (3, True)
    assert verbs.now_query("incident", limit=10)["truncated"] is False
    assert verbs.now_query("incident", limit=0)["count"] == 1  # clamped up to 1


def test_get_missing_raises_value_error(client):
    assert verbs.now_get("incident", "id1")["short_description"] == "x1"
    with pytest.raises(ValueError):
        verbs.now_get("incident", "nope")


def test_update_previews_by_default_and_writes_only_when_confirmed(client):
    p = verbs.now_update("incident", "id1", {"state": "2", "short_description": "x1", "bogus": "y"})
    assert p["action"] == "preview"
    assert p["changes"] == [{"field": "state", "current": "1", "proposed": "2"}]
    assert p["no_ops"] == ["short_description"]
    assert p["unknown_fields"] == ["bogus"]
    assert client.db["incident"]["id1"]["state"] == "1"  # nothing written

    done = verbs.now_update("incident", "id1", {"state": "2", "bogus": "y"}, confirmed=True)
    assert done["action"] == "updated" and done["changed_fields"] == ["state"]
    assert done["not_applied"] == [] and done["journal_unconfirmed"] == [] and "note" not in done
    assert client.db["incident"]["id1"]["state"] == "2"
    assert "bogus" not in client.db["incident"]["id1"]


# ── now-mcp#3: a confirmed write is read back ────────────────────────────────

def test_update_a_field_that_does_not_take_is_not_applied(client):
    # A business rule keeps state (setAbortAction on that field's change), and the instance still answers 200.
    client.drop = {"state"}
    done = verbs.now_update("incident", "id1", {"state": "2", "short_description": "moved"}, confirmed=True)
    assert done["action"] == "partially_applied"
    assert done["changed_fields"] == ["short_description"]
    assert done["not_applied"] == [{"field": "state", "requested": "2", "actual": "1"}]
    assert "state" in done["note"] and "business rule" in done["note"]

    nothing = verbs.now_update("incident", "id2", {"state": "2"}, confirmed=True)
    assert nothing["action"] == "not_applied" and nothing["changed_fields"] == []
    assert nothing["not_applied"] == [{"field": "state", "requested": "2", "actual": "1"}]


def test_update_reads_back_by_sys_id_only_the_fields_it_wrote(client):
    verbs.now_update("incident", "id1", {"state": "2", "short_description": "y"}, confirmed=True)
    back = client.records[-1]
    assert back.filters == [("sys_id", "id1")]
    assert back.fields == ["sys_id", "state", "short_description"]


def test_update_a_no_op_that_a_rule_changes_is_not_applied_not_a_no_op(client):
    client.coerce = {"short_description": lambda v: "rewritten by a rule"}
    done = verbs.now_update("incident", "id1", {"state": "2", "short_description": "x1"}, confirmed=True)
    # short_description already held x1 and was not sent as a change; the rule changed it anyway.
    assert done["not_applied"] == [{"field": "short_description", "requested": "x1", "actual": "rewritten by a rule"}]
    assert done["no_ops"] == [] and done["changed_fields"] == ["state"]
    assert done["action"] == "partially_applied"


@pytest.mark.parametrize("answer", [None, "", "some-other-sys-id"])
def test_update_not_confirmed_by_update_raises(client, answer):
    client.update_returns = answer
    with pytest.raises(RuntimeError, match=r"incident id1: not saved; the instance did not confirm the update"):
        verbs.now_update("incident", "id1", {"state": "2"}, confirmed=True)


def test_update_that_cannot_be_read_back_raises_and_says_it_was_accepted(client):
    client.unreadable = {"incident"}  # get() finds it; the read-back query sees no row
    with pytest.raises(RuntimeError, match=r"incident id1: the update was accepted, but no row came back.*"
                                           r"Check the record before retrying"):
        verbs.now_update("incident", "id1", {"state": "2"}, confirmed=True)


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
    # The same note was written last week; this write leaves no new entry (e.g. it can't be read).
    client.db["sys_journal_field"] = {"old": {"sys_id": "old", "element_id": "id1", "element": "work_notes",
                                              "value": "Customer contacted"}}
    client.journal_writes = False
    done = verbs.now_update("incident", "id1", {"work_notes": "Customer contacted", "state": "2"}, confirmed=True)
    assert done["journal_unconfirmed"] == ["work_notes"]
    assert done["not_applied"] == []  # unconfirmed is not "failed": it may have landed
    assert done["changed_fields"] == ["state"]
    assert done["action"] == "updated"
    assert "could not be confirmed" in done["note"] and "duplicated" in done["note"]


def test_journal_entry_on_another_record_or_field_does_not_confirm(client):
    client.journal_writes = False
    client.db["sys_journal_field"] = {
        "a": {"sys_id": "a", "element_id": "id2", "element": "work_notes", "value": "hi"},
        "b": {"sys_id": "b", "element_id": "id1", "element": "comments", "value": "hi"},
    }
    done = verbs.now_update("incident", "id1", {"work_notes": "hi"}, confirmed=True)
    assert done["journal_unconfirmed"] == ["work_notes"]


def test_unconfirmed_journal_with_a_field_not_applied_is_partially_applied(client):
    # Nothing confirmed landed, but the note may have: "not_applied" would invite retrying it.
    client.journal_writes, client.drop = False, {"state"}
    done = verbs.now_update("incident", "id1", {"work_notes": "note", "state": "2"}, confirmed=True)
    assert done["changed_fields"] == [] and done["journal_unconfirmed"] == ["work_notes"]
    assert done["action"] == "partially_applied"


def test_journal_unreadable_before_the_write_writes_nothing(client):
    client.query_errors = {"sys_journal_field": RuntimeError("403 Forbidden")}  # pysnc raises on a 403
    with pytest.raises(RuntimeError, match="403"):
        verbs.now_update("incident", "id1", {"work_notes": "note", "state": "2"}, confirmed=True)
    assert client.db["incident"]["id1"]["state"] == "1"
    assert "sys_journal_field" not in client.db


def test_journal_type_comes_from_the_column_type_not_the_name(client, monkeypatch):
    # A custom journal field is confirmed by the journal; a string field named like one is compared.
    monkeypatch.setitem(GENERIC, "u_notes", {"type": "journal_input", "internal_type": "journal_input"})
    monkeypatch.setitem(GENERIC, "u_work_notes", {"type": "string"})
    client.db["incident"]["id1"].update({"u_notes": "", "u_work_notes": ""})
    client.drop = {"u_work_notes"}
    done = verbs.now_update("incident", "id1", {"u_notes": "a", "u_work_notes": "b"}, confirmed=True)
    assert done["changed_fields"] == ["u_notes"]  # read back empty, confirmed by its new entry
    assert done["journal_unconfirmed"] == []
    assert done["not_applied"] == [{"field": "u_work_notes", "requested": "b", "actual": ""}]


def test_date_given_to_a_date_time_field_reads_back_at_midnight_and_is_equal(client):
    client.coerce = {"due_date": lambda v: f"{v} 00:00:00"}
    done = verbs.now_update("incident", "id1", {"due_date": "2026-10-20"}, confirmed=True)
    assert client.db["incident"]["id1"]["due_date"] == "2026-10-20 00:00:00"
    assert done["action"] == "updated" and done["changed_fields"] == ["due_date"] and done["not_applied"] == []
    # ...and asking for that date again is a no-op, not a change.
    assert verbs.now_update("incident", "id1", {"due_date": "2026-10-20"})["no_ops"] == ["due_date"]


@pytest.mark.parametrize("given, back, same", [
    ("2026-10-20", "2026-10-20 00:00:00", True),
    ("2026-10-20", "2026-10-20 09:30:00", False),
    (" x ", "x", True),
    (True, "true", True),
    (False, "false", True),
    (None, "", True),
    (2, "2", True),
    ("2", None, False),
    ("", None, True),
])
def test_same_value(given, back, same):
    assert verbs._same_value(given, back) is same


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


def test_insert_without_a_sys_id_raises(client):
    client.insert_returns = None
    with pytest.raises(RuntimeError, match="incident: not inserted; the instance returned no sys_id"):
        verbs.now_insert("incident", {"short_description": "x"}, confirmed=True)


def test_insert_whose_row_cannot_be_read_back_raises_naming_it(client):
    client.unreadable = {"incident"}  # inserted, but this account can't read it
    with pytest.raises(RuntimeError, match=r"incident new5 was created.*no row came back.*"
                                           r"Do not retry the insert"):
        verbs.now_insert("incident", {"short_description": "x"}, confirmed=True)


def test_insert_whose_read_back_errors_raises_naming_it(client):
    client.query_errors = {"incident": KeyError("result")}
    with pytest.raises(RuntimeError, match=r"incident new5 was created.*reading it back failed \(KeyError"):
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

    def get(self, url, params=None, headers=None, timeout=None):
        if "/api/now/ui/meta/" in url:
            self.meta_reads.append(url.rsplit("/", 1)[1])
            return FakeResp(200, {"result": {"columns": dict(GENERIC)}})  # a copy, as a real response is
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
