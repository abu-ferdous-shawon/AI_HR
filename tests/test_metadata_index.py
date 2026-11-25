from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from app.core import sites
from app.frappe_client.client import FrappeClient, FrappeError
from app.frappe_client.service_purpose import active_purpose, service_purpose
from app.indexer import sync
from app.indexer.cards import TIERS
from app.indexer.embeddings import GeminiEmbeddings
from app.indexer.vector_store import read_json, read_tier


@pytest.mark.parametrize(("indexing", "outcomes", "expected_status", "delays"), [
    (True, [requests.ConnectionError(), requests.ReadTimeout(), 200], 200, [1, 2]),
    (True, [requests.ConnectionError()] * 3, 0, [1, 2]),
    (True, [403], 403, []),
    (False, [requests.ConnectionError()], 0, []),
])
def test_frappe_connection_retries_are_bounded_and_indexing_only(
    monkeypatch, indexing, outcomes, expected_status, delays,
):
    client = FrappeClient("https://frappe.example", "test-key", "test-secret")
    pending = iter(outcomes)
    slept = []

    def get(*args, **kwargs):
        outcome = next(pending)
        if isinstance(outcome, Exception):
            raise outcome
        response = requests.Response()
        response.status_code = outcome
        response._content = b'{"data": [{"name": "Employee"}]}'
        return response

    monkeypatch.setattr(client.session, "get", get)
    monkeypatch.setattr("app.frappe_client.client.time.sleep", slept.append)
    with service_purpose("indexing") if indexing else nullcontext():
        if expected_status == 200:
            assert client.get_list("DocType") == [{"name": "Employee"}]
        else:
            with pytest.raises(FrappeError) as caught:
                client.get_list("DocType")
            assert caught.value.status_code == expected_status
    assert slept == delays
    assert list(pending) == []


def field(name, kind="Data", **properties):
    return {"fieldname": name, "label": name.replace("_", " ").title(), "fieldtype": kind, **properties}


class MetadataSite:
    def __init__(self):
        self.schemas = {
            "Leave Application": {
                "name": "Leave Application", "module": "HR", "description": "<p>Time away</p>",
                "search_fields": "employee, api_secret", "title_field": "employee", "is_submittable": 1,
                "__js": "must never be indexed",
                "fields": [
                    field("employee", "Link", options="Employee", reqd=1, in_list_view=1),
                    field("status", "Select", options="Open\nApproved", in_standard_filter=1),
                    field("items", "Table", options="Leave Item"),
                    field("reference", "Dynamic Link", options="reference_type"),
                    field("break", "Section Break"),
                    field("api_secret", default="must never be indexed"),
                ],
            },
            "Leave Item": {
                "name": "Leave Item", "module": "HR", "istable": 1,
                "fields": [field("leave_date", "Date")],
            },
            "Leave Settings": {"name": "Leave Settings", "module": "HR", "issingle": 1, "fields": []},
        }
        self.listing = {
            "DocType": [
                {"name": name, "module": "HR", "modified": "1"}
                for name in [*self.schemas, "Error Log"]
            ],
            "Module Def": [{"name": "HR"}],
            "Custom Field": [],
            "Property Setter": [],
            "Workflow": [{"name": "Approve Leave", "is_active": 1, "document_type": "Leave Application",
                          "modified": "1"}],
            "Report": [
                {"name": "Leave Summary", "modified": "1", "module": None, "ref_doctype": "Leave Application",
                 "report_type": "Script Report", "disabled": 0},
                {"name": "Disabled Report", "modified": "1", "module": "HR", "ref_doctype": "Leave Application",
                 "report_type": "Query Report", "disabled": 1},
            ],
        }
        self.meta_reads = []
        self.report_reads = []
        self.fail_meta = False

    def get_list(self, doctype, fields, filters, order_by, limit, start):
        assert active_purpose() == "indexing"
        assert order_by == "name asc"
        rows = self.listing[doctype]
        if doctype == "Report":
            assert filters == [["disabled", "=", 0], ["report_type", "in", ["Query Report", "Script Report"]]]
            rows = [row for row in rows if not row["disabled"]]
        return [{key: row[key] for key in fields}
                for row in sorted(rows, key=lambda r: r["name"])[start:start + limit]]

    def get_meta(self, doctype):
        self.meta_reads.append(doctype)
        if self.fail_meta:
            raise FrappeError(503, "temporary failure")
        return [deepcopy(self.schemas[doctype])]

    def get_doc(self, doctype, name):
        if doctype == "Workflow":
            return {"workflow_state_field": "workflow_state", "states": [{"state": "Reviewed", "doc_status": "1"}]}
        assert doctype == "Report"
        self.report_reads.append(name)
        return {**self.listing["Report"][0], "query": "must never be indexed"}

    def call_method(self, method, params):
        assert method == "frappe.desk.query_report.get_script"
        assert params == {"report_name": "Leave Summary"}
        return {"script": """
            frappe.query_reports["Other Report"] = {filters: [{fieldname: "other"}]};
            frappe.query_reports["Leave Summary"] = {
                filters: [
                    { fieldname: "company", label: __("Company"), fieldtype: "Link",
                      options: "Company", reqd: 1, on_change: () => { throw new Error("Do not run"); } },
                    { fieldname: "view", fieldtype: "Select", options: ["Summary", "Detailed"] },
                    { fieldname: "auth_token", fieldtype: "Data" }
                ]
            };
        """}


@pytest.fixture
def site_index(monkeypatch, tmp_path):
    client = MetadataSite()
    site = sites.get_default_site().model_copy(update={"embedding_provider": "local", "knowledge_packs": ["hrms_core"]})
    monkeypatch.setattr(sites, "_loaded", lambda: ({site.name: site}, site.name))
    monkeypatch.setattr(sync, "get_service_client", lambda: client)
    monkeypatch.setattr(sync.settings, "metadata_index_dir", str(tmp_path / "index"))
    monkeypatch.setattr(sync.settings, "knowledge_pack_dir", "")
    return site, client


def test_four_tiers_use_clean_metadata_and_report_filters(site_index):
    site, client = site_index
    assert sync.sync_metadata(site, check=True)["state"] == "stale"
    result = sync.sync_metadata(site)
    assert result["state"] == "updated"
    assert result["counts"] == {"table_index": 3, "schema_index": 3, "field_index": 5, "report_index": 1}
    path = Path(result["path"])
    for tier in TIERS:
        cards, index = read_tier(path, tier)
        assert index.ntotal == len(cards) == result["counts"][tier]
        assert "must never be indexed" not in str(cards)
    assert read_json(path / "alias_map.json")["pto"] == ["Leave Application"]
    assert read_json(path / "alias_map.json")["leave applications"] == ["Leave Application"]
    assert read_json(path / "business_keywords.json")["employee"] == ["Leave Application"]
    schema = next(card["data"] for card in read_json(path / "schema_index.json") if card["key"] == "Leave Application")
    assert schema["status_options"] == ["Open", "Approved"]
    assert schema["is_submittable"]
    assert schema["description"] == "Time away"
    assert schema["search_fields"] == ["employee"]
    assert schema["workflow_states"][0]["states"] == [{"state": "Reviewed", "doc_status": "1"}]
    assert schema["child_tables"]["Leave Item"]["istable"]
    assert schema["child_tables"]["Leave Item"]["fields"][0]["fieldname"] == "leave_date"
    assert any(link["kind"] == "Dynamic Link" for link in schema["relationships"])
    assert "Error Log" not in client.meta_reads
    assert sync.search_metadata(site, "table_index", "pto", 1)[0]["key"] == "Leave Application"
    report = sync.search_metadata(site, "report_index", "leave summary", 1)[0]["data"]
    assert report["module"] == "HR" and report["filter_source"] == "JavaScript"
    assert [item["fieldname"] for item in report["filters"]] == ["company", "view"]
    assert report["filters"][0]["reqd"]
    assert report["filters"][1]["options"] == "Summary\nDetailed"


def test_schema_hash_additions_deletion_reuse_and_failed_sync(site_index):
    site, client = site_index
    first = sync.sync_metadata(site)
    client.meta_reads.clear()
    client.report_reads.clear()
    assert sync.sync_metadata(site)["state"] == "unchanged"
    assert sync.sync_metadata(site, check=True)["state"] == "current"
    assert not client.meta_reads and not client.report_reads

    client.listing["Custom Field"] = [{"name": "Leave Item-project", "modified": "2", "dt": "Leave Item"}]
    client.schemas["Leave Item"]["fields"].append(field("project", "Link", options="Project"))
    assert sync.sync_metadata(site, check=True)["state"] == "stale"
    assert not client.meta_reads
    client.fail_meta = True
    with pytest.raises(FrappeError):
        sync.sync_metadata(site)
    assert sync.index_status(site)["path"] == first["path"]

    client.fail_meta = False
    client.meta_reads.clear()
    second = sync.sync_metadata(site)
    assert client.meta_reads == ["Leave Item"] and not client.report_reads
    assert second["new_embeddings"]["field_index"] == 1
    result = sync.search_metadata(site, "field_index", "project", 1, "Leave Item")
    assert result[0]["key"] == "Leave Item.project"

    client.listing["Custom Field"].clear()
    client.schemas["Leave Item"]["fields"].pop()
    client.listing["DocType"].append({"name": "New App Record", "module": "Custom App", "modified": "3"})
    client.listing["Module Def"].append({"name": "Custom App"})
    client.schemas["New App Record"] = {"name": "New App Record", "module": "Custom App", "fields": []}
    third = sync.sync_metadata(site)
    assert third["counts"]["field_index"] == first["counts"]["field_index"]
    assert third["counts"]["schema_index"] == first["counts"]["schema_index"] + 1
    assert sync.search_metadata(site, "field_index", "project", 10, "Leave Item")[0]["key"] == "Leave Item.leave_date"


def test_embedding_failure_keeps_previous_generation(site_index, monkeypatch):
    site, client = site_index
    first = sync.sync_metadata(site)
    client.listing["DocType"][0]["modified"] = "later"
    client.schemas["Leave Application"]["fields"].append(field("new_column"))

    class FailingEmbedder:
        dimension = 512

        def encode(self, texts, *, query):
            raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(sync, "create_embedder", lambda site: FailingEmbedder())
    with pytest.raises(RuntimeError, match="embedding unavailable"):
        sync.sync_metadata(site)
    assert sync.index_status(site)["path"] == first["path"]
    assert len(list(Path(first["path"]).parent.glob("snapshot-*"))) == 1


def test_gemini_keeps_batch_items_separate_and_sets_document_and_query_tasks():
    embedder = GeminiEmbeddings.__new__(GeminiEmbeddings)
    embedder.model = "gemini-embedding-001"
    calls = []

    def embed_content(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(embeddings=[
            SimpleNamespace(values=[1.0, 0.0]) for _ in kwargs["contents"]
        ])

    embedder.client = SimpleNamespace(models=SimpleNamespace(embed_content=embed_content))
    assert len(list(embedder.encode(["first", "second"]))) == 2
    assert len(list(embedder.encode(["search"], query=True))) == 1
    assert [call["config"].task_type for call in calls] == ["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]
    assert calls[0]["contents"][1].parts[0].text == "second"


def test_cli_checks_staleness_then_searches_a_persisted_snapshot(site_index, capsys):
    import json
    from app.indexer.__main__ import main

    assert main(["sync_metadata", "--check"]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "stale"
    assert main(["sync_metadata"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "updated"
    assert main(["sync_metadata", "--check"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "current"
    assert main(["search", "field_index", "status", "--doctype", "Leave Application", "--limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["key"] == "Leave Application.status"


@pytest.mark.parametrize(("tier", "query", "target"), [
    ("table_index", "time off", "Leave Application"),
    ("schema_index", "PTO", "Leave Application"),
    ("report_index", "leave summary", "Leave Summary"),
])
def test_exact_metadata_match_beats_higher_vector_score(site_index, monkeypatch, tier, query, target):
    import faiss
    import numpy as np

    site, _ = site_index
    sync.sync_metadata(site)
    index = faiss.IndexFlatIP(2)
    index.add(np.array([[1, 0], [0, 1]], dtype=np.float32))
    cards = [{"key": "Higher Vector Score", "data": {}}, {"key": target, "data": {}}]
    embedder = SimpleNamespace(dimension=2, encode=lambda texts, query: [[1, 0]])
    monkeypatch.setattr(sync, "read_tier", lambda path, tier: (cards, index))
    monkeypatch.setattr(sync, "create_embedder", lambda site: embedder)

    hits = sync.search_metadata(site, tier, query, 2)
    assert [hit["key"] for hit in hits] == [target, "Higher Vector Score"]
    assert [hit["score"] for hit in hits] == [0, 1]


def test_new_never_send_rule_invalidates_the_old_snapshot_and_filters_cards(site_index, monkeypatch, tmp_path):
    site, client = site_index
    schema = client.schemas["Leave Application"]
    schema["fields"].append(field("custom_private_note", "Small Text"))
    schema["search_fields"] += ", custom_private_note"
    result = sync.sync_metadata(site)
    before = Path(result["path"])
    assert "custom_private_note" in (before / "field_index.json").read_text()
    (tmp_path / "hrms_core.yaml").write_text("""
        pack_name: hrms_core
        never_send:
          doctype_fields:
            Leave Application: [custom_private_note]
    """)
    monkeypatch.setattr(sync.settings, "knowledge_pack_dir", str(tmp_path))
    assert sync.sync_metadata(site, check=True)["state"] == "stale"
    with pytest.raises(sync.IndexError, match="Knowledge packs changed"):
        sync.search_metadata(site, "schema_index", "Leave Application", 1)
    result = sync.sync_metadata(site)
    after = Path(result["path"])
    for tier in ("table_index", "schema_index", "field_index"):
        assert "custom_private_note" not in (after / f"{tier}.json").read_text()
    assert "custom private note" not in read_json(after / "business_keywords.json")
