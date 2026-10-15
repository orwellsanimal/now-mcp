"""The snapshot exporter (now-mcp#14) against a fake paging Table API, and the redaction port."""

import json

import pytest

from now_data import export
from now_data.redact import REDACTED, redact_secret_value


def cell(value, display=None):
    return {"value": value, "display_value": value if display is None else display}


def row(**fields):
    return {k: v if isinstance(v, dict) else cell(v) for k, v in fields.items()}


TABLES = {
    "sys_db_object": [
        row(sys_id="t1", name="task", label="Task", super_class="", sys_scope="global", is_extendable="true",
            number_ref="", access="public"),
        row(sys_id="t2", name="incident", label="Incident", super_class="t1", sys_scope="global",
            is_extendable="true", number_ref="INC", access="public"),
    ],
    "sys_dictionary": [
        row(sys_id="d1", name="task", element="state", column_label="State", internal_type="integer", max_length="40",
            mandatory="false", read_only="false", active="true", default_value="1", reference="", sys_scope="global"),
        row(sys_id="d2", name="task", element="assigned_to", column_label="Assigned to", internal_type="reference",
            max_length="32", mandatory="false", read_only="false", active="true", default_value="",
            reference="sys_user", sys_scope="global"),
        row(sys_id="d3", name="incident", element="caller_id", column_label="Caller", internal_type="reference",
            max_length="32", mandatory="true", read_only="false", active="true", default_value="",
            reference="sys_user", sys_scope="global"),
    ],
    "sys_choice": [
        row(sys_id="h1", name="incident", element="state", value="1", label="New", sequence="1", inactive="false"),
        row(sys_id="h2", name="incident", element="state", value="7", label="Closed", sequence="7", inactive="false"),
    ],
    "sys_properties": [
        row(sys_id="p1", name="glide.ui.title", value="Corp", description="Title", type=cell("string", "string"),
            sys_scope=cell("global", "Global")),
        row(sys_id="p2", name="glide.user.default_password", value="{{gpaes}}abc==", description="",
            type=cell("password2", "password2"), sys_scope=cell("global", "Global")),
        row(sys_id="p3", name="x_app.api_key", value="aB3dEfGhIjKlMnOpQrSt1234567890", description="",
            type=cell("string", "string"), sys_scope=cell("x_app", "My App")),
    ],
    "sys_scope": [
        row(sys_id="s1", name="Global", scope="global", version="", vendor="", active="true"),
        row(sys_id="s2", name="My App", scope="x_app", version="1.0.0", vendor="Corp", active="true"),
    ],
    "sys_user_role": [
        row(sys_id="r1", name="itil", description="ITIL", elevated_privilege="false",
            sys_scope=cell("global", "Global")),
    ],
    "sc_cat_item": [
        row(sys_id="c1", name="Laptop", short_description="A laptop", active="true", category="hw",
            price="1000", recurring_price="0", flow_designer_flow="f1"),
    ],
}


class Resp:
    def __init__(self, status, body=None, headers=None, reason="", text=None):
        self.status_code, self.reason = status, reason
        self._body = body
        self.headers = {"Content-Type": "application/json;charset=UTF-8", **(headers or {})}
        if text is not None:
            self.headers["Content-Type"] = "text/html"

    def json(self):
        return self._body


def sid(r):
    c = r["sys_id"]
    return c["value"] if isinstance(c, dict) else c


# As a Brazil PDI has them (glide.buildtag, which export-instance.js reads, doesn't exist there).
BUILD = [row(sys_id="b1", name="glide.buildtag.last", value="glide-brazil-08-25-2026__patch0-08-26-2026"),
         row(sys_id="b2", name="glide.builddate.last", value="09-22-2026_1636"),
         row(sys_id="b3", name="glide.war", value="glide-brazil-08-25-2026__patch0-08-26-2026_09-22-2026_1636.zip")]


class Instance:
    """A Table API that pages by `sys_id>X^ORDERBYsys_id` with an offset, like the real one.

    `hidden` maps table -> sys_ids that read ACLs remove. As on a real instance they are
    removed from a page after the database picked it, so they still count in
    X-Total-Count and a page can come back short, or empty while rows remain.
    `fail` maps table -> (request number, Resp) to answer instead. Rows come back raw
    unless display_value=all is asked for.
    """

    def __init__(self, tables=None, hidden=None, fail=None, build=None):
        self.tables = TABLES if tables is None else tables
        self.build = BUILD if build is None else build
        self.hidden = hidden or {}
        self.fail = fail or {}
        self.gets = []

    def get(self, url, params=None, headers=None, timeout=None):
        table = url.rsplit("/", 1)[1]
        terms = params["sysparm_query"].split("^")
        assert terms[-1] == "ORDERBYsys_id"
        after = next((t[len("sys_id>"):] for t in terms if t.startswith("sys_id>")), "")
        n = sum(1 for t, *_ in self.gets if t == table)
        self.gets.append((table, after, params))
        if table in self.fail and self.fail[table][0] == n:
            return self.fail[table][1]
        build_lookup = "nameINglide.buildtag.last,glide.builddate.last,glide.war" in terms
        source = self.build if build_lookup else self.tables.get(table, [])
        rows = sorted((r for r in source if sid(r) > after), key=sid)
        offset, limit = int(params["sysparm_offset"]), int(params["sysparm_limit"])
        page = [r for r in rows[offset:offset + limit] if sid(r) not in self.hidden.get(table, ())]
        if params["sysparm_display_value"] == "false":
            page = [{k: v["value"] for k, v in r.items()} for r in page]
        return Resp(200, {"result": page}, {"X-Total-Count": str(len(rows))})


class Client:
    instance = "https://corp.service-now.com/"

    def __init__(self, session):
        self.session = session


def run(tmp_path, session=None, **kw):
    session = session or Instance()
    return export.export_snapshot(tmp_path, client=Client(session), **kw), session


def load(tmp_path, rel):
    return json.loads((tmp_path / rel).read_text(encoding="utf-8"))


# --- the round trip: now-config reads what the exporter wrote --------------------------

def test_now_config_answers_from_the_exported_snapshot(tmp_path):
    report, _ = run(tmp_path)
    assert report["skipped"] == {} and report["warnings"] == []
    assert report["written"] == {
        "schema/tables.json": 2, "schema/columns.json": 2, "schema/choices.json": 1,
        "platform/properties.json": 3, "platform/scopes.json": 2, "security/roles.json": 1,
        "catalog/items.json": 1,
    }

    cfg = pytest.importorskip("now_config.verbs")
    cfg.use_snapshot(root=tmp_path)
    try:
        inherited = cfg.now_field_exists(table="incident", field="state")
        assert inherited["exists"] is True and inherited["inherited_from"] == "task"
        assert cfg.now_field_exists(table="incident", field="nope")["exists"] is False
        assert [c["value"] for c in cfg.now_choice_values(table="incident", field="state")] == ["1", "7"]
        assert cfg.now_role_exists(role="itil")["exists"] is True
        assert cfg.now_scope_info(scope="x_app")["vendor"] == "Corp"
        assert cfg.now_catalog_item(sys_id_or_name="Laptop")["sys_id"] == "c1"
        assert cfg.now_property(name="glide.ui.title")["value"] == "Corp"
    finally:
        cfg.use_snapshot(root=None)


def test_shapes_match_the_node_exporter(tmp_path):
    run(tmp_path)
    assert load(tmp_path, "schema/tables.json")["tables"]["incident"] == {
        "sys_id": "t2", "label": "Incident", "super_class": "t1", "scope": "global", "is_extendable": True,
        "number_prefix": "INC", "access": "public"}
    assert load(tmp_path, "schema/columns.json")["columns"]["task"][1] == {
        "element": "state", "column_label": "State", "internal_type": "integer", "max_length": 40,
        "mandatory": False, "read_only": False, "active": True, "default_value": "1", "reference": "",
        "scope": "global"}
    # Where the Node exporter kept display values, so does this: scope names, type labels.
    prop = load(tmp_path, "platform/properties.json")["properties"]["glide.ui.title"]
    assert prop["scope"] == "Global" and prop["type"] == "string"
    assert load(tmp_path, "security/roles.json")["roles"]["itil"]["scope"] == "Global"
    assert load(tmp_path, "instance.json")["instance_url"] == "https://corp.service-now.com"


def test_property_secrets_are_redacted_by_raw_type_name_or_shape(tmp_path):
    run(tmp_path)
    props = load(tmp_path, "platform/properties.json")["properties"]
    assert props["glide.user.default_password"]["value"] == REDACTED
    assert props["x_app.api_key"]["value"] == REDACTED
    assert props["glide.ui.title"]["value"] == "Corp"
    assert "{{gpaes}}" not in (tmp_path / "platform/properties.json").read_text(encoding="utf-8")


# --- paging: never a short file without a word -----------------------------------------

def test_pages_by_keyset_until_an_empty_page(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "PAGE", 2)
    big = [row(sys_id=f"r{i}", name=f"role{i}", description="", elevated_privilege="false",
               sys_scope=cell("global", "Global")) for i in range(5)]
    report, s = run(tmp_path, Instance({**TABLES, "sys_user_role": big}), only=["security"])
    assert report["written"] == {"security/roles.json": 5}
    assert [p["sysparm_query"] for t, _, p in s.gets if t == "sys_user_role"] == [
        "ORDERBYsys_id", "sys_id>r1^ORDERBYsys_id", "sys_id>r3^ORDERBYsys_id", "sys_id>r4^ORDERBYsys_id"]


def test_the_section_query_is_kept_and_display_values_asked_only_where_kept(tmp_path):
    _, s = run(tmp_path)
    by_table = {}
    for t, _, p in s.gets:
        by_table.setdefault(t, p)  # each table's first request (instance.json's build lookup comes later)
    assert by_table["sys_dictionary"]["sysparm_query"] == "internal_type!=collection^elementISNOTEMPTY^ORDERBYsys_id"
    assert by_table["sys_dictionary"]["sysparm_fields"].startswith("sys_id,name,element")
    assert {t for t, p in by_table.items() if p["sysparm_display_value"] == "all"} == {"sys_properties", "sys_user_role"}


def test_a_page_that_does_not_advance_stops_the_export(tmp_path):
    stuck = Resp(200, {"result": [{"sys_id": ""}]}, {"X-Total-Count": "9"})
    with pytest.raises(export.ExportError, match="sys_db_object: paging stalled"):
        run(tmp_path, Instance(fail={"sys_db_object": (0, stuck)}))


ROLES = [row(sys_id=f"r{i}", name=f"role{i}", description="", elevated_privilege="false",
             sys_scope=cell("global", "Global")) for i in range(7)]


@pytest.mark.parametrize("hidden,read", [
    ({"r0"}, 6),                     # a short page is not the last page
    ({"r2", "r3"}, 5),               # a whole page hidden mid-table: step over it, don't stop
    ({"r2", "r3", "r4", "r5"}, 3),   # two hidden pages in a row
    ({"r5", "r6"}, 5),               # a hidden tail: the end, not a stall
    ({"r6"}, 6),
])
def test_rows_hidden_by_acls_never_end_the_read_early(tmp_path, monkeypatch, hidden, read):
    monkeypatch.setattr(export, "PAGE", 2)
    report, _ = run(tmp_path, Instance({**TABLES, "sys_user_role": ROLES}, hidden={"sys_user_role": hidden}),
                    only=["security"])
    roles = load(tmp_path, "security/roles.json")["roles"]
    assert sorted(roles) == sorted(f"role{i}" for i in range(7) if f"r{i}" not in hidden)
    assert report["written"] == {"security/roles.json": read}
    assert any(f"read {read} of 7 rows" in w for w in report["warnings"])


def test_a_hidden_page_is_stepped_over_by_offset_inside_the_keyset(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "PAGE", 2)
    _, s = run(tmp_path, Instance({**TABLES, "sys_user_role": ROLES}, hidden={"sys_user_role": {"r2", "r3"}}),
               only=["security"])
    asked = [(after, p["sysparm_offset"]) for t, after, p in s.gets if t == "sys_user_role"]
    assert asked == [("", "0"), ("r1", "0"), ("r1", "2"), ("r5", "0"), ("r6", "0")]


def test_a_schema_table_with_every_row_hidden_fails(tmp_path):
    # The rows exist (X-Total-Count says 2), the user can read none of them.
    with pytest.raises(export.ExportError, match="sys_db_object: 0 of 2 rows readable"):
        run(tmp_path, Instance(hidden={"sys_db_object": {"t1", "t2"}}))
    assert not any(tmp_path.iterdir())


def test_no_total_count_header_fails_rather_than_guessing(tmp_path):
    no_header = Resp(200, {"result": []})
    with pytest.raises(export.ExportError, match="sys_db_object: no X-Total-Count"):
        run(tmp_path, Instance(fail={"sys_db_object": (0, no_header)}))


@pytest.mark.parametrize("bad", [
    Resp(403, {"error": {"message": "User Not Authorized"}}, reason="Forbidden"),
    Resp(500, {"error": {"message": "boom"}}, reason="Server Error"),
    Resp(200, None, text="<html>waking up</html>"),
])
def test_a_failed_schema_page_fails_the_export_and_writes_nothing(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(export, "PAGE", 1)
    with pytest.raises(export.ExportError, match="sys_dictionary.*after sys_id 'd1'"):
        run(tmp_path, Instance(fail={"sys_dictionary": (1, bad)}))
    assert not (tmp_path / "schema").exists() and not (tmp_path / "instance.json").exists()


def test_an_empty_schema_table_is_an_error_not_an_empty_snapshot(tmp_path):
    with pytest.raises(export.ExportError, match="sys_db_object: 0 of 0 rows"):
        run(tmp_path, Instance({**TABLES, "sys_db_object": []}))


@pytest.mark.parametrize("bad", [
    Resp(200, "not a dict"),               # JSON that isn't {result: [...]}
    Resp(200, {"result": {"oops": 1}}),
])
def test_a_malformed_page_is_an_export_error(tmp_path, bad):
    with pytest.raises(export.ExportError, match="sys_dictionary"):
        run(tmp_path, Instance(fail={"sys_dictionary": (0, bad)}))


def test_a_failed_export_leaves_the_previous_snapshot_untouched(tmp_path):
    run(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}

    class Boom(Instance):
        def get(self, url, **kw):
            if url.endswith("/sc_cat_item"):
                raise KeyboardInterrupt  # not an ExportError: aborts the run, as Ctrl-C would
            return super().get(url, **kw)

    other = Client(Boom())
    other.instance = "https://other.service-now.com"
    with pytest.raises(KeyboardInterrupt):
        export.export_snapshot(tmp_path, client=other)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.json")} == before


def test_a_write_that_dies_part_way_leaves_no_instance_claim(tmp_path, monkeypatch):
    run(tmp_path)
    real, calls = export._write_json, []

    def dies_on_second(path, doc):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        real(path, doc)

    monkeypatch.setattr(export, "_write_json", dies_on_second)
    other = Client(Instance())
    other.instance = "https://other.service-now.com"
    with pytest.raises(OSError):
        export.export_snapshot(tmp_path, client=other)
    # One file is now other's and the rest corp's: no instance.json may say it's all one instance.
    assert not (tmp_path / "instance.json").exists()


def test_an_optional_section_is_skipped_and_the_rest_written(tmp_path):
    bad = Resp(403, {"error": {"message": "User Not Authorized"}}, reason="Forbidden")
    report, _ = run(tmp_path, Instance(fail={"sc_cat_item": (0, bad)}))
    assert "catalog/items.json" not in report["written"]
    assert "HTTP 403" in report["skipped"]["catalog"] and "User Not Authorized" in report["skipped"]["catalog"]
    assert (tmp_path / "schema/tables.json").exists()


def test_family_falls_back_to_glide_war(tmp_path):
    run(tmp_path, Instance(build=BUILD[2:]))
    info = load(tmp_path, "instance.json")
    assert info["family"] == "brazil" and info["build"] == ""


def test_a_skipped_sections_file_from_another_instance_is_removed(tmp_path):
    run(tmp_path)  # a full export of corp
    (tmp_path / "instance.json").write_text(json.dumps({"instance_url": "https://lab.service-now.com"}))
    bad = Resp(403, {"error": {"message": "no"}})
    report, _ = run(tmp_path, Instance(fail={"sc_cat_item": (0, bad)}))
    assert not (tmp_path / "catalog/items.json").exists()
    assert report["removed"] == ["catalog/items.json"]


def test_only_some_sections_from_another_instance_removes_the_rest(tmp_path):
    # --only schema into another instance's snapshot must not leave its roles and properties behind.
    run(tmp_path)
    (tmp_path / "instance.json").write_text(json.dumps({"instance_url": "https://lab.service-now.com"}))
    (tmp_path / "schema/relationships.json").write_text("{}")  # a Node-only file
    report, _ = run(tmp_path, only=["schema"])
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.json")) == [
        "instance.json", "schema/choices.json", "schema/columns.json", "schema/relationships.json",
        "schema/tables.json"]
    assert any("schema/relationships.json is from https://lab" in w for w in report["warnings"])
    assert load(tmp_path, "instance.json")["instance_url"] == "https://corp.service-now.com"


def test_a_skipped_sections_file_from_the_same_instance_is_kept(tmp_path):
    run(tmp_path)
    bad = Resp(403, {"error": {"message": "no"}})
    report, _ = run(tmp_path, Instance(fail={"sc_cat_item": (0, bad)}))
    assert (tmp_path / "catalog/items.json").exists()
    assert report["removed"] == []


def test_instance_json_names_the_release_and_files_carry_their_schema(tmp_path):
    run(tmp_path)
    info = load(tmp_path, "instance.json")
    assert info["family"] == "brazil" and info["build"] == "glide-brazil-08-25-2026__patch0-08-26-2026"
    assert info["build_date"] == "09-22-2026_1636"
    assert info["$schema"] == "./schemas/instance.schema.json" and info["profile"] == "exported"
    assert load(tmp_path, "catalog/items.json")["$schema"] == "../schemas/catalog-items.schema.json"
    assert list(load(tmp_path, "schema/tables.json")["tables"]) == ["incident", "task"]  # sorted by name
    assert [c["element"] for c in load(tmp_path, "schema/columns.json")["columns"]["task"]] == [
        "assigned_to", "state"]


def test_unknown_section_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown section"):
        run(tmp_path, only=["schema", "acls"])


def test_cli_reports_and_exits_nonzero_on_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(export, "get_client", lambda: Client(Instance()))
    assert export.main(["--out", str(tmp_path), "--only", "schema"]) == 0
    assert "wrote schema/tables.json (2 entries)" in capsys.readouterr().out
    monkeypatch.setattr(export, "get_client", lambda: Client(Instance({**TABLES, "sys_db_object": []})))
    assert export.main(["--out", str(tmp_path)]) == 1
    assert "export failed: sys_db_object: 0 of 0 rows" in capsys.readouterr().err


# --- redaction: the cases from servicenow-coder's redact.test.js -----------------------

@pytest.mark.parametrize("name,value,type_", [
    ("glide.user.default_password", "{{gpaes}}MmrFy3gjXymplWkQyo/Qxg==", "password2"),
    ("glide.authenticate.sso.saml2.keystore.password", "saml2sp", "password"),
    ("some_integration.token_store", "96epGNp692HLpgAoctCpiIxMX3Me0FvB2UUcIkusJZeM7Y6KkLKCV183FYr93rnU", "password2"),
    ("glide.geocode.google.api_key", "**", "password2"),
    ("some_random_prop", "AIzaSyD-fakeGoogleApiKeyLookingValue123", None),
    ("x_myapp.api_key", "aB3dEfGhIjKlMnOpQrSt1234567890", None),
    ("com.glide.cs.analytics_api_key", "fakeTenantId12345678:fake_token_value_abcdefghij", "string"),
    ("x_app.client_secret", "abc!def@ghi#jkl$mno%", None),
    ("x_app.client_secret", "key{with}braces~and^more", None),
    ("x_app.client_secret", "user:pass@host;extra", None),
    ("x_app.api_key", "aB3dEfGhIjKlMnOpQrSt1234567890\n", None),
    ("x_app.api_key", "  aB3d:EfGhIjKlMnOpQrSt12345  ", None),
    ("some_random_prop", " AIzaSyD-fakeGoogleApiKeyLookingValue123", None),
    ("some_random_prop", "﻿ghp_fakeGithubToken", None),          # JS trim() strips a BOM
    ("x_app.api_key", "abcdefghijklmnopq", None),                 #  isn't JS whitespace
] + [("x_app.some_cert_blob", f"-----BEGIN {h}-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END {h}-----\n", "string")
     for h in ("PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY", "ENCRYPTED PRIVATE KEY")])
def test_redacted(name, value, type_):
    assert redact_secret_value(name, value, type_) == REDACTED


@pytest.mark.parametrize("name,value,type_", [
    ("glide.some.string_prop", "abc123XYZ456!!!!", "string"),
    ("password_reset.sms.max_per_day", "10", None),
    ("glide.some.password", "", "password2"),
    ("x_app.token_max_age", " 1234567890123456789 ", None),
    ("x_app.api_key.help_text", "Enter the API key issued by the vendor portal", None),
    ("x_app.token_ttl", "3600s", None),
    ("x_app.server_cert", "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIU\n-----END CERTIFICATE-----\n", "string"),
])
def test_kept(name, value, type_):
    assert redact_secret_value(name, value, type_) == value
