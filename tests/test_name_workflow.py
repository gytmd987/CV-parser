"""명칭 관리를 덜 귀찮게 + 목록 속도 + 분석 대기열이 재시작을 견디게.

- 이름 하나를 바꾸면 묶인 표기가 **전부** 따라간다 (한 줄씩 고치면 그룹이 쪼개졌다)
- 여러 줄을 골라 한 이름으로 합친다
- 사람이 확인한 이름을 물려받은 새 표기, 경력에만 나온 회사는 할 일로 세지 않는다
- 사전 조회 캐시가 고친 내용을 곧바로 따라간다
- 표에 보이는 이름으로 검색된다
- 분석을 기다리던 CV 가 서버 재시작 때 지워지지 않고 이어서 분석된다
"""

from __future__ import annotations

import importlib
import os
import queue
import re
import threading
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

import pytest

from cvtool.names import AUTO_CAREER, AUTO_SAME_NAME, NameRegistry, observe_record
from cvtool.schemas import Career, CVRecord
from cvtool.store import CandidateStore


@pytest.fixture
def reg(tmp_path):
    r = NameRegistry(tmp_path / "n.db")
    yield r
    r.close()


# --- 이름 단위로 바꾸기 --------------------------------------------------------
def _묶음(reg, 이름들: list[str], 표시명: str) -> list[int]:
    ids = [reg.observe("소속", x).id for x in 이름들]
    reg.merge(ids, 표시명)
    return ids


def test_rename_group_moves_every_spelling(reg):
    ids = _묶음(reg, ["POSTECH", "포항공과대학교", "포항공대"], "POSTECH")
    assert reg.rename_group("소속", "POSTECH", "포항공대(POSTECH)") == 3
    assert {reg.get(i).표시명 for i in ids} == {"포항공대(POSTECH)"}
    assert reg.display("소속", "포항공대") == "포항공대(POSTECH)"


def test_rename_group_into_existing_name_merges_and_keeps_its_class(reg):
    a = reg.observe("학회", "ICML")
    b = reg.observe("학회", "International Conference on Machine Learning")
    reg.classify(a.id, 등급="최우수", 국내해외="해외")
    reg.classify(b.id, 등급="일반")
    reg.rename_group("학회·저널", b.표시명, "ICML")
    합친것 = reg.get(b.id)
    assert (합친것.표시명, 합친것.등급, 합친것.국내해외) == ("ICML", "최우수", "해외")
    assert reg.class_of("학회·저널", b.표시명)["등급"] == "미분류"   # 옛 분류는 치웠다


def test_rename_group_to_new_name_carries_the_class(reg):
    a = reg.observe("저널", "Nano Energy")
    reg.classify(a.id, 등급="우수", IF="17.9")
    reg.rename_group("학회·저널", "Nano Energy", "NANO ENERGY")
    나 = reg.get(a.id)
    assert (나.표시명, 나.등급, 나.IF) == ("NANO ENERGY", "우수", "17.9")


def test_merge_joins_picked_rows_only(reg):
    ids = [reg.observe("소속", x).id for x in ("KAIST", "카이스트", "서울대학교")]
    바뀐것 = reg.merge(ids[:2], "KAIST")
    assert 바뀐것 == [ids[1]]                     # 이미 KAIST 인 줄은 안 바뀐다
    assert reg.get(ids[1]).표시명 == "KAIST"
    assert reg.get(ids[2]).표시명 == "서울대학교"


def test_merge_to_new_name_takes_class_from_most_seen(reg):
    a = reg.observe("학회", "NIPS")
    for _ in range(3):
        b = reg.observe("학회", "NeurIPS")
    reg.classify(b.id, 등급="최우수")
    reg.merge([a.id, b.id], "NeurIPS (NIPS)")
    assert reg.get(a.id).등급 == "최우수"


# --- 할 일로 세지 않을 것 ------------------------------------------------------
def test_inheriting_a_confirmed_name_is_not_a_todo(reg):
    먼저 = reg.observe("학회", "ICML")
    reg.confirm(먼저.id, 사람="admin")
    새것 = reg.observe("학회", "Proc. of ICML 2023")
    assert 새것.표시명 == "ICML"
    assert 새것.확인 and 새것.확인자 == AUTO_SAME_NAME
    assert reg.unconfirmed_count("학회·저널") == 0


def test_inheriting_an_unconfirmed_name_is_still_a_todo(reg):
    reg.observe("학회", "ICML")
    새것 = reg.observe("학회", "ICML 2024")
    assert not 새것.확인
    assert reg.unconfirmed_count("학회·저널") == 2


def test_auto_confirmed_rows_do_not_spread(reg):
    """자동으로 확인된 줄만 있는 이름은 «사람이 확인한 이름» 이 아니다."""
    reg.observe("소속", "가나다소프트", 검토=False)
    새것 = reg.observe("소속", "(주)가나다소프트")
    assert not 새것.확인


def test_career_only_companies_are_not_todos(reg):
    rec = CVRecord(지원자_ID="A", 경력=[Career(회사="가나다소프트"), Career(회사="라마바랩")])
    observe_record(rec, reg)
    assert reg.unconfirmed_count("소속") == 0
    assert reg.lookup("소속", "라마바랩").확인자 == AUTO_CAREER


def test_career_company_becomes_a_todo_when_seen_as_affiliation(reg):
    reg.observe("소속", "라마바랩", 검토=False)
    reg.observe("소속", "라마바랩")
    assert not reg.lookup("소속", "라마바랩").확인


def test_representative_career_is_still_reviewed(reg):
    """열로 뽑히는 대표 경력 회사는 표에 보이므로 할 일이다."""
    rec = CVRecord(지원자_ID="A", 경력_회사="가나다소프트",
                   경력=[Career(회사="가나다소프트"), Career(회사="라마바랩")])
    observe_record(rec, reg)
    assert not reg.lookup("소속", "가나다소프트").확인
    assert reg.lookup("소속", "라마바랩").확인


# --- 조회 캐시 ---------------------------------------------------------------
def test_cache_follows_own_writes(reg):
    나 = reg.observe("소속", "포항공대")
    assert reg.display("소속", "포항공대") == "포항공대"
    reg.classify(나.id, 표시명="POSTECH")
    assert reg.display("소속", "포항공대") == "POSTECH"
    reg.forget(나.id)
    assert reg.lookup("소속", "포항공대") is None


def test_cache_remembers_misses_until_a_write(reg):
    assert reg.lookup("소속", "KAIST") is None
    reg.observe("소속", "KAIST")
    assert reg.lookup("소속", "KAIST") is not None


def test_cache_follows_other_connections(tmp_path):
    """다른 프로세스(다른 연결)가 사전을 고쳐도 따라간다."""
    a = NameRegistry(tmp_path / "n.db")
    b = NameRegistry(tmp_path / "n.db")
    a.CACHE_CHECK_SECONDS = 0
    나 = a.observe("소속", "포항공대")
    assert a.display("소속", "포항공대") == "포항공대"
    b.classify(나.id, 표시명="POSTECH")
    assert a.display("소속", "포항공대") == "POSTECH"
    a.close()
    b.close()


# --- 검색 ---------------------------------------------------------------------
def test_search_finds_the_displayed_name(tmp_path, reg):
    store = CandidateStore(tmp_path / "c.db")
    store.save(CVRecord(지원자_ID="A", 박사_학교="포항공과대학교"))
    나 = reg.observe("소속", "포항공과대학교")
    reg.classify(나.id, 표시명="POSTECH")
    assert store.list_filtered("POSTECH") == []                  # 사전 없이는 원문만
    assert [r.지원자_ID for r in store.list_filtered("postech", registry=reg)] == ["A"]
    assert [r.지원자_ID for r in store.list_filtered("포항공과", registry=reg)] == ["A"]


# --- 분석 대기열 ----------------------------------------------------------------
def test_pending_job_files_are_not_orphans(tmp_path):
    store = CandidateStore(tmp_path / "c.db")
    저장명 = store.store_file("CV-WAIT", "a.pdf", b"x")
    store.add_job("CV-WAIT", "a.pdf", 저장명)
    assert store.orphan_files() == []
    assert [j["지원자_ID"] for j in store.pending_jobs()] == ["CV-WAIT"]
    store.finish_job("CV-WAIT")
    assert [f.name for f in store.orphan_files()] == [저장명]


def test_deleting_a_candidate_drops_its_job(tmp_path):
    store = CandidateStore(tmp_path / "c.db")
    store.save(CVRecord(지원자_ID="A"))
    store.add_job("A", "a.pdf", "A.pdf", "재분석")
    store.delete("A")
    assert store.pending_jobs() == []


# --- 웹 ---------------------------------------------------------------------
@pytest.fixture(scope="module")
def web(tmp_path_factory):
    data = tmp_path_factory.mktemp("cvdata")
    os.environ["CVTOOL_DATA_DIR"] = str(data)
    os.environ["CVTOOL_ADMIN_PASSWORD"] = "pw1234"
    os.environ["CVTOOL_ADMIN_ID"] = "admin"
    mod = importlib.reload(importlib.import_module("cvtool.web.app"))
    mod.bootstrap_admin()
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    base = f"http://127.0.0.1:{server.server_address[1]}"

    class Client:
        module = mod

        def post(self, path: str, **fields):
            body = urllib.parse.urlencode(fields, doseq=True, encoding="utf-8").encode()
            with opener.open(urllib.request.Request(base + path, data=body), timeout=20) as r:
                return r.status, r.read().decode("utf-8", "replace")

        def get(self, path: str) -> str:
            with opener.open(base + path, timeout=20) as r:
                return r.read().decode("utf-8", "replace")

    c = Client()
    c.post("/login", userid="admin", password="pw1234")
    yield c
    server.shutdown()


def _새이름(web, 종류: str, 표기들: list[str]) -> list[int]:
    reg = web.module.registry
    return [reg.observe(종류, x).id for x in 표기들]


def test_merge_from_the_spelling_view(web):
    reg = web.module.registry
    ids = _새이름(web, "소속", ["한국과학기술원", "카이스트", "KAIST-웹"])
    code, body = web.post("/names/save", kind="소속", id=ids, pick=ids[:2],
                          merge_to="KAIST-웹", action="merge")
    assert code == 200 and "합쳤습니다" in body
    assert {reg.get(i).표시명 for i in ids} == {"KAIST-웹"}
    assert reg.get(ids[0]).확인 and reg.get(ids[0]).확인자 == "admin"
    이력 = web.module.audit.recent(50, 대상종류="명칭")
    assert any(e.새값 == "KAIST-웹" and "한국과학기술원" in e.대상 for e in 이력)


def test_merge_needs_both_rows_and_a_name(web):
    ids = _새이름(web, "소속", ["합칠것없음"])
    code, body = web.post("/names/save", kind="소속", id=ids, merge_to="",
                          action="merge")
    assert "합칠 줄" in body


def test_confirm_all_marks_every_visible_row(web):
    reg = web.module.registry
    ids = _새이름(web, "전공", ["전산학", "컴퓨터공학"])
    web.post("/names/save", kind="전공", id=ids, action="confirm_all")
    assert all(reg.get(i).확인 for i in ids)


def test_name_view_lists_one_row_per_name(web):
    _새이름(web, "소속", ["연세대학교", "Yonsei University"])
    reg = web.module.registry
    reg.merge([reg.lookup("소속", "Yonsei University").id,
               reg.lookup("소속", "연세대학교").id], "연세대")
    page = web.get("/names?kind=" + urllib.parse.quote("소속") + "&view=name")
    assert "id='groupform'" in page
    assert page.count("name='g' value='연세대'") == 1   # 표기 둘이 한 줄
    assert "Yonsei University" in page and "연세대학교" in page


def test_rename_from_the_name_view_moves_all_spellings(web):
    reg = web.module.registry
    ids = _새이름(web, "소속", ["고려대학교", "Korea University"])
    reg.merge(ids, "고려대")
    page = web.get("/names?kind=" + urllib.parse.quote("소속") + "&view=name")
    그룹들 = re.findall(r"name='g' value='([^']*)'", page)
    g = 그룹들.index("고려대")
    code, body = web.post("/names/save_groups", kind="소속", g=그룹들,
                          **{f"이름_{g}": "Korea Univ."})
    assert "고려대 → Korea Univ." in body
    assert {reg.get(i).표시명 for i in ids} == {"Korea Univ."}
    assert all(reg.get(i).확인 for i in ids)


def test_name_view_saves_class_for_the_whole_name(web):
    reg = web.module.registry
    a = reg.observe("학회", "WebConf-테스트")
    page = web.get("/names?kind=" + urllib.parse.quote("학회·저널") + "&view=name")
    그룹들 = re.findall(r"name='g' value='([^']*)'", page)
    g = 그룹들.index("WebConf-테스트")
    web.post("/names/save_groups", kind="학회·저널", g=그룹들,
             **{f"등급_{g}": "우수", f"국내해외_{g}": "해외"})
    나 = reg.get(a.id)
    assert (나.등급, 나.국내해외) == ("우수", "해외")


def test_merge_names_in_the_name_view(web):
    reg = web.module.registry
    a, b = _새이름(web, "소속", ["성균관대학교", "SKKU"])
    page = web.get("/names?kind=" + urllib.parse.quote("소속") + "&view=name")
    그룹들 = re.findall(r"name='g' value='([^']*)'", page)
    골라 = [그룹들.index("성균관대학교"), 그룹들.index("SKKU")]
    web.post("/names/save_groups", kind="소속", g=그룹들, pick=골라,
             merge_to="성균관대", action="merge")
    assert reg.get(a).표시명 == reg.get(b).표시명 == "성균관대"


def test_resume_puts_pending_jobs_back(web, monkeypatch):
    from cvtool.web import jobs

    mod = web.module
    q: queue.Queue = queue.Queue()
    monkeypatch.setattr(jobs, "_jobs", q)            # 진짜 워커가 집어 가지 않게
    저장명 = mod.store.store_file("CV-RESUME", "이력서.pdf", b"x")
    mod.store.add_job("CV-RESUME", "이력서.pdf", 저장명)
    try:
        assert 저장명 not in [f.name for f in mod._startup_cleanup()] + \
            [f.name for f in mod.store.orphan_files()]
        assert (mod.store.files_dir / 저장명).exists()
        assert mod._resume_jobs() >= 1
        assert ("이력서.pdf", "CV-RESUME", 저장명) in list(q.queue)
        assert mod._status["이력서.pdf"]["state"] == "대기중"
    finally:
        mod.store.finish_job("CV-RESUME")


def test_enqueue_writes_to_db_first(web, monkeypatch):
    from cvtool.web import jobs

    mod = web.module
    q: queue.Queue = queue.Queue()
    monkeypatch.setattr(jobs, "_jobs", q)
    mod._enqueue("b.pdf", "CV-ENQ", "CV-ENQ.pdf")
    try:
        assert any(j["지원자_ID"] == "CV-ENQ" for j in mod.store.pending_jobs())
        assert q.get_nowait() == ("b.pdf", "CV-ENQ", "CV-ENQ.pdf")
    finally:
        mod.store.finish_job("CV-ENQ")


def test_saving_the_name_view_keeps_auto_confirmations(web):
    """다 본 이름은 확인 칸이 켜진 채로 들어온다. 그걸로 자동 확인 줄의
    확인자를 사람으로 덮어쓰면 안 된다."""
    reg = web.module.registry
    먼저 = reg.observe("학회", "AutoKeepConf")
    reg.confirm(먼저.id, 사람="admin")
    자동 = reg.observe("학회", "AutoKeepConf 2024")
    assert 자동.확인자 == AUTO_SAME_NAME
    page = web.get("/names?kind=" + urllib.parse.quote("학회·저널") + "&view=name")
    그룹들 = re.findall(r"name='g' value='([^']*)'", page)
    g = 그룹들.index("AutoKeepConf")
    web.post("/names/save_groups", kind="학회·저널", g=그룹들, **{f"확인_{g}": "on"})
    assert reg.get(자동.id).확인자 == AUTO_SAME_NAME
