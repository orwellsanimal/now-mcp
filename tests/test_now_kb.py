"""now_data.kb against fake now-data verbs (no network, servicenow-coder#282).

The fake holds the shapes measured on dev442835 on 2026-11-21:
- a category tree under parent_table/parent_id;
- article numbers shared by several rows, as versions in one KB and as demo data across KBs;
- `version` as a reference (a kb_version sys_id whose label is "1.0");
- KBs whose kb_publish_flow is "Knowledge - Instant Publish" or "Knowledge - Approval Publish";
- sys_flow_context rows by source_record and state.

Like the instance, it returns only the fields asked for, so a field dropped from a request breaks
a test (PR #26 review).
"""

import re

import pytest

from now_data import kb

KB_FS, KB_IT, KB_TWIN1, KB_TWIN2, KB_NOFLOW = (c * 32 for c in "abcde")
CAT_OS, CAT_WIN, CAT_HOWTO_WIN, CAT_APPS, CAT_HOWTO_APPS, CAT_ROUTERS = (
    f"{i:032x}" for i in range(1, 7))
CAT_IT_ONLY = f"{7:032x}"
DRAFT, PUBLISHED, IT_DRAFT, OLD_V, NEW_V, DEMO_A, DEMO_B, NOFLOW_DRAFT = (f"{i:032x}" for i in range(100, 108))
INSTANT, APPROVAL = "Knowledge - Instant Publish", "Knowledge - Approval Publish"


def _kb(sys_id, title, flow, version="3"):
    return {"sys_id": sys_id, "title": title, "kb_version": version, "active": "true",
            "kb_publish_flow": "f" * 32 if flow else "", "_labels": {"kb_publish_flow": flow or ""}}


def _cat(sys_id, label, parent, parent_table="kb_category"):
    return {"sys_id": sys_id, "label": label, "parent_id": parent, "parent_table": parent_table, "active": "true"}


def _art(sys_id, number, kb_id, state="draft", latest="true", version="0.01"):
    # version references kb_version: a sys_id value whose label is the number, as on the instance.
    return {"sys_id": sys_id, "number": number, "short_description": f"article {number}", "workflow_state": state,
            "kb_knowledge_base": kb_id, "version": "7" * 32, "latest": latest, "_labels": {"version": version}}


class Fake:
    def __init__(self):
        self.rows = {
            "kb_knowledge_base": [
                _kb(KB_FS, "Field Service Knowledge", INSTANT), _kb(KB_IT, "IT", APPROVAL),
                _kb(KB_TWIN1, "Twin", INSTANT), _kb(KB_TWIN2, "Twin", INSTANT),
                _kb(KB_NOFLOW, "No Flow", None),
            ],
            "kb_category": [
                _cat(CAT_OS, "Operating Systems", KB_FS, "kb_knowledge_base"),
                _cat(CAT_WIN, "Windows", CAT_OS),
                _cat(CAT_HOWTO_WIN, "How To", CAT_WIN),
                _cat(CAT_APPS, "Applications", KB_FS, "kb_knowledge_base"),
                _cat(CAT_HOWTO_APPS, "How To", CAT_APPS),
                _cat(CAT_ROUTERS, "Routers", KB_FS, "kb_knowledge_base"),
                _cat(CAT_IT_ONLY, "Policies", KB_IT, "kb_knowledge_base"),
            ],
            "kb_knowledge": [
                _art(DRAFT, "KB0010100", KB_FS),
                _art(PUBLISHED, "KB0010101", KB_FS, state="published", version="1.0"),
                _art(IT_DRAFT, "KB0010102", KB_IT),
                _art(OLD_V, "KB0010103", KB_FS, state="outdated", latest="false", version="1.0"),
                _art(NEW_V, "KB0010103", KB_FS, version="1.01"),
                _art(DEMO_A, "KB0010104", KB_FS, latest="true"),
                _art(DEMO_B, "KB0010104", KB_IT, latest="true"),
                _art(NOFLOW_DRAFT, "KB0010105", KB_NOFLOW),
            ],
            "sys_flow_context": [],
        }
        self.queries, self.inserts, self.scripts, self.state_reads = [], [], [], 0
        self.endpoint = "https://dev.example/api/global/now_data/run_script"
        # What the publish does: the state it leaves the article in, after how many state reads.
        self.publish_to, self.publish_after_reads = "published", 0
        self.script_answer = None  # overrides the script's answer (and publishes nothing)
        self.get_raises = None
        self.flow_query_raises = None
        self.published_id = None

    # -- now-data verbs ---------------------------------------------------------------------------
    def _match(self, row, term):
        if term.startswith("ORDERBY"):
            return True
        if "IN" in term and "=" not in term:
            f, v = term.split("IN", 1)
            return str(row.get(f)) in v.split(",")
        f, v = term.split("=", 1)
        return str(row.get(f, "")) == v

    def _shape(self, table, row, fields, display_value):
        def label(f):
            if f in row.get("_labels", {}):
                return row["_labels"][f]
            if table == "kb_knowledge" and f == "kb_knowledge_base":
                return next(k["title"] for k in self.rows["kb_knowledge_base"] if k["sys_id"] == row[f])
            return row[f]
        wanted = [f.strip() for f in fields.split(",") if f.strip()] if fields else None
        out = {}
        for f in (k for k in row if not k.startswith("_")):
            if wanted is not None and f not in wanted:
                continue  # as the instance does: only the fields asked for
            if display_value == "all":
                out[f] = {"value": row[f], "display_value": label(f)}
            elif display_value is True:
                out[f] = label(f)
            else:
                out[f] = row[f]
        return out

    def now_query(self, table, query="", fields="", limit=50, display_value=False):
        self.queries.append((table, query))
        if table == "sys_flow_context" and self.flow_query_raises:
            raise self.flow_query_raises
        terms = [t for t in query.split("^") if t]
        rows = [r for r in self.rows.get(table, []) if all(self._match(r, t) for t in terms)]
        out = [self._shape(table, r, fields, display_value) for r in rows[:limit]]
        return {"count": len(out), "truncated": len(rows) > limit, "records": out}

    def now_get(self, table, sys_id, fields="", display_value=False):
        if self.get_raises:
            raise self.get_raises
        row = next((r for r in self.rows[table] if r["sys_id"] == sys_id), None)
        if row is None:
            raise ValueError(f"No {table} record with sys_id {sys_id!r}.")
        if table == "kb_knowledge" and sys_id == self.published_id and fields == "workflow_state":
            self.state_reads += 1
            if self.state_reads > self.publish_after_reads:
                row["workflow_state"] = self.publish_to
        return self._shape(table, row, fields, display_value)

    def now_insert(self, table, values, confirmed=False):
        unknown = sorted(f for f in values if f in ("bogus",))
        if not confirmed:
            return {"confirmed": False, "action": "preview", "table": table, "values": values,
                    "unknown_fields": unknown, "note": "Dry run — nothing inserted."}
        self.inserts.append((table, values))
        new = _art("9" * 32, "KB0010999", values["kb_knowledge_base"])
        self.rows[table].append(new)
        return {"confirmed": True, "action": "inserted", "sys_id": new["sys_id"], "table": table,
                "differences": []}

    def now_run_script(self, script, confirmed=False):
        """The publish script, as the instance would run it."""
        assert confirmed is True
        self.scripts.append(script)
        sys_id = re.search(r"gr\.get\('([0-9a-f]{32})'\)", script).group(1)
        if self.script_answer is not None:
            return self.script_answer
        # The script stops on a running flow only if it says so before it publishes.
        states = re.search(r"addQuery\('state', 'IN', '([A-Z_,]+)'\)", script).group(1).split(",")
        stop = script.find("if (flows.next()) return {found: true, before: before, in_flight:")
        guarded = 0 <= stop < script.find("KnowledgeUIAction().publish(gr)")
        running = [c for c in self.rows["sys_flow_context"]
                   if c["source_record"] == sys_id and c["state"] in states]
        if running and guarded:
            return {"action": "executed", "result": {"found": True, "before": "draft",
                                                     "in_flight": running[0]["name"]}}
        self.published_id = sys_id
        return {"confirmed": True, "action": "executed", "success": True,
                "result": {"found": True, "before": "draft", "can_publish": True, "publish_returned": True}}

    def resolve_url(self):
        return self.endpoint


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    for name in ("now_query", "now_get", "now_insert", "now_run_script"):
        monkeypatch.setattr(kb.verbs, name, getattr(f, name))
    monkeypatch.setattr(kb.verbs, "_resolve_run_script_url", f.resolve_url)
    # A fake clock: each poll's sleep moves it on, so a wait is a count of reads, not real time.
    now = [0.0]
    monkeypatch.setattr(kb, "_clock", lambda: now[0])
    monkeypatch.setattr(kb, "_sleep", lambda s: now.__setitem__(0, now[0] + s))
    return f


ALL_READS = 1 + int(kb.SETTLE_SECONDS // kb._POLL_SECONDS)  # one read, then one per poll


# ── create: lookups ─────────────────────────────────────────────────────────────────────────────

def test_create_dry_run_resolves_kb_and_category_and_inserts_nothing(fake):
    r = kb.now_kb_create_article("Field Service Knowledge", "Reset a router", "<p>Hold reset.</p>",
                                 category="Routers")
    assert (r["confirmed"], r["action"], fake.inserts) == (False, "preview", [])
    assert r["values"] == {"kb_knowledge_base": KB_FS, "short_description": "Reset a router",
                           "text": "<p>Hold reset.</p>", "kb_category": CAT_ROUTERS}
    assert r["knowledge_base"] == {"sys_id": KB_FS, "title": "Field Service Knowledge"}
    assert r["category"] == {"sys_id": CAT_ROUTERS, "path": "Routers"}
    assert r["publish_flow"] == INSTANT


def test_kb_title_matches_without_case_when_no_exact_match(fake):
    r = kb.now_kb_create_article("field service knowledge", "s", "t")
    assert r["knowledge_base"]["sys_id"] == KB_FS


def test_kb_by_sys_id(fake):
    assert kb.now_kb_create_article(KB_IT, "s", "t")["knowledge_base"]["title"] == "IT"


def test_an_unknown_kb_lists_the_active_ones(fake):
    with pytest.raises(ValueError, match=r"No active knowledge base titled 'Nope'.*Field Service Knowledge; IT"):
        kb.now_kb_create_article("Nope", "s", "t")


def test_an_ambiguous_kb_title_is_refused_with_both_sys_ids(fake):
    with pytest.raises(ValueError, match=rf"2 active knowledge bases are titled 'Twin': {KB_TWIN1}, {KB_TWIN2}"):
        kb.now_kb_create_article("Twin", "s", "t")


def test_a_kb_title_is_not_trusted_unique_when_the_kb_list_was_truncated(fake, monkeypatch):
    monkeypatch.setattr(kb.verbs, "MAX_LIMIT", 2)
    with pytest.raises(ValueError, match="more than 1000 active knowledge bases.*give its sys_id"):
        kb.now_kb_create_article("Field Service Knowledge", "s", "t")


def test_a_kb_title_never_reaches_an_encoded_query(fake):
    # A title can hold ^, which would extend the query: titles are compared in Python.
    with pytest.raises(ValueError):
        kb.now_kb_create_article("IT^ORsys_id!=x", "s", "t")
    assert all("IT^OR" not in q for _, q in fake.queries)


def test_category_by_full_path(fake):
    r = kb.now_kb_create_article("Field Service Knowledge", "s", "t", category="Operating Systems / Windows / How To")
    assert r["category"] == {"sys_id": CAT_HOWTO_WIN, "path": "Operating Systems / Windows / How To"}


def test_category_path_ignores_case_and_spacing_around_slashes(fake):
    r = kb.now_kb_create_article("Field Service Knowledge", "s", "t", category="operating systems/windows")
    assert r["category"]["sys_id"] == CAT_WIN


def test_a_unique_label_names_a_nested_category(fake):
    assert kb.now_kb_create_article("Field Service Knowledge", "s", "t", category="Windows")["category"]["sys_id"] == CAT_WIN


def test_an_ambiguous_category_label_lists_every_path(fake):
    with pytest.raises(ValueError, match=r"2 categories .* 'How To': Applications / How To .*; "
                                         r"Operating Systems / Windows / How To"):
        kb.now_kb_create_article("Field Service Knowledge", "s", "t", category="How To")


def test_a_path_that_is_also_another_categorys_label_is_refused(fake):
    # A top-level "Windows" (path "Windows") and "Operating Systems / Windows" (label "Windows"):
    # "Windows" names both, so neither is picked (PR #26 review).
    top = "6" * 32
    fake.rows["kb_category"].append(_cat(top, "Windows", KB_FS, "kb_knowledge_base"))
    with pytest.raises(ValueError, match="2 categories .* match 'Windows'") as e:
        kb.now_kb_create_article("Field Service Knowledge", "s", "t", category="Windows")
    assert f"Windows ({top})" in str(e.value) and f"Operating Systems / Windows ({CAT_WIN})" in str(e.value)
    r = kb.now_kb_create_article("Field Service Knowledge", "s", "t", category="Operating Systems / Windows")
    assert r["category"]["sys_id"] == CAT_WIN


def test_a_category_of_another_kb_is_not_found(fake):
    with pytest.raises(ValueError, match="has no active category 'Policies'"):
        kb.now_kb_create_article("Field Service Knowledge", "s", "t", category="Policies")
    with pytest.raises(ValueError, match="is not an active category of the knowledge base"):
        kb.now_kb_create_article("Field Service Knowledge", "s", "t", category=CAT_IT_ONLY)


def test_a_category_row_whose_parent_is_another_table_is_not_in_the_tree(fake):
    # parent_id is a document_id: the KB's sys_id under parent_table=kb_category isn't the KB.
    fake.rows["kb_category"].append(_cat("8" * 32, "Impostor", KB_FS, "kb_category"))
    with pytest.raises(ValueError, match="has no active category 'Impostor'"):
        kb.now_kb_create_article("Field Service Knowledge", "s", "t", category="Impostor")


def _chain(fake, depth):
    parent, table = KB_IT, "kb_knowledge_base"
    for i in range(depth):
        sid = f"c{i:031x}"
        fake.rows["kb_category"].append(_cat(sid, f"L{i + 1}", parent, table))
        parent, table = sid, "kb_category"
    return parent


def test_a_tree_exactly_max_depth_deep_resolves_and_one_deeper_is_refused(fake):
    leaf = _chain(fake, kb._MAX_DEPTH)
    r = kb.now_kb_create_article("IT", "s", "t", category=f"L{kb._MAX_DEPTH}")
    assert r["category"]["sys_id"] == leaf
    fake.rows["kb_category"].append(_cat("d" * 32, "Too deep", leaf))
    with pytest.raises(ValueError, match="nested more than 20 deep"):
        kb.now_kb_create_article("IT", "s", "t", category="Too deep")


def test_a_category_sys_id_from_the_instance_that_isnt_one_is_dropped(fake):
    # Instance values go into the next level's parent_idIN query, so only real sys_ids may.
    fake.rows["kb_category"].append(_cat("x,y'^ORactive=false", "Odd", KB_FS, "kb_knowledge_base"))
    with pytest.raises(ValueError, match="has no active category 'Odd'"):
        kb.now_kb_create_article("Field Service Knowledge", "s", "t", category="Odd")
    assert all("ORactive=false" not in q for _, q in fake.queries)


# ── create: fields and writes ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("field, why", [
    ("workflow_state", "the instance sets it"), ("version", "the instance sets it"),
    ("number", "the instance sets it"),
    ("kb_category", "category argument"), ("text", "text argument"), ("sys_created_by", "a system field"),
])
def test_fields_the_verb_or_the_instance_owns_are_refused_before_anything(fake, field, why):
    with pytest.raises(ValueError, match=why):
        kb.now_kb_create_article("IT", "s", "t", fields={field: "x"})
    assert fake.queries == [] and fake.inserts == []


@pytest.mark.parametrize("short, text", [("", "t"), ("  ", "t"), ("s", ""), ("s", " \n")])
def test_title_and_body_are_required(fake, short, text):
    with pytest.raises(ValueError, match="required"):
        kb.now_kb_create_article("IT", short, text)


def test_extra_fields_pass_through_and_unknown_ones_are_reported(fake):
    r = kb.now_kb_create_article("IT", "s", "t", fields={"meta": "wan", "bogus": 1})
    assert r["values"]["meta"] == "wan" and r["unknown_fields"] == ["bogus"]


def test_confirmed_create_inserts_once_and_reads_back_the_number(fake):
    r = kb.now_kb_create_article("IT", "s", "t", confirmed=True)
    assert len(fake.inserts) == 1
    assert (r["action"], r["number"], r["workflow_state"], r["version"]) == ("inserted", "KB0010999", "draft", "0.01")


def test_confirmed_create_names_the_row_when_its_number_cannot_be_read(fake):
    fake.get_raises = RuntimeError("boom")
    r = kb.now_kb_create_article("IT", "s", "t", confirmed=True)
    assert r["number"] is None and "9" * 32 in r["note"] and "Don't create it again" in r["note"]


def test_confirmed_create_flags_an_article_that_isnt_a_draft(fake, monkeypatch):
    orig = fake.now_insert

    def insert(table, values, confirmed=False):
        out = orig(table, values, confirmed)
        fake.rows["kb_knowledge"][-1]["workflow_state"] = "published"
        return out

    monkeypatch.setattr(kb.verbs, "now_insert", insert)
    r = kb.now_kb_create_article("IT", "s", "t", confirmed=True)
    assert "not a draft" in r["note"]


# ── publish: lookups and dry run ───────────────────────────────────────────────────────────────

def test_publish_dry_run_runs_nothing_and_says_what_the_flow_will_do(fake):
    r = kb.now_kb_publish_article("KB0010100")
    assert (r["action"], r["publishable"], fake.scripts) == ("preview", True, [])
    assert r["publish_flow"] == INSTANT and r["expected"] == "publish it at once"
    assert "Publishing would publish it at once." in r["note"]
    assert r["article"]["sys_id"] == DRAFT


def test_publish_dry_run_on_an_approval_kb_expects_review(fake):
    r = kb.now_kb_publish_article(IT_DRAFT)
    assert r["publish_flow"] == APPROVAL and "Review" in r["expected"]


def test_the_article_shows_its_version_number_not_the_kb_version_sys_id(fake):
    assert kb.now_kb_publish_article("KB0010101")["article"]["version"] == "1.0"


def test_publish_takes_a_lowercase_number(fake):
    assert kb.now_kb_publish_article("kb0010100")["article"]["sys_id"] == DRAFT


def test_a_shared_number_in_one_kb_resolves_to_its_latest_version(fake):
    assert kb.now_kb_publish_article("KB0010103")["article"]["sys_id"] == NEW_V


def test_a_number_two_current_articles_share_is_refused_listing_both(fake):
    with pytest.raises(ValueError, match=rf"KB0010104 is the number of 2 articles: {DEMO_A} .*; {DEMO_B} "):
        kb.now_kb_publish_article("KB0010104")


def test_a_number_shared_across_kbs_is_refused_even_with_one_latest(fake):
    # The dev442835 KB0010001 shape: a demo row (latest=false) in one KB, an unrelated article
    # (latest=true) in another. Latest says nothing about which was meant (PR #26 review).
    fake.rows["kb_knowledge"][6]["latest"] = "false"  # DEMO_B
    with pytest.raises(ValueError, match="KB0010104 is the number of 2 articles"):
        kb.now_kb_publish_article("KB0010104")


def test_a_number_with_more_rows_than_were_read_is_refused(fake):
    for i in range(kb._NUMBER_ROWS):
        fake.rows["kb_knowledge"].append(_art(f"e{i:031x}", "KB0010103", KB_FS, latest="false"))
    with pytest.raises(ValueError, match="more than 20 articles: give the sys_id"):
        kb.now_kb_publish_article("KB0010103")


@pytest.mark.parametrize("bad", ["", "KB-1", "x' ); gs.info('x", "a" * 31])
def test_anything_but_a_number_or_sys_id_is_refused_before_a_query(fake, bad):
    with pytest.raises(ValueError):
        kb.now_kb_publish_article(bad)
    assert fake.queries == []


def test_a_sys_id_from_the_instance_that_isnt_one_never_reaches_the_script(fake):
    # The article's sys_id is formatted into the publish script: one read from the instance is
    # checked like one given by the caller (PR #26 review).
    fake.rows["kb_knowledge"].append(_art("x'); gs.print('pwned'); //", "KB0010200", KB_FS))
    with pytest.raises(ValueError, match="is not a sys_id"):
        kb.now_kb_publish_article("KB0010200", confirmed=True)
    assert fake.scripts == []


def test_publish_dry_run_of_a_published_article_says_why_not(fake):
    r = kb.now_kb_publish_article("KB0010101")
    assert (r["action"], r["publishable"], r["expected"]) == ("preview", False, None)
    assert "only a draft can be published" in r["note"]


def test_publish_dry_run_without_the_endpoint_is_not_publishable(fake):
    fake.endpoint = None
    r = kb.now_kb_publish_article("KB0010100")
    assert r["publishable"] is False and "run_script endpoint" in r["note"]


def test_a_version_3_kb_with_no_publish_flow_is_not_publishable(fake):
    r = kb.now_kb_publish_article("KB0010105")
    assert r["publishable"] is False and "no publish flow" in r["note"]
    r = kb.now_kb_publish_article("KB0010105", confirmed=True)
    assert r["action"] == "refused" and fake.scripts == []


def test_an_article_whose_kb_cannot_be_read_is_refused(fake):
    fake.rows["kb_knowledge_base"] = [k for k in fake.rows["kb_knowledge_base"] if k["sys_id"] != KB_FS]
    fake.rows["kb_knowledge"][0]["_labels"]["kb_knowledge_base"] = "(unreadable)"
    r = kb.now_kb_publish_article("KB0010100", confirmed=True)
    assert r["action"] == "refused" and "can't be read" in r["reason"] and fake.scripts == []


def test_publish_dry_run_with_a_flow_running_on_the_article_is_not_publishable(fake):
    fake.rows["sys_flow_context"].append({"source_record": DRAFT, "state": "IN_PROGRESS", "name": INSTANT})
    r = kb.now_kb_publish_article("KB0010100")
    assert r["publishable"] is False and "already running" in r["note"]


def test_a_finished_flow_on_the_article_does_not_block(fake):
    fake.rows["sys_flow_context"].append({"source_record": DRAFT, "state": "COMPLETE", "name": INSTANT})
    assert kb.now_kb_publish_article("KB0010100")["publishable"] is True


def test_dry_run_when_flow_contexts_are_unreadable_still_previews(fake):
    fake.flow_query_raises = PermissionError("403")
    assert kb.now_kb_publish_article("KB0010100")["publishable"] is True


# ── publish: confirmed ─────────────────────────────────────────────────────────────────────────

def test_confirmed_publish_runs_the_platform_publish_once_and_reads_back(fake):
    r = kb.now_kb_publish_article("KB0010100", confirmed=True)
    assert (r["action"], r["workflow_state"]) == ("published", "published")
    assert r["article"]["workflow_state"] == "published"  # refreshed, not the draft it was
    assert len(fake.scripts) == 1
    script = fake.scripts[0]
    assert f"gr.get('{DRAFT}')" in script
    assert "new global.KnowledgeUIAction().publish(gr)" in script
    assert "new global.KBKnowledge().canPublish(gr)" in script


def test_the_script_refuses_while_any_unfinished_flow_state_is_on_the_article(fake):
    kb.now_kb_publish_article("KB0010100", confirmed=True)
    states = re.search(r"addQuery\('state', 'IN', '([A-Z_,]+)'\)", fake.scripts[0]).group(1).split(",")
    assert set(states) == {"QUEUED", "IN_PROGRESS", "WAITING", "CONTINUE_SYNC", "PAUSED", "PAUSED_IN_DEBUG"}


def test_publish_never_writes_workflow_state_itself(fake, monkeypatch):
    # The whole point (servicenow-coder#282): a field write would skip the KB's flow and approval.
    calls = []
    monkeypatch.setattr(kb.verbs, "now_update", lambda *a, **k: calls.append(a))
    kb.now_kb_publish_article("KB0010100", confirmed=True)
    assert calls == []
    assert "workflow_state" not in kb._PUBLISH_SCRIPT.replace("getValue('workflow_state')", "")


def test_publish_waits_for_a_flow_that_finishes_after_the_script(fake):
    fake.publish_after_reads = 3
    r = kb.now_kb_publish_article("KB0010100", confirmed=True)
    assert r["action"] == "published" and fake.state_reads == 4


@pytest.mark.parametrize("state, action", [("review", "in_review"), ("scheduled_publish", "scheduled")])
def test_flow_outcomes_other_than_published(fake, state, action):
    fake.publish_to = state
    r = kb.now_kb_publish_article("KB0010102", confirmed=True)
    assert r["action"] == action


def test_a_state_no_publish_leads_to_is_unexpected(fake):
    fake.publish_to = "retired"
    assert kb.now_kb_publish_article("KB0010100", confirmed=True)["action"] == "unexpected_state"


def test_a_draft_after_the_wait_is_unconfirmed_not_not_published(fake):
    # The flow may still be running: "not_published" would invite a second publish (PR #26 review).
    fake.publish_to = "draft"
    r = kb.now_kb_publish_article("KB0010100", confirmed=True)
    assert (r["action"], r["workflow_state"]) == ("unconfirmed", "draft")
    assert "Check it with now_get before anything else" in r["note"]
    assert fake.state_reads == ALL_READS


def test_a_retry_while_the_first_flow_runs_starts_nothing(fake):
    # The first publish's flow is still running (the instance shows its context); the retry's
    # script sees it and publishes nothing, so no second flow or approval request.
    fake.rows["sys_flow_context"].append({"source_record": DRAFT, "state": "WAITING", "name": APPROVAL})
    r = kb.now_kb_publish_article("KB0010100", confirmed=True)
    assert r["action"] == "not_published" and "already running" in r["reason"]
    assert fake.published_id is None and fake.state_reads == 0


def test_can_publish_false_is_not_published_with_the_platforms_reason_and_no_wait(fake):
    fake.script_answer = {"action": "executed", "result": {"found": True, "before": "draft", "can_publish": False,
                                                           "publish_returned": False}}
    fake.published_id, fake.publish_to = DRAFT, "draft"  # count its reads; nothing publishes it
    r = kb.now_kb_publish_article("KB0010100", confirmed=True)
    assert r["action"] == "not_published" and "canPublish is false" in r["reason"]
    assert fake.state_reads == 1


def test_found_false_is_not_published_and_does_not_wait(fake):
    fake.script_answer = {"action": "executed", "result": {"found": False}}
    fake.published_id, fake.publish_to = DRAFT, "draft"
    r = kb.now_kb_publish_article("KB0010100", confirmed=True)
    assert r["action"] == "not_published" and "couldn't read the article" in r["reason"]
    assert fake.state_reads == 1


def test_a_script_error_with_the_article_still_a_draft_is_unconfirmed(fake):
    fake.script_answer = {"action": "script_error", "error": "TypeError", "message": "x is undefined"}
    fake.published_id, fake.publish_to = DRAFT, "draft"
    r = kb.now_kb_publish_article("KB0010100", confirmed=True)
    assert r["action"] == "unconfirmed" and "script_error" in r["reason"] and "x is undefined" in r["reason"]
    assert fake.state_reads == ALL_READS  # waited: the flow may have started before the error


def test_a_call_that_raises_is_judged_by_the_read_back_after_waiting(fake, monkeypatch):
    # The POST may have reached the instance, and the flow may finish later: wait, then say.
    def publish_anyway(script, confirmed=False):
        fake.published_id = DRAFT
        raise ConnectionError("reset")

    monkeypatch.setattr(kb.verbs, "now_run_script", publish_anyway)
    fake.publish_after_reads = 2
    r = kb.now_kb_publish_article("KB0010100", confirmed=True)
    assert r["action"] == "published" and r["script"]["error"] == "ConnectionError"
    assert fake.state_reads == 3


def test_a_call_that_raises_and_publishes_nothing_is_unconfirmed(fake, monkeypatch):
    def lost(script, confirmed=False):
        fake.published_id, fake.publish_to = DRAFT, "draft"
        raise TimeoutError("read timed out")

    monkeypatch.setattr(kb.verbs, "now_run_script", lost)
    r = kb.now_kb_publish_article("KB0010100", confirmed=True)
    assert r["action"] == "unconfirmed" and "TimeoutError" in r["reason"]


def test_a_read_back_that_fails_is_unconfirmed(fake, monkeypatch):
    orig = fake.now_get

    def get(table, sys_id, fields="", display_value=False):
        if fields == "workflow_state":
            raise RuntimeError("down")
        return orig(table, sys_id, fields, display_value)

    monkeypatch.setattr(kb.verbs, "now_get", get)
    r = kb.now_kb_publish_article("KB0010100", confirmed=True)
    assert r["action"] == "unconfirmed" and "before trying again" in r["note"]


def test_confirmed_publish_of_a_published_article_is_refused_and_runs_nothing(fake):
    r = kb.now_kb_publish_article("KB0010101", confirmed=True)
    assert r["action"] == "refused" and fake.scripts == []


def test_confirmed_publish_without_the_endpoint_runs_nothing(fake):
    fake.endpoint = None
    r = kb.now_kb_publish_article("KB0010100", confirmed=True)
    assert r["action"] == "endpoint_missing" and fake.scripts == []
