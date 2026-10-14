"""now-mcp#4: refuse a request ServiceNow would silently drop or misread.

Ported from whitcortex tests/test_2623_stats_refuses_unapplied_queries.py (PR #2626).
Each shape below was measured against a developer instance there before the rule
was written. Receipts on that instance:
  'state=new'          -> 0   (state stores 1)
  'nonsense_field=xyz' -> every incident
  'assigned_to.bogus=x'-> every incident; group_by=bogus_field -> one empty group of all.
Measured again through now-data 0.3.0 (now-mcp#4): state=new -> 0 (59 really),
nonsense_field=xyz -> 122 (the whole table), and now_query unfiltered.

The fake answers /ui/meta/<table> from COLUMNS and RAISES if the Stats or Table API
is reached on a path that must refuse, so a missed refusal fails loudly.
"""

import pytest

from now_data import query_check, verbs
from now_data.query_check import FieldsUnreadable, QueryRefused

STATE_CHOICES = [
    {"value": "1", "label": "New"}, {"value": "2", "label": "In Progress"},
    {"value": "3", "label": "On Hold"}, {"value": "6", "label": "Resolved"},
    {"value": "7", "label": "Closed"}, {"value": "8", "label": "Canceled"},
]

COLUMNS = {
    "incident": {
        "state": {"type": "choice", "internal_type": "integer", "choices": STATE_CHOICES},
        "category": {"type": "choice", "internal_type": "string", "choices": [
            {"value": "hardware", "label": "Hardware"}, {"value": "network", "label": "Network"}]},
        "priority": {"type": "choice", "internal_type": "integer", "choices": [
            {"value": "", "label": "-- None --"}, {"value": "1", "label": "1 - Critical"}]},
        "active": {"type": "boolean", "internal_type": "boolean"},
        "number": {"type": "string"},
        "short_description": {"type": "string"},
        "opened_at": {"type": "glide_date_time"},
        "reassignment_count": {"type": "integer"},
        "assigned_to": {"type": "reference", "reference": "sys_user"},
        "assignment_group": {"type": "reference", "reference": "sys_user_group"},
        "caller_id": {"type": "reference", "reference": "sys_user"},
        "opened_by": {"type": "reference", "reference": "sys_user"},
        "sys_class_name": {"type": "sys_class_name"},
    },
    "sys_user": {"user_name": {"type": "string"}},
    "sys_user_group": {"name": {"type": "string"}},
}


class Resp:
    def __init__(self, status, body, content_type="application/json"):
        self.status_code, self._body = status, body
        self.headers = {"Content-Type": content_type}
        self.reason = "x"

    def json(self):
        return self._body


class Record:
    def __init__(self, fake, table):
        self.fake, self.table = fake, table
        self.encoded, self.fields, self.limit = None, None, None

    def add_encoded_query(self, q):
        self.encoded = q

    def query(self):
        self.fake.reached("table", self.encoded)

    def serialize_all(self, display_value=False, fields=None):
        return []

    def get(self, sys_id):
        self.fake.reached("get", sys_id)
        return True

    def serialize(self, display_value=False, fields=None):
        return {"sys_id": "id1", "state": "1"}

    def set_value(self, f, v):
        self.fake.writes.append((f, v))

    def update(self):
        pass


class FakeInstance:
    def __init__(self, allowed=True, meta_status=200, meta_error=None):
        self.instance = "https://dev2623.service-now.com"
        self.allowed, self.meta_status, self.meta_error = allowed, meta_status, meta_error
        self.columns = {t: dict(c) for t, c in COLUMNS.items()}
        self.meta_reads, self.calls, self.writes = [], [], []
        self.session = self

    def reached(self, kind, what):
        if not self.allowed:
            raise AssertionError(f"the instance was queried ({kind}) for a request that must refuse: {what}")
        self.calls.append((kind, what))

    def get(self, url, params=None, headers=None, timeout=None):
        if "/api/now/ui/meta/" in url:
            table = url.rsplit("/", 1)[1]
            self.meta_reads.append(table)
            if self.meta_error:
                raise self.meta_error
            # A copy, as a real response is: the cache must not see later edits for free.
            return Resp(self.meta_status, {"result": {"columns": dict(self.columns.get(table, {}))}})
        assert "/api/now/stats/" in url, url
        self.reached("stats", dict(params))
        if params.get("sysparm_group_by"):
            return Resp(200, {"result": [{"stats": {"count": "5"},
                                          "groupby_fields": [{"field": params["sysparm_group_by"],
                                                              "value": "1", "display_value": "1 - Critical"}]}]})
        return Resp(200, {"result": {"stats": {"count": "28"}}})

    def GlideRecord(self, table):
        return Record(self, table)

    @property
    def stats_params(self):
        return [what for kind, what in self.calls if kind == "stats"]


@pytest.fixture(autouse=True)
def _fresh_column_cache():
    query_check._COLUMNS.clear()
    yield
    query_check._COLUMNS.clear()


@pytest.fixture
def refusing(monkeypatch):
    fake = FakeInstance(allowed=False)
    monkeypatch.setattr(verbs, "get_client", lambda: fake)
    return fake


@pytest.fixture
def counting(monkeypatch):
    fake = FakeInstance()
    monkeypatch.setattr(verbs, "get_client", lambda: fake)
    return fake


def count(query="", **kw):
    return verbs.now_aggregate(kw.pop("table", "incident"), query=query, **kw)


def _refusal(call) -> str:
    with pytest.raises(QueryRefused) as caught:
        call()
    message = str(caught.value)
    assert message.startswith("this request would give a wrong or unchecked result") or \
        "is not a table" in message, message
    return message


# ── The issue's receipts ────────────────────────────────────────────────────

@pytest.mark.parametrize("query, names", [
    ("state = 'new'", "spaces or quotes"),
    ("state=new", "state stores '1' for 'new': use state=1"),
    ("nonsense_field=xyz", "'nonsense_field' is not a field of incident"),
])
def test_issue_receipts_refuse_by_name(refusing, query, names):
    assert names in _refusal(lambda: count(query))


@pytest.mark.parametrize("query", ["state=1", "active=true"])
def test_issue_receipts_that_were_right_still_count_unchanged(counting, query):
    assert count(query) == {"count": 28}
    assert counting.stats_params == [{"sysparm_count": "true", "sysparm_query": query}]


def test_a_refusal_is_a_value_error(refusing):
    # Consumers that map ValueError to a refusal the model can act on (rio's _call) get it as one.
    with pytest.raises(ValueError):
        count("state=new")


# ── Every verb is checked, not just the count ──────────────────────────────

def test_now_query_refuses_an_unknown_field_instead_of_returning_the_whole_table(refusing):
    assert "'nonsense_field' is not a field" in _refusal(
        lambda: verbs.now_query("incident", query="nonsense_field=xyz"))


def test_now_query_refuses_an_unknown_field_in_fields(refusing):
    assert "fields: 'nmber' is not a field" in _refusal(
        lambda: verbs.now_query("incident", fields="number,nmber"))


def test_now_query_with_a_valid_query_runs_it_verbatim(counting):
    verbs.now_query("incident", query="state=1^active=true", fields="number,state")
    assert counting.calls == [("table", "state=1^active=true")]


def test_now_get_refuses_an_unknown_field_in_fields(refusing):
    assert "fields: 'bogus' is not a field" in _refusal(
        lambda: verbs.now_get("incident", "id1", fields="number,bogus"))


def test_now_update_refuses_a_choice_label_and_names_the_value(refusing):
    message = _refusal(lambda: verbs.now_update("incident", "id1", {"state": "In Progress"}))
    assert "values: state stores '2' for 'In Progress': use state=2" in message


def test_now_update_with_a_stored_choice_value_previews(counting):
    p = verbs.now_update("incident", "id1", {"state": "2", "bogus": "x"})
    assert p["action"] == "preview" and p["unknown_fields"] == ["bogus"]
    assert counting.writes == []


def test_now_update_refusal_writes_nothing_even_when_confirmed(refusing):
    _refusal(lambda: verbs.now_update("incident", "id1", {"state": "open"}, confirmed=True))
    assert refusing.writes == []


# ── Choice fields: labels and unknown values ────────────────────────────────

def test_label_inside_an_in_list_names_every_stored_value(refusing):
    message = _refusal(lambda: count("stateINNew,In Progress,7"))
    assert "'1' for 'New', '2' for 'In Progress'" in message
    assert "use stateIN1,2,7" in message


def test_underscore_label_form_is_mapped(refusing):
    assert "use state=2" in _refusal(lambda: count("state=in_progress"))


def test_value_that_is_neither_stored_nor_a_label_lists_the_values(refusing):
    message = _refusal(lambda: count("state!=open"))
    assert "'open' is not a stored value of state" in message
    assert "1 (New)" in message and "8 (Canceled)" in message


def test_empty_stored_value_is_a_value(counting):
    count("priority=")
    assert counting.stats_params


# ── Dot-walks, OR / NQ / ORDERBY terms ──────────────────────────────────────

def test_unknown_field_after_a_dot_walk_refuses(refusing):
    assert "'bogus' is not a field of sys_user" in _refusal(lambda: count("assigned_to.bogus=x"))


def test_dot_walk_through_a_non_reference_refuses(refusing):
    assert "is not a reference" in _refusal(lambda: count("number.user_name=x"))


def test_unknown_field_in_an_or_term_refuses(refusing):
    assert "'nonsense' is not a field" in _refusal(lambda: count("active=true^ORnonsense=1"))


@pytest.mark.parametrize("query", [
    "assigned_to.user_name=admin",
    "ORDERBYnumber",                     # starts with "OR": must not be read as an OR term
    "ORDERBYDESCopened_at",
    "state=1^NQpriority=1",
    "state=1^ORstate=2^EQ",
    "stateIN1,2",
    "short_descriptionLIKEnetwork",
    "opened_at>javascript:gs.daysAgoStart(7)",
    "123TEXTQUERY321=printer",
])
def test_valid_queries_reach_the_stats_api_verbatim(counting, query):
    count(query)
    assert counting.stats_params[-1]["sysparm_query"] == query


def test_orderby_with_an_unknown_field_refuses(refusing):
    assert "'bogus' is not a field" in _refusal(lambda: count("ORDERBYDESCbogus"))


# ── group_by and aggregate fields ──────────────────────────────────────────

def test_unknown_group_by_refuses(refusing):
    assert "group_by: 'bogus_field' is not a field of incident" in _refusal(
        lambda: count(group_by="bogus_field"))


def test_unknown_aggregate_field_refuses(refusing):
    assert "sum_fields: 'bogus' is not a field" in _refusal(lambda: count(sum_fields="bogus"))


def test_aggregate_with_valid_fields_counts(counting):
    out = count("active=true", group_by="priority", avg_fields="reassignment_count")
    assert "1 - Critical" in out


# ── Tables, and what cannot be checked ──────────────────────────────────────

def test_unknown_table_refuses_by_name(refusing):
    assert "'incidents' is not a table" in _refusal(lambda: count(table="incidents"))


@pytest.mark.parametrize("fake", [
    FakeInstance(allowed=False, meta_status=403),
    FakeInstance(allowed=False, meta_error=TimeoutError("read timed out")),
])
def test_unreadable_columns_refuse_rather_than_run_unchecked(monkeypatch, fake):
    monkeypatch.setattr(verbs, "get_client", lambda: fake)
    with pytest.raises(FieldsUnreadable) as caught:
        count("state=1")
    assert "could not read the fields of 'incident'" in str(caught.value)
    assert "nothing was run" in str(caught.value)


def test_unreadable_columns_over_http_name_the_status_and_the_api(monkeypatch):
    fake = FakeInstance(allowed=False, meta_status=403)
    monkeypatch.setattr(verbs, "get_client", lambda: fake)
    with pytest.raises(FieldsUnreadable, match="HTTP 403.*UI Meta API"):
        count("state=1")


def test_a_hibernating_instance_is_named_not_a_json_error(monkeypatch):
    fake = FakeInstance(allowed=False)
    fake.get = lambda url, **kw: Resp(200, None, content_type="text/html")
    monkeypatch.setattr(verbs, "get_client", lambda: fake)
    with pytest.raises(FieldsUnreadable, match="not JSON.*hibernating"):
        count("state=1")


# ── The column cache ────────────────────────────────────────────────────────

def test_columns_are_read_once_per_table(counting):
    count("state=1")
    count("active=true")
    assert counting.meta_reads == ["incident"]


def test_a_field_added_since_the_cache_was_filled_is_found_by_one_re_read(counting):
    count("state=1")
    counting.columns["incident"]["u_new_field"] = {"type": "string"}
    count("u_new_field=x")
    assert counting.meta_reads == ["incident", "incident"]  # filled once, re-read once
    assert counting.stats_params[-1]["sysparm_query"] == "u_new_field=x"


def test_cache_drop_survives_a_concurrent_call_dropping_the_same_key(counting, monkeypatch):
    class RacyDict(dict):
        """Iterating hands out the keys, then another call drops them all."""
        def __iter__(self):
            keys = list(super().__iter__())
            for k in keys:
                super().pop(k, None)
            return iter(keys)

    monkeypatch.setattr(query_check, "_COLUMNS", RacyDict())
    count("state=1")
    counting.columns["incident"]["u_new_field"] = {"type": "string"}
    count("u_new_field=x")
    assert counting.stats_params[-1]["sysparm_query"] == "u_new_field=x"


# ── Review round 1 (whitcortex PR #2626): each measured first ──────────────

@pytest.mark.parametrize("query", [
    "short_descriptionLIKEa^^b",   # '^^' is a literal caret
    "category=Hardware",           # string choices match case-insensitively
    "state=1 ",                    # the value is trimmed
    "state=",                      # "is empty", applied
    "state>=6",
    "state>5",                     # a number between stored values is a real comparison
    "categoryLIKEhard",            # LIKE works on a string choice
])
def test_review_shapes_that_servicenow_applies_are_counted(counting, query):
    count(query)
    assert counting.stats_params[-1]["sysparm_query"] == query


def test_a_label_in_a_comparison_refuses_with_its_value(refusing):
    assert "use state>=1" in _refusal(lambda: count("state>=new"))


@pytest.mark.parametrize("query", ["stateLIKEnew", "stateSTARTSWITHN", "stateLIKE1"])
def test_text_operators_on_a_number_stored_choice_refuse(refusing, query):
    assert "state stores numbers" in _refusal(lambda: count(query))


def test_related_list_conditions_refuse_as_unchecked(refusing):
    message = _refusal(lambda: count("active=true^RLQUERYincident_task.incident,>=1^ENDRLQUERY"))
    assert "related-list conditions" in message
    assert "cannot read a field name" not in message


# ── Review round 2 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "active=true^state~1",
    "active=true^stateEQUALS1",
    "active=true^active",         # a bare field
    "short_description~x",
])
def test_a_term_with_no_operator_servicenow_applies_refuses(refusing, query):
    assert "no known operator" in _refusal(lambda: count(query))


@pytest.mark.parametrize("query", [
    "state=01", "state=1.0",
    "categoryINHardware", "category!=HARDWARE",
    "stateNOT IN1,2",
    "assigned_toISEMPTY", "assigned_toISNOTEMPTY",
    "stateBETWEEN1@2",
    "opened_atRELATIVEGT@hour@ago@3",
    "opened_atONToday@javascript:gs.beginningOfToday()@javascript:gs.endOfToday()",
    "caller_idSAMEASopened_by",
    "sys_class_nameINSTANCEOFincident",
    "sys_class_nameNOT INSTANCEOFincident",       # must not be read as NOT IN
    "short_descriptionLIKERLQUERY",                # the word in a value is not a related-list query
])
def test_operators_servicenow_applies_are_counted(counting, query):
    count(query)
    assert counting.stats_params[-1]["sysparm_query"] == query


def test_not_in_is_value_checked(refusing):
    assert "use stateNOT IN1,2" in _refusal(lambda: count("stateNOT INnew,2"))


def test_a_quoted_stored_value_names_the_quotes(refusing):
    assert "quotes are part of the value" in _refusal(lambda: count("state='1'"))


# ── Review round 3 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("query", ["state=+1", "state=1e0"])
def test_number_forms_servicenow_matches_are_counted(counting, query):
    count(query)
    assert counting.stats_params[-1]["sysparm_query"] == query


@pytest.mark.parametrize("query, why", [
    ("state=1_0", "not a stored value"),
    ("state>inf", "not a stored value"),
    ("active=true^stateONE", "not a valid ON condition"),
    ("active=true^stateNOT BETWEEN1@2", "no known operator"),
    ("active=true^short_descriptionNOTEMPTYSTRING", "no known operator"),
    ("active=true^stateVALCHANGES", "no known operator"),
    ("active=true^stateCHANGESFROM1", "no known operator"),
    ("active=true^active==true", "repeats its operator"),
])
def test_round_three_shapes_refuse(refusing, query, why):
    assert why in _refusal(lambda: count(query))


# ── Review round 4 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "short_descriptionLIKE<script",
    "short_descriptionSTARTSWITH=",
    "short_description=!important",
])
def test_a_text_value_may_start_with_an_operator_character(counting, query):
    count(query)
    assert counting.stats_params[-1]["sysparm_query"] == query


@pytest.mark.parametrize("query", ["priority==1", "active==true"])
def test_a_repeated_operator_on_a_non_text_field_refuses(refusing, query):
    assert "repeats its operator" in _refusal(lambda: count(query))
