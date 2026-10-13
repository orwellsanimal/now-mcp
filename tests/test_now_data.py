"""now-data verbs against an in-memory fake PySNC client (no network)."""

import pytest

from now_data import verbs


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


def test_aggregate_without_glideaggregate_raises_clearly(client):
    with pytest.raises(NotImplementedError, match="GlideAggregate"):
        verbs.now_aggregate("incident", group_by="state")


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
