from pathlib import Path

import pytest

from now_config import verbs

FIXTURE = Path(__file__).parent / "fixtures" / "instance-config"


@pytest.fixture(autouse=True)
def snapshot():
    verbs.use_snapshot(FIXTURE)
    yield
    verbs.use_snapshot(None)


def test_field_exists_walks_super_class():
    r = verbs.now_field_exists("incident", "cmdb_ci")
    assert r["exists"] is True
    assert r["inherited_from"] == "task"
    assert r["reference"] == "cmdb_ci"


def test_field_defined_directly_has_no_inherited_from():
    r = verbs.now_field_exists("incident", "caller_id")
    assert r == {
        "exists": True,
        "internal_type": "reference",
        "reference": "sys_user",
        "mandatory": True,
        "read_only": False,
        "column_label": "Caller",
    }


def test_field_missing_vs_table_missing_are_distinguished():
    assert "not defined on 'incident'" in verbs.now_field_exists("incident", "nope")["reason"]
    assert "not in snapshot" in verbs.now_field_exists("incidnet", "state")["reason"]


def test_table_columns_raises_on_unknown_table():
    with pytest.raises(ValueError):
        verbs.now_table_columns("incidnet")


def test_table_columns_inherited_marks_source_and_skips_overridden():
    rows = verbs.now_table_columns("incident", include_inherited=True)
    by_element = {r["element"]: r for r in rows}
    assert "_inherited_from" not in by_element["state"]  # incident's own wins
    assert by_element["number"]["_inherited_from"] == "task"


def test_choice_values_inherited_and_active_only_by_default():
    rows = verbs.now_choice_values("incident", "state")
    assert rows == [{"value": "1", "label": "Open", "_inherited_from": "task"}]
    all_rows = verbs.now_choice_values("incident", "state", include_inactive=True)
    assert [r["value"] for r in all_rows] == ["1", "7"]
    assert all_rows[1]["inactive"] is True


def test_choice_values_raises_when_no_list():
    with pytest.raises(ValueError):
        verbs.now_choice_values("incident", "caller_id")


def test_role_scope_property_catalog():
    assert verbs.now_role_exists("itil")["exists"] is True
    assert verbs.now_role_exists("nope") == {"exists": False}
    assert verbs.now_scope_info("sn_example")["version"] == "1.0.0"
    assert verbs.now_property("glide.example.flag")["value"] == "true"
    item = verbs.now_catalog_item("Example Laptop")
    assert item["sys_id"] == "6" * 32
    assert verbs.now_catalog_item("6" * 32)["name"] == "Example Laptop"
    for call, arg in [
        (verbs.now_scope_info, "sn_nope"),
        (verbs.now_property, "nope"),
        (verbs.now_catalog_item, "nope"),
    ]:
        with pytest.raises(ValueError):
            call(arg)


def test_snapshot_env_var(monkeypatch, tmp_path):
    verbs.use_snapshot(None)
    monkeypatch.setenv("INSTANCE_CONFIG_ROOT", str(FIXTURE))
    monkeypatch.chdir(tmp_path)  # no ancestor snapshot to fall back on
    assert verbs.now_role_exists("itil")["exists"] is True
