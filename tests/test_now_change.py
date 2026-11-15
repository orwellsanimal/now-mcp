"""now_data.change against a fake guard and fake now-data verbs (no network, now-mcp#22)."""

from pathlib import Path

import pytest
from pysnc import exceptions as px

from now_data import change

STANDARD, NORMAL = "a" * 32, "b" * 32
PRODUCER, VERSION = "c" * 32, "d" * 32
CHG = "e" * 32
CI = "f" * 32


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    # No .env from the checkout leaks in, and no cached guard URL from another test.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(change.FLAG, raising=False)
    monkeypatch.delenv(change.GUARD_URI, raising=False)
    monkeypatch.setattr(change, "_guard_base", None)


class Fake:
    """The guard's answers and the now-data verbs the change verbs call, recorded."""

    def __init__(self):
        self.allowed, self.reasons = True, []
        self.checks, self.inserts, self.updates = [], [], []
        self.write_error = None
        self.rows = {
            "chg_model": [{"sys_id": STANDARD, "name": "Standard"}, {"sys_id": NORMAL, "name": "Normal"}],
            "std_change_record_producer": [{"sys_id": PRODUCER, "name": "Reboot", "current_version": VERSION,
                                            "short_description": "Reboot a server"}],
            "change_request": [{"sys_id": CHG, "number": "CHG0000123", "state": "-5"}],
        }

    def guard(self, method, path, body=None):
        if path == "/policy":
            return {"governed": True, "active": True, "allowed_cis": [{"sys_id": CI}]}
        self.checks.append(body)
        return {"governed": True, "allowed": self.allowed, "reasons": list(self.reasons)}

    def now_query(self, table, query="", fields="", limit=50, display_value=False):
        terms = [t.split("=", 1) for t in query.split("^") if "=" in t and not t.startswith("ORDERBY")]
        rows = [r for r in self.rows.get(table, [])
                if all(k == "active" or str(r.get(k)) == v for k, v in terms)]
        return {"count": len(rows[:limit]), "truncated": False, "records": rows[:limit]}

    def now_insert(self, table, values, confirmed=False):
        if self.write_error:
            raise self.write_error
        self.inserts.append((table, values, confirmed))
        self.rows["change_request"].append({"sys_id": "1" * 32, "number": "CHG0000124", "state": "-5"})
        return {"confirmed": True, "action": "inserted", "sys_id": "1" * 32, "table": table}

    def now_update(self, table, sys_id, values, confirmed=False):
        if self.write_error:
            raise self.write_error
        self.updates.append((table, sys_id, values, confirmed))
        return {"confirmed": True, "action": "updated", "sys_id": sys_id, "table": table}


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setenv(change.FLAG, "1")
    monkeypatch.setattr(change, "_guard", f.guard)
    for name in ("now_query", "now_insert", "now_update"):
        monkeypatch.setattr(change.verbs, name, getattr(f, name))
    return f


# ── the flag ──────────────────────────────────────────────────────────────────

CALLS = {
    "now_change_policy": {},
    "now_change_get": {"change": "CHG0000123"},
    "now_change_preview": {"values": {"state": "-4"}},
    "now_change_create_standard": {"template": "Reboot", "cmdb_ci": CI},
    "now_change_create_normal": {"cmdb_ci": CI, "short_description": "s", "justification": "j",
                                 "implementation_plan": "i", "backout_plan": "b", "test_plan": "t"},
    "now_change_move": {"change": "CHG0000123", "to": "assess"},
}


def test_every_verb_is_covered_by_the_flag_test():
    assert set(CALLS) == set(change.__all__)


@pytest.mark.parametrize("verb", sorted(CALLS))
def test_off_by_default_every_verb_refuses_before_touching_the_instance(monkeypatch, verb):
    def boom(*a, **k):
        raise AssertionError("a disabled verb must not reach the instance")

    monkeypatch.setattr(change, "get_client", boom)
    monkeypatch.setattr(change, "_guard", boom)
    for name in ("now_query", "now_get", "now_insert", "now_update"):
        monkeypatch.setattr(change.verbs, name, boom)
    with pytest.raises(change.ChangeVerbsDisabled, match=change.FLAG):
        getattr(change, verb)(**CALLS[verb])


@pytest.mark.parametrize("value, on", [("1", True), ("true", True), (" YES ", True),
                                       ("0", False), ("", False), ("no", False), ("enabled", False)])
def test_flag_values(monkeypatch, value, on):
    monkeypatch.setenv(change.FLAG, value)
    assert change.enabled() is on


def test_flag_from_dotenv(tmp_path):
    assert change.enabled() is False
    Path(tmp_path, ".env").write_text(f"{change.FLAG}=1\n", encoding="utf-8")
    assert change.enabled() is True


# ── writes ask the guard first ────────────────────────────────────────────────

def test_create_standard_preview_asks_the_guard_and_writes_nothing(fake):
    r = change.now_change_create_standard("Reboot", CI)
    assert (r["action"], r["allowed"], fake.inserts) == ("preview", True, [])
    assert fake.checks == [{"values": {
        "type": "standard", "chg_model": STANDARD, "std_change_producer_version": VERSION,
        "cmdb_ci": CI, "short_description": "Reboot a server"}}]


def test_create_standard_by_sys_id_and_unknown_template(fake):
    assert change.now_change_create_standard(PRODUCER, CI)["values"]["std_change_producer_version"] == VERSION
    with pytest.raises(ValueError, match="No active standard change template"):
        change.now_change_create_standard("Nope", CI)


def test_confirmed_write_the_guard_refuses_is_not_sent(fake):
    fake.allowed, fake.reasons = False, ["this configuration item is not in the agent policy"]
    r = change.now_change_create_standard("Reboot", "9" * 32, confirmed=True)
    assert r["action"] == "refused" and r["reasons"] == fake.reasons
    assert fake.inserts == []


def test_confirmed_create_inserts_and_names_the_change(fake):
    r = change.now_change_create_normal(CI, "s", "j", "i", "b", "t", assignment_group="g", confirmed=True)
    assert r["action"] == "inserted" and r["number"] == "CHG0000124" and r["state"] == "new"
    table, values, confirmed = fake.inserts[0]
    assert (table, confirmed) == ("change_request", True)
    assert values["type"] == "normal" and values["chg_model"] == NORMAL and values["assignment_group"] == "g"


def test_refused_at_write_time_returns_the_guards_current_reasons(fake):
    # Allowed on the check, refused by the business rule a moment later.
    checks = iter([{"governed": True, "allowed": True, "reasons": []},
                   {"governed": True, "allowed": False, "reasons": ["an agent may not mark its own change approved"]}])
    change._guard = lambda m, p, b=None: next(checks)
    fake.write_error = px.RoleException("403 - aborted")
    r = change.now_change_move("CHG0000123", "assess", confirmed=True)
    assert r["action"] == "refused" and r["reasons"] == ["an agent may not mark its own change approved"]


def test_refused_by_a_stock_rule_names_the_rule(fake):
    # The guard allows it both times; the change model's transition check aborts it.
    fake.write_error = px.RoleException({"error": {"message": "Operation Failed", "detail":
                                         "Operation against file 'change_request' was aborted by Business "
                                         "Rule 'Change Model: Check State Transition'"}, "status": "failure"})
    r = change.now_change_move("CHG0000123", "scheduled", confirmed=True)
    assert r["action"] == "refused_by_instance" and "Check State Transition" in r["detail"]
    assert "assignment_group" in r["note"]


def test_a_401_is_not_taken_for_a_refusal(fake):
    fake.write_error = px.AuthenticationException("401")
    with pytest.raises(px.AuthenticationException):
        change.now_change_move("CHG0000123", "assess", confirmed=True)


def test_move_maps_states_and_passes_the_window(fake):
    r = change.now_change_move("CHG0000123", "Scheduled", start_date="2026-11-16 10:00:00",
                               end_date="2026-11-16 12:00:00")
    assert r["action"] == "preview"
    assert fake.checks[-1] == {"sys_id": CHG, "values": {
        "state": "-2", "start_date": "2026-11-16 10:00:00", "end_date": "2026-11-16 12:00:00"}}
    change.now_change_move(CHG, "closed", close_code="successful", close_notes="done", confirmed=True)
    assert fake.updates[-1] == ("change_request", CHG,
                                {"state": "3", "close_code": "successful", "close_notes": "done"}, True)
    with pytest.raises(ValueError, match="Unknown state"):
        change.now_change_move(CHG, "approved")


def test_unknown_change_number(fake):
    with pytest.raises(ValueError, match="No change_request"):
        change.now_change_move("CHG9999999", "assess")
    with pytest.raises(ValueError, match="No change_request"):
        change.now_change_move("number=CHG0000123^ORstate=1", "assess")


# ── finding the guard ─────────────────────────────────────────────────────────

class GuardGR:
    def __init__(self, rows):
        self.rows, self.filters = rows, []

    def add_query(self, f, v):
        self.filters.append((f, v))

    def query(self):
        self.hits = [r for r in self.rows if all(r.get(f) == v for f, v in self.filters)]

    def next(self):
        return bool(self.hits)

    def get_value(self, f):
        return self.hits[0].get(f)


class GuardClient:
    instance = "https://example.service-now.com"

    def __init__(self, rows):
        self.rows = rows

    def GlideRecord(self, table):
        assert table == "sys_ws_definition"
        return GuardGR(self.rows)


def test_the_guard_is_found_by_name_and_service_id():
    rows = [{"name": "Agent Change Guard", "service_id": "guard", "base_uri": "/api/x_acme_chg/guard"}]
    assert change._guard_url(GuardClient(rows)) == "https://example.service-now.com/api/x_acme_chg/guard"


def test_the_guard_uri_setting_wins_and_is_checked(monkeypatch):
    monkeypatch.setenv(change.GUARD_URI, "/api/x_acme_chg/guard")
    assert change._guard_url(GuardClient([])) == "https://example.service-now.com/api/x_acme_chg/guard"
    monkeypatch.setattr(change, "_guard_base", None)
    monkeypatch.setenv(change.GUARD_URI, "https://evil.example/api/x/guard")
    with pytest.raises(ValueError, match="not a REST base path"):
        change._guard_url(GuardClient([]))


def test_an_account_that_cant_read_sys_ws_definition_is_told_what_to_set():
    class Hidden(GuardGR):
        def query(self):
            raise px.RoleException("User Not Authorized")

    class HiddenClient(GuardClient):
        def GlideRecord(self, table):
            return Hidden([])

    with pytest.raises(change.GuardMissing, match=change.GUARD_URI):
        change._guard_url(HiddenClient([]))


def test_no_guard_on_the_instance():
    with pytest.raises(change.GuardMissing, match="nothing was checked or written"):
        change._guard_url(GuardClient([{"name": "Other", "service_id": "guard", "base_uri": "/api/x/guard"}]))


def test_no_instance_names_in_the_module():
    # The guard is found by setting or by lookup; only the docs' example scope may appear.
    import re

    src = Path(change.__file__).read_text(encoding="utf-8")
    assert set(re.findall(r"\bx_[a-z0-9]+_[a-z0-9_]+", src)) <= {"x_acme_chg"}
    assert not re.search(r"\bdev[0-9]{4,}\b", src)
