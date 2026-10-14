"""now-data verbs against an in-memory fake PySNC client (no network)."""

import pytest

from now_data import query_check, verbs

# What the fake instance's UI Meta API answers (now-mcp#4 checks every request against it).
STATE = [{"value": str(v), "label": l} for v, l in ((1, "New"), (2, "In Progress"), (6, "Resolved"), (7, "Closed"))]
GENERIC = {
    "sys_id": {"type": "GUID"}, "number": {"type": "string"}, "short_description": {"type": "string"},
    "state": {"type": "choice", "internal_type": "integer", "choices": STATE},
    "priority": {"type": "choice", "internal_type": "integer", "choices": [{"value": "1", "label": "1 - Critical"}]},
    "active": {"type": "boolean", "internal_type": "boolean"},
    "assigned_to": {"type": "reference", "reference": "sys_user"},
    "assignment_group": {"type": "reference", "reference": "sys_user_group"},
    "reassignment_count": {"type": "integer", "internal_type": "integer"},
}


@pytest.fixture(autouse=True)
def _fresh_column_cache():
    query_check._COLUMNS.clear()
    yield
    query_check._COLUMNS.clear()


class FakeGR:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.fields, self.limit, self.encoded = None, None, None
        self._rec, self.updates = None, {}

    def add_encoded_query(self, q):
        self.encoded = q

    def add_query(self, field, value):
        self.encoded = f"{field}={value}"

    def query(self):
        self._rows = list(self.db.get(self.table, {}).values())
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

    def update(self):
        self._rec.update(self.updates)


class FakeClient:
    instance = "https://example.service-now.com"

    def __init__(self, db):
        self.db = db
        self.posts = []
        self.session = FakeSession(None)

    def GlideRecord(self, table):
        return FakeGR(self.db, table)


@pytest.fixture
def client(monkeypatch):
    db = {"incident": {f"id{i}": {"sys_id": f"id{i}", "state": "1", "short_description": f"x{i}"} for i in range(5)}}
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
    assert client.db["incident"]["id1"]["state"] == "2"
    assert "bogus" not in client.db["incident"]["id1"]


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
            return FakeResp(200, {"result": {"columns": GENERIC}})
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
