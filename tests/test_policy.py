from app.core.policy_index import PolicyIndex
from app.tools.hr import policy
from app.tools.registry import run_tool
from app.core.security import UserContext
from app.core.policy_index import _strip_repeated_lines

CTX = UserContext("u@x.com", "HR-EMP-00001")


def make_folder(tmp_path):
    (tmp_path / "leave.md").write_text(
        "# Leave Policy\n\nAnnual leave: employees get 15 days per year.\n\n"
        "Sick leave needs a medical certificate after 3 days.", encoding="utf-8")
    (tmp_path / "conduct.txt").write_text("Dress code: smart casual on weekdays.", encoding="utf-8")
    (tmp_path / "bn.md").write_text("ছুটির নীতি: বছরে ১৫ দিন বার্ষিক ছুটি পাওয়া যাবে।", encoding="utf-8")
    return PolicyIndex(tmp_path)


def test_finds_the_right_document(tmp_path):
    assert make_folder(tmp_path).search("sick leave medical certificate")[0]["source"] == "leave.md"
    assert make_folder(tmp_path).search("dress code")[0]["source"] == "conduct.txt"


def test_bangla_text_is_searchable(tmp_path):
    assert make_folder(tmp_path).search("বার্ষিক ছুটি")[0]["source"] == "bn.md"


def test_no_match_returns_nothing(tmp_path):
    assert make_folder(tmp_path).search("quantum physics") == []


def test_missing_or_empty_folder_is_fine(tmp_path):
    assert PolicyIndex(tmp_path / "nope").search("leave") == []
    assert PolicyIndex(tmp_path).search("leave") == []


def test_new_files_are_picked_up_without_a_restart(tmp_path):
    index = make_folder(tmp_path)
    assert index.search("overtime") == []
    (tmp_path / "overtime.txt").write_text("Overtime is paid at 1.5 times.", encoding="utf-8")
    assert index.search("overtime")[0]["source"] == "overtime.txt"


def test_policy_tool_returns_excerpts_and_rejects_empty_queries(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "get_index", lambda: make_folder(tmp_path))
    ok = run_tool("search_hr_policy", {"query": "sick leave"}, CTX)
    assert ok["result"]["excerpts"][0]["source"] == "leave.md"
    assert "error" in run_tool("search_hr_policy", {"query": "  "}, CTX)

def test_synonyms_find_the_policys_own_wording(tmp_path):
    (tmp_path / "leave.md").write_text(
        "Earned leave accrues at one day for every eighteen days worked.", encoding="utf-8")
    (tmp_path / "social.md").write_text("Annual dinner is held in December.", encoding="utf-8")
    assert PolicyIndex(tmp_path).search("annual leave")[0]["source"] == "leave.md"


def test_plurals_match(tmp_path):
    (tmp_path / "a.md").write_text("Public holiday list is published each year.", encoding="utf-8")
    (tmp_path / "b.md").write_text("Dress code is smart casual.", encoding="utf-8")
    assert PolicyIndex(tmp_path).search("holidays")[0]["source"] == "a.md"


def test_page_headers_and_footers_are_removed():
    bodies = ["Probation lasts six months.", "Notice period is thirty days.",
              "Overtime is paid at one and a half times.", "Dress code is smart casual.",
              "Gratuity is paid after five years.", "Training leave needs approval."]
    pages = [f"HR Policy and Guideline {n}\n{body}" for n, body in enumerate(bodies, 1)]
    cleaned = _strip_repeated_lines(pages)
    assert all("HR Policy and Guideline" not in page for page in cleaned)
    assert [page.strip() for page in cleaned] == bodies