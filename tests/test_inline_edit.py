"""표에서 바로 고치기 (/api/cell) 통합 테스트.

웹 모듈은 import 시점에 DATA_DIR 에 DB 를 만들기 때문에, 임시 폴더를 가리키게
환경변수를 먼저 세우고 reload 한 뒤 진짜 HTTP 서버를 띄워서 확인한다.
브라우저가 하는 것과 같은 요청을 보내야 낙관적 잠금·형식 검사가 실제로
동작하는지 알 수 있기 때문이다.
"""

from __future__ import annotations

import html
import importlib
import json
import os
import re
import threading
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    data = tmp_path_factory.mktemp("cvdata")
    os.environ["CVTOOL_DATA_DIR"] = str(data)
    os.environ["CVTOOL_ADMIN_PASSWORD"] = "pw1234"
    os.environ["CVTOOL_ADMIN_ID"] = "admin"
    mod = importlib.import_module("cvtool.web.app")
    mod = importlib.reload(mod)
    mod.bootstrap_admin()

    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar())
    )

    class Client:
        module = mod
        base = f"http://127.0.0.1:{port}"

        def post(self, path: str, **fields):
            body = urllib.parse.urlencode(fields, doseq=True, encoding="utf-8").encode()
            req = urllib.request.Request(self.base + path, data=body)
            try:
                with self._opener.open(req, timeout=20) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:            # 4xx 도 본문을 봐야 한다
                return e.code, e.read().decode("utf-8", "replace")

        def cell(self, **fields):
            code, body = self.post("/api/cell", **fields)
            return code, json.loads(body)

        def get(self, path: str) -> str:
            with self._opener.open(self.base + path, timeout=20) as r:
                return r.read().decode("utf-8", "replace")

        def raw(self, path: str) -> bytes:
            with self._opener.open(self.base + path, timeout=20) as r:
                return r.read()

        def post_raw(self, path: str, **fields):
            body = urllib.parse.urlencode(fields, doseq=True, encoding="utf-8").encode()
            req = urllib.request.Request(self.base + path, data=body)
            try:
                with self._opener.open(req, timeout=20) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:      # 4xx 도 코드를 봐야 한다
                return e.code, e.read()

    def make_client():
        jar = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        c = Client()
        c._opener = jar
        return c

    Client.new = staticmethod(make_client)
    client = Client()
    client._opener = opener
    client.post("/login", userid="admin", password="pw1234")
    yield client
    server.shutdown()


@pytest.fixture
def cid(web):
    """빈 지원자 하나를 만들고 그 ID 를 돌려준다 (ID 는 무작위라 차집합으로 찾는다)."""
    before = {r.지원자_ID for r in web.module.store.list_all()}
    web.post("/candidate/new")
    after = {r.지원자_ID for r in web.module.store.list_all()}
    return (after - before).pop()


@pytest.fixture
def 채용cid(web, cid):
    """채용을 시작한 지원자. 채용 현황 표에는 이런 사람만 올라온다."""
    web.module.recruit.start(cid, "admin")
    return cid


# --- 기본 열 ----------------------------------------------------------------
def test_edit_cell_saves(web, cid):
    code, res = web.cell(id=cid, 항목="한글_이름", 새값="홍길동", 이전값="")
    assert code == 200 and res["ok"] is True
    assert res["raw"] == "홍길동"
    assert web.module.store.get(cid).한글_이름 == "홍길동"


def test_edit_cell_records_audit(web, cid):
    web.cell(id=cid, 항목="한글_이름", 새값="김철수", 이전값="")
    이력 = web.module.audit.for_target("지원자", cid)
    assert any(e.항목 == "한글_이름" and e.새값 == "김철수" for e in 이력)


def test_edit_cell_rejects_bad_format(web, cid):
    code, res = web.cell(id=cid, 항목="박사_졸업", 새값="20xx", 이전값="")
    assert code == 400 and res["ok"] is False
    assert "YYYYMM" in res["error"]
    assert web.module.store.get(cid).박사_졸업 == ""      # 저장되지 않았다


def test_edit_cell_rejects_value_outside_choices(web, cid):
    code, res = web.cell(id=cid, 항목="현재_신분", 새값="우주비행사", 이전값="")
    assert code == 400 and "다음 중 하나" in res["error"]


def test_edit_cell_detects_conflict(web, cid):
    """다른 사람이 먼저 바꿨으면 조용히 덮어쓰지 않는다."""
    web.cell(id=cid, 항목="한글_이름", 새값="먼저저장", 이전값="")
    code, res = web.cell(id=cid, 항목="한글_이름", 새값="나중저장", 이전값="")
    assert code == 409 and res["ok"] is False
    assert "다른 사람이" in res["error"]
    assert web.module.store.get(cid).한글_이름 == "먼저저장"


def test_edit_cell_rejects_readonly_column(web, cid):
    code, res = web.cell(id=cid, 항목="지원자_ID", 새값="X", 이전값=cid)
    assert code == 400 and "수정할 수 없습니다" in res["error"]


def test_edit_cell_unknown_candidate(web):
    code, res = web.cell(id="없는ID", 항목="한글_이름", 새값="x", 이전값="")
    assert code == 404 and res["ok"] is False


def test_registry_columns_are_not_editable_in_the_table(web, cid):
    """소속·전공을 표에서 고치면 대표명이 원문 자리에 들어가 사전과 꼬인다."""
    code, res = web.cell(id=cid, 항목="박사_학교", 새값="서울대학교", 이전값="")
    assert code == 400 and res["ok"] is False
    assert "명칭" in res["error"] or "표에서 직접" in res["error"]
    assert web.module.store.get(cid).박사_학교 == ""


def test_registry_column_editable_from_detail_with_dictionary(web, cid):
    """상세 화면에서는 사전에 있는 이름 중 골라 넣을 수 있다."""
    web.module.registry.observe("소속", "한국과학기술원")
    web.post("/candidate/edit", id=cid, 항목="석사_학교", 새값="한국과학기술원", 이전값="")
    assert web.module.store.get(cid).석사_학교 == "한국과학기술원"


def test_detail_edit_rejects_name_outside_dictionary(web, cid):
    web.post("/candidate/edit", id=cid, 항목="학사_학교", 새값="없는대학교", 이전값="")
    assert web.module.store.get(cid).학사_학교 == ""


# --- 사용자 정의 열 ----------------------------------------------------------
def test_edit_custom_cell(web, cid):
    web.module.store.add_field("희망연봉", "숫자")
    code, res = web.cell(id=cid, 항목="희망연봉", 새값="9000", 이전값="", scope="사용자")
    assert code == 200 and res["ok"] is True
    assert web.module.store.custom_values(cid)["희망연봉"] == "9000"


def test_edit_custom_cell_rejects_bad_value(web, cid):
    web.module.store.add_field("연봉", "숫자")
    code, res = web.cell(id=cid, 항목="연봉", 새값="많이", 이전값="", scope="사용자")
    assert code == 400 and "숫자" in res["error"]


def test_edit_custom_cell_detects_conflict(web, cid):
    web.module.store.add_field("메모", "텍스트")
    web.cell(id=cid, 항목="메모", 새값="먼저", 이전값="", scope="사용자")
    code, res = web.cell(id=cid, 항목="메모", 새값="나중", 이전값="", scope="사용자")
    assert code == 409 and "다른 사람이" in res["error"]


def test_edit_custom_cell_unknown_field(web, cid):
    code, res = web.cell(id=cid, 항목="없는열", 새값="x", 이전값="", scope="사용자")
    assert code == 404


# --- 화면 렌더링 -------------------------------------------------------------
def test_dashboard_renders_editable_cells(web, cid):
    page = web.get("/")
    assert "data-col='한글_이름'" in page
    assert "/api/cell" in page                       # 저장 스크립트가 붙어 있다


def test_computed_columns_are_not_editable(web):
    """등급별 논문 수는 계산 결과라 손으로 못 고친다."""
    assert web.module._editable("한글_이름") is True
    assert web.module._editable("지원자_ID") is False
    assert web.module._editable("1저자_해외논문_최우수") is False


def test_cell_keeps_raw_and_display_apart(web):
    from cvtool.edit import field_spec

    td = web.module._cell("CV-1", "박사_학교", "KAIST", "한국과학기술원", field_spec("박사_학교"))
    assert "data-raw='한국과학기술원'" in td
    assert ">KAIST</td>" in td


def test_cell_escapes_quotes(web):
    from cvtool.edit import field_spec

    td = web.module._cell("CV-1", "한글_이름", "a'b\"c", "a'b\"c", field_spec("한글_이름"))
    assert "a'b" not in td.replace("&#x27;", "")      # 따옴표가 그대로 새어나가지 않는다


def test_select_column_carries_options(web):
    from cvtool.edit import field_spec

    td = web.module._cell("CV-1", "현재_신분", "박사", "박사", field_spec("현재_신분"))
    assert "data-kind='select'" in td
    assert "포닥" in td


# --- 권한 -------------------------------------------------------------------
def test_field_worker_cannot_edit_cells(web, cid):
    """현업은 지원자 정보를 고칠 수 없다. 화면에서 감추는 것만으로는 부족하다."""
    web.module.auth.create_user("hyunup", "현업이", "pw1234", "현업", 생성자="admin")
    other = web.__class__.new()
    other.post("/login", userid="hyunup", password="pw1234")
    code, res = other.cell(id=cid, 항목="한글_이름", 새값="몰래수정", 이전값="")
    assert code == 403 and res["ok"] is False
    assert web.module.store.get(cid).한글_이름 == ""


def test_logged_out_cell_edit_returns_json(web, cid):
    """로그인이 풀렸을 때 리다이렉트를 주면 화면에 HTML 이 박힌다."""
    익명 = web.__class__.new()
    code, res = 익명.cell(id=cid, 항목="한글_이름", 새값="x", 이전값="")
    assert code == 401 and res["ok"] is False


def test_recruit_table_does_not_edit_candidate_fields(web, 채용cid):
    """채용 현황은 채용 상태만 고친다. 지원자 정보를 여기서 고치면 실수로 덮어쓴다."""
    page = web.get("/recruit")
    assert "data-col=" not in page
    assert "id='recruitform'" in page          # 채용 상태는 한 폼으로 저장


# --- 명칭 관리 화면 ----------------------------------------------------------
@pytest.fixture
def names(web):
    """소속 표기 3개를 넣고 {원표기: id} 를 돌려준다."""
    reg = web.module.registry
    reg._conn.execute("DELETE FROM names WHERE 종류='소속'")
    reg._conn.execute("DELETE FROM name_classes WHERE 종류='소속'")
    reg._conn.commit()
    made = [reg.observe("소속", n) for n in ("포항공과대학교", "포항공대", "서울대학교")]
    return {i.원표기: i.id for i in made}


def test_names_page_keeps_navigation(web):
    """예전에는 이 화면만 me 를 넘기지 않아 상단 메뉴가 통째로 사라졌다."""
    page = web.get("/names?kind=" + urllib.parse.quote("학교"))
    assert "/history" in page and "/org" in page



def test_control_column_is_not_clipped(web, names):
    """수정 칸이 max-width 260px 에 잘려서 안 보이던 문제."""
    page = web.get("/names?kind=" + urllib.parse.quote("학교"))
    assert "td.ctl" in page and "<td class='ctl'>" in page






def test_bulk_save_renames_only_changed_rows(web, names):
    """줄마다 저장 버튼을 누르지 않고 표 전체를 한 번에 보낸다."""
    fields = {"kind": "소속", "id": list(names.values())}
    for 이름, nid in names.items():
        fields[f"표시명_{nid}"] = "SNU" if 이름 == "서울대학교" else 이름
    web.post("/names/save", **fields)

    이름들 = {i.표시명 for i in web.module.registry.list_all("소속")}
    assert "SNU" in 이름들 and "포항공대" in 이름들
    이력 = web.module.audit.recent(50, 대상종류="명칭")
    변경 = [e for e in 이력 if e.항목 == "표에 보일 이름"]
    assert len(변경) == 1 and 변경[0].새값 == "SNU"      # 안 바뀐 줄은 이력이 안 남는다


def test_save_tells_what_changed(web, names):
    """저장하고 나면 무엇을 바꿨는지 화면에 남아야 한다."""
    nid = names["서울대학교"]
    code, body = web.post("/names/save", kind="소속", id=nid,
                          **{f"표시명_{nid}": "SNU"})
    assert "class='toast ok'" in body                # 리다이렉트를 따라간 결과 화면
    assert "서울대학교" in body and "SNU" in body
    assert "1건 저장했습니다" in body



def test_grade_and_display_saved_together(web):
    """학회 화면은 이름·등급·국내해외를 한 폼으로 함께 저장한다."""
    reg = web.module.registry
    나 = reg.observe("학회", "International Conference on Sample Things")
    code, body = web.post("/names/save", kind="학회", id=나.id,
                          **{f"표시명_{나.id}": "ICST",
                             f"등급_{나.id}": "최우수",
                             f"국내해외_{나.id}": "해외"})
    이후 = reg.get(나.id)
    assert (이후.표시명, 이후.등급, 이후.국내해외) == ("ICST", "최우수", "해외")
    assert "등급 최우수" in body and "해외" in body


def test_school_kind_renamed_to_affiliation(web):
    """학교 사전이 회사까지 담게 '소속' 으로 바뀌었다."""
    from cvtool.names import KINDS
    from cvtool.schemas import NAME_COLUMNS

    assert "소속" in KINDS and "학교" not in KINDS
    assert NAME_COLUMNS["현재_소속"] == "소속"
    assert NAME_COLUMNS["박사_학교"] == "소속"


def test_old_school_bookmark_still_opens(web):
    """예전 링크(`?kind=학교`)로 들어와도 소속 화면이 떠야 한다."""
    page = web.get("/names?kind=" + urllib.parse.quote("학교"))
    assert "소속 " in page


def test_company_names_group_automatically(web):
    """(주) 같은 괄호 표기는 줄은 따로지만 이름은 같게 붙는다."""
    reg = web.module.registry
    a = reg.observe("소속", "(주)가나다소프트")
    b = reg.observe("소속", "가나다소프트")
    assert a.id != b.id                         # 표기마다 한 줄
    assert a.표시명 == b.표시명                   # 이름은 자동으로 같아진다


# --- CV 업로드 탭 ------------------------------------------------------------
def test_upload_has_its_own_tab(web):
    page = web.get("/")
    assert "href='/upload'" in page


def test_upload_page_has_the_form(web):
    page = web.get("/upload")
    assert "enctype='multipart/form-data'" in page
    assert "CV 없이 지원자 추가" in page


def test_dashboard_no_longer_shows_upload_box(web):
    """표를 보러 올 때마다 업로드 상자가 자리를 차지하지 않게 뺐다."""
    page = web.get("/")
    assert "enctype='multipart/form-data'" not in page


# --- 채용 현황 일괄 저장 ------------------------------------------------------
@pytest.fixture
def org(web):
    """부서 하나 + 과제 하나를 만들고 id 를 돌려준다."""
    a = web.module.auth
    if not a.departments():
        a.add_department("반도체사업부")
    did = a.departments()[0]["id"]
    if not a.projects(did):
        a.add_project(did, "차세대공정")
    return did, a.projects(did)[0]["id"]


def test_recruit_saves_many_rows_at_once(web, cid, org):
    did, pid = org
    code, body = web.post(
        "/recruit/save",
        **{f"부서_{cid}": str(did), f"과제_{cid}": str(pid),
           f"단계_{cid}_서류 검토": "합격", f"비고_{cid}": "1차 통과"},
    )
    p = web.module.recruit.get(cid)
    assert (p.부서_id, p.project_id) == (did, pid)
    assert p.단계상태["서류 검토"] == "합격"
    assert p.비고 == "1차 통과"
    assert "저장했습니다" in body


def test_recruit_save_reports_nothing_changed(web, cid):
    code, body = web.post("/recruit/save", **{f"비고_{cid}": ""})
    assert "바뀐 내용이 없습니다" in body


def test_recruit_save_is_recorded_in_history(web, cid, org):
    did, pid = org
    web.post("/recruit/save", **{f"단계_{cid}_전화 면접": "불합격"})
    이력 = web.module.audit.recent(50, 대상종류="채용현황")
    assert any(e.항목 == "전화 면접" and e.새값 == "불합격" for e in 이력)


def test_recruit_has_separate_dept_and_project_columns(web, 채용cid, org):
    page = web.get("/recruit")
    assert f"name='부서_{채용cid}'" in page
    assert f"name='과제_{채용cid}'" in page
    assert "syncProjects" in page          # 부서를 바꾸면 과제 목록이 따라온다


def test_recruit_has_no_per_row_save_buttons(web, cid):
    page = web.get("/recruit")
    for 옛라우트 in ("/recruit/stage", "/recruit/note", "/recruit/assign"):
        assert 옛라우트 not in page


def test_recruit_exports_xlsx(web, cid, org):
    did, pid = org
    web.post("/recruit/save", **{f"부서_{cid}": str(did), f"과제_{cid}": str(pid)})
    body = web.raw("/recruit/export.xlsx")
    assert body[:2] == b"PK"                       # zip = xlsx
    assert "반도체사업부".encode() in body or b"sheet1" in body


# --- 표 공통 기능 -------------------------------------------------------------
def test_every_page_gets_the_table_toolkit(web):
    for path in ("/", "/recruit", "/history", "/users"):
        page = web.get(path)
        assert "enhanceTables" in page, path
        assert "tableTSV" in page, path


def test_table_xlsx_turns_a_pasted_table_into_a_workbook(web):
    tsv = "이름\t점수\n홍길동\t90\n김영희\t85"
    code, body = web.post_raw("/table.xlsx", name="시험", tsv=tsv)
    assert body[:2] == b"PK"
    assert len(body) > 500


def test_table_xlsx_survives_duplicate_headers(web):
    code, body = web.post_raw("/table.xlsx", name="x", tsv="A\tA\n1\t2")
    assert body[:2] == b"PK"


def test_tsv_view_is_gone(web):
    page = web.get("/")
    assert "export.tsv" not in page


# --- 이름 바꾸기 / 브랜드 / 표 도구 --------------------------------------------


def test_system_is_named_for_applicants_not_cv_analysis(web):
    page = web.get("/")
    assert ">지원자 관리<" in page
    assert "CV 분석" not in page


def test_dashboard_has_no_upload_button(web):
    """업로드는 탭 하나로 충분하다."""
    page = web.get("/")
    assert "main" in page
    본문 = page.split("<main>", 1)[1]
    assert "href='/upload'" not in 본문


def test_upload_tab_still_in_the_menu(web):
    assert "href='/upload'" in web.get("/").split("<main>", 1)[0]


def test_column_menu_and_guard_are_shipped(web):
    page = web.get("/")
    for 기능 in ("openColMenu", "applyFilters", "sortBy", "dirtyGuard", "beforeunload"):
        assert 기능 in page, 기능


def test_copy_always_includes_headers(web):
    """머리글이 빠지면 엑셀에 붙였을 때 무슨 열인지 알 수 없다. 옵션 없이 항상 넣는다."""
    page = web.get("/")
    assert "twithhead" not in page                 # 켜고 끄는 옵션 없음
    assert "tcopy" not in page                     # '표 복사' 버튼도 없앰
    assert "머리글은 늘 함께 복사한다" in page


# --- 명칭 관리: 원래 표기를 감추지 않는다 ---------------------------------------
def test_names_page_shows_every_spelling_as_a_row(web, names):
    page = web.get("/names?kind=" + urllib.parse.quote("소속"))
    assert "<th>CV 에 적힌 표기</th>" in page
    assert "<th>같은 이름으로 묶인 표기</th>" in page
    assert "매칭 키" not in page                       # 정규화키는 화면에 안 낸다
    for 원문 in ("포항공과대학교", "포항공대", "서울대학교"):
        assert 원문 in page


def test_renaming_keeps_the_original_visible(web, names):
    nid = names["포항공과대학교"]
    web.post("/names/save", kind="소속", id=nid, **{f"표시명_{nid}": "포항공대"})
    page = web.get("/names?kind=" + urllib.parse.quote("소속"))
    assert "포항공과대학교" in page          # 고쳐도 CV 표기 줄은 그대로 남는다
    assert web.module.registry.get(nid).원표기 == "포항공과대학교"


def test_siblings_are_listed(web, names):
    """같은 이름으로 묶인 표기가 무엇인지 보여야 한다."""
    for 원표기, nid in names.items():
        if 원표기 != "서울대학교":
            web.post("/names/save", kind="소속", id=nid, **{f"표시명_{nid}": "포항공대"})
    page = web.get("/names?kind=" + urllib.parse.quote("소속"))
    형제 = web.module.registry.siblings(names["포항공대"])
    assert [n.원표기 for n in 형제] == ["포항공과대학교"]
    assert "포항공과대학교" in page


def test_a_wrong_grouping_can_be_undone(web, names):
    """잘못 묶였으면 그 줄의 이름만 다시 고치면 된다."""
    nid = names["포항공과대학교"]
    web.post("/names/save", kind="소속", id=nid, **{f"표시명_{nid}": "포항공대"})
    assert web.module.registry.get(nid).표시명 == "포항공대"
    web.post("/names/save", kind="소속", id=nid, **{f"표시명_{nid}": "포항공과대학교"})
    assert web.module.registry.get(nid).표시명 == "포항공과대학교"


def test_a_spelling_can_be_forgotten(web, names):
    nid = names["포항공대"]
    web.post("/names/forget", kind="소속", id=nid)
    assert web.module.registry.get(nid) is None


def test_names_sorted_by_display_name(web, names):
    """사전이니까 표에 보일 이름 오름차순이 기본이다."""
    assert [n.표시명 for n in web.module.registry.list_all("소속")] == [
        "서울대학교", "포항공과대학교", "포항공대",
    ]
    page = web.get("/names?kind=" + urllib.parse.quote("소속"))
    순서 = [page.index(f"name='표시명_{names[원표기]}'")
           for 원표기 in ("서울대학교", "포항공과대학교", "포항공대")]
    assert 순서 == sorted(순서)


def test_same_name_does_not_merge_rows(web, names):
    fields = {"kind": "소속", "id": list(names.values())}
    for nid in names.values():
        fields[f"표시명_{nid}"] = "포항공대"
    web.post("/names/save", **fields)
    남은 = web.module.registry.list_all("소속")
    assert len(남은) == 3                                   # 줄은 그대로
    assert {n.원표기 for n in 남은} == {"포항공과대학교", "포항공대", "서울대학교"}
    assert {n.표시명 for n in 남은} == {"포항공대"}


def test_merge_ui_is_gone(web, names):
    page = web.get("/names?kind=" + urllib.parse.quote("소속"))
    assert "mergeform" not in page
    assert "여기로 묶기" not in page


# --- 표 항목: DB 에 있는 모든 열이 보이고 고칠 수 있다 ---------------------------
def test_fields_page_lists_every_group_of_columns(web):
    """지원자 정보 · 관리 정보 · 채용 현황 · 추가한 열이 모두 나와야 한다."""
    page = web.get("/fields")
    for 구분 in ("지원자 정보", "관리 정보", "채용 현황", "추가한 열"):
        assert 구분 in page, 구분
    for 기본 in ("한글_이름", "박사_학교", "검토_필요"):
        assert 기본 in page


def test_fields_page_lists_recruit_and_management_columns(web):
    """예전에는 없던 채용 현황 열과 관리 정보 열도 관리할 수 있어야 한다."""
    page = web.get("/fields")
    for col in ("부서", "과제", "최종상태", "비고"):
        assert col in page, col
    for col in ("등록년도", "등록일시", "원본_파일명", "보관_만료일"):
        assert col in page, col


def test_recruit_column_can_be_renamed(web, 채용cid):
    """표 항목 탭에서 바꾼 이름이 채용 현황 표 머리글에 나와야 한다."""
    web.post("/fields/columns", col_1="최종상태", label_1="합격 여부", order_1="")
    page = web.get("/recruit")
    assert "합격 여부" in page


def test_management_column_can_be_shown_in_the_candidate_table(web, cid):
    """관리 정보 열은 기본으로 접혀 있지만 숨김을 풀면 표에 나온다."""
    assert "원본_파일명" not in web.module.표열()
    web.post("/fields/columns", col_1="원본_파일명", label_1="", order_1="")
    assert "원본_파일명" in web.module.표열()


def test_builtin_column_can_be_renamed_and_hidden(web, cid):
    web.post("/fields/columns", col_1="영문_이름", col_2="생년월일",
             label_1="English Name", order_1="", label_2="", order_2="", hide_2="on")
    page = web.get("/")
    assert "English Name" in page
    assert ">생년월일<" not in page


def test_builtin_column_order_changes_the_table(web, cid):
    web.post("/fields/columns", col_1="검토_필요", label_1="", order_1="1")
    assert web.module.표열()[0] == "검토_필요"


def test_column_settings_are_recorded_in_history(web):
    web.post("/fields/columns", col_1="경력_요약", label_1="경력", order_1="")
    이력 = web.module.audit.recent(50, 대상종류="표항목")
    assert any(e.항목 == "표에 보일 이름" and e.새값 == "경력" for e in 이력)


def test_builtin_columns_cannot_be_deleted(web):
    page = web.get("/fields")
    첫줄 = page.split("<td>한글_이름</td>", 1)[1].split("</tr>", 1)[0]
    assert "danger" not in 첫줄            # 기본 열에는 삭제 버튼이 없다


def test_field_worker_cannot_change_columns(web):
    try:
        web.module.auth.create_user("hyunup", "현업이", "pw1234", "현업", 생성자="admin")
    except ValueError:
        pass                                   # 다른 테스트에서 이미 만들었을 수 있다
    other = web.__class__.new()
    other.post("/login", userid="hyunup", password="pw1234")
    code, _ = other.post("/fields/columns", col_1="한글_이름", label_1="이름")
    assert code == 403


# --- 현업은 배정된 과제의 채용 현황만 --------------------------------------------
@pytest.fixture
def 현업(web, org):
    """차세대공정에 배정된 현업 계정으로 로그인한 클라이언트."""
    did, pid = org
    a = web.module.auth
    try:
        a.create_user("hyun2", "현업이", "pw1234", "현업", 생성자="admin")
    except ValueError:
        pass
    a.assign("hyun2", pid)
    c = web.__class__.new()
    c.post("/login", userid="hyun2", password="pw1234")
    return c, did, pid


def test_field_worker_sees_only_the_recruit_tab(현업):
    c, _did, _pid = 현업
    머리 = c.get("/recruit").split("<main>", 1)[0]
    assert "href='/recruit'" in 머리
    for 없어야할것 in ("<a href='/'", "href='/history'", "href='/upload'", "href='/mail'",
                   "href='/users'", "href='/org'", "href='/fields'"):
        assert 없어야할것 not in 머리, 없어야할것


def test_home_is_not_a_duplicate_of_the_applicant_tab(web):
    """제목과 '인재 Pool' 탭이 같은 곳으로 가서 헷갈렸다. 제목은 이름일 뿐이다."""
    머리 = web.get("/").split("<main>", 1)[0]
    assert "<span class='brand'>지원자 관리</span>" in 머리
    assert 머리.count("<a href='/'") == 1        # '인재 Pool' 탭 하나뿐


def test_the_current_tab_is_marked(web):
    """지금 어느 탭을 보고 있는지 눈에 보여야 한다."""
    for 경로, 라벨 in (("/", "인재 Pool"), ("/recruit", "채용 현황"),
                    ("/fields", "표 항목"), ("/history", "변경 이력")):
        머리 = web.get(경로).split("<main>", 1)[0]
        켜진것 = [조각.split(">", 1)[1].split("<", 1)[0]
               for 조각 in 머리.split("<a ")[1:] if 조각.startswith("href=") and "class=on" in 조각.split(">", 1)[0]]
        assert 켜진것 == [라벨], (경로, 켜진것)


def test_a_detail_screen_keeps_its_tab_lit(web, cid):
    """지원자 상세는 탭이 아니지만 인재 Pool 에서 들어간 화면이다."""
    머리 = web.get("/candidate?id=" + urllib.parse.quote(cid)).split("<main>", 1)[0]
    assert "<a href='/' class=on>인재 Pool</a>" in 머리


def test_field_worker_lands_on_recruit(현업):
    c, _d, _p = 현업
    assert "채용 현황" in c.get("/recruit")
    코드, 본문 = c.post_raw("/mail/send", id="1")      # 발송 권한 없음
    assert 코드 == 403


@pytest.mark.parametrize("경로", ["/history", "/upload", "/mail", "/users", "/org",
                                "/fields", "/export.xlsx", "/recruit/columns"])
def test_field_worker_is_refused_elsewhere(현업, 경로):
    """화면에서 감추는 것으로는 부족하다. 주소를 직접 쳐도 막혀야 한다."""
    c, _d, _p = 현업
    with pytest.raises(urllib.error.HTTPError) as exc:
        c._opener.open(c.base + 경로)
    assert exc.value.code == 403


def test_field_worker_gets_redirected_from_the_applicant_list(현업):
    c, _d, _p = 현업
    with c._opener.open(c.base + "/") as r:
        assert r.url.endswith("/recruit")        # 막는 대신 자기 홈으로 보낸다


def test_field_worker_cannot_open_another_projects_candidate(web, 현업, cid):
    """배정 안 된 지원자는 주소를 알아도 못 본다."""
    c, _did, _pid = 현업
    with pytest.raises(urllib.error.HTTPError) as exc:
        c._opener.open(c.base + "/candidate?id=" + urllib.parse.quote(cid))
    assert exc.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as exc:
        c._opener.open(c.base + "/candidate/file?id=" + urllib.parse.quote(cid))
    assert exc.value.code == 403


def test_field_worker_can_open_their_own_candidate(web, 현업, cid):
    c, did, pid = 현업
    web.module.recruit.set_assignment(cid, did, pid, "admin")
    본문 = c.get("/candidate?id=" + urllib.parse.quote(cid))
    assert "추출 결과" in 본문


def test_field_worker_does_not_see_management_cards(web, 현업, cid):
    """현업은 자기 과제 지원자가 어디까지 왔는지만 보면 된다.

    LLM 이 무엇을 확신하지 못했나(검토 필요) · 언제 누가 등록했나(관리 정보) ·
    누가 무엇을 고쳤나(변경 이력)는 채용을 굴리는 쪽의 일이다.
    """
    c, did, pid = 현업
    web.module.recruit.set_assignment(cid, did, pid, "admin")
    본문 = c.get("/candidate?id=" + urllib.parse.quote(cid))
    assert "관리 정보" not in 본문
    assert "변경 이력" not in 본문
    assert "검토 필요" not in 본문
    # 채용담당자에게는 그대로 보인다
    담당자본문 = web.get("/candidate?id=" + urllib.parse.quote(cid))
    assert "관리 정보" in 담당자본문
    assert "변경 이력" in 담당자본문


def test_purge_needs_delete_permission(현업):
    c, _d, _p = 현업
    코드, _ = c.post_raw("/candidates/purge")
    assert 코드 == 403


def test_department_delete_button_is_not_swallowed(web, org):
    """폼 안에 폼을 넣으면 브라우저가 안쪽을 버려서, 삭제가 이름 저장으로 바뀐다."""
    page = web.get("/org/edit")
    이름폼 = page.index("action='/org/dept/rename'")
    닫힘 = page.index("</form>", 이름폼)
    삭제폼 = page.index("action='/org/dept/delete'")
    assert 닫힘 < 삭제폼          # 삭제 폼이 이름 폼 **밖에** 있어야 한다


# --- 형식 검사를 건드리지 않는 선에서는 고칠 수 있다 -------------------------------
def test_stage_statuses_can_be_edited(web, 채용cid):
    """단계에서 고를 수 있는 상태는 추출 스키마와 무관하니 고칠 수 있어야 한다."""
    web.post("/recruit/statuses", choices="진행중 | 합격 | 불합격 | 보류 | 1차 통과")
    assert "1차 통과" in web.module.recruit.statuses()
    assert "1차 통과" in web.get("/recruit")


def test_stage_statuses_keep_the_load_bearing_ones(web):
    """합격·불합격은 최종상태와 탈락 판정이 쓰므로 뺄 수 없다."""
    코드, 본문 = web.post("/recruit/statuses", choices="진행중 | 보류")
    assert "뺄 수 없습니다" in 본문 or "뺄 수 없습니다" in web.get("/fields")
    assert "합격" in web.module.recruit.statuses()


def test_stage_status_in_use_cannot_be_removed(web, cid):
    web.post("/recruit/statuses", choices="진행중 | 합격 | 불합격 | 보류 | 검토중")
    web.module.recruit.set_stage(cid, "서류 검토", "검토중", "admin")
    web.post("/recruit/statuses", choices="진행중 | 합격 | 불합격 | 보류")
    assert "검토중" in web.module.recruit.statuses()      # 쓰고 있어서 안 빠진다
    web.module.recruit.set_stage(cid, "서류 검토", "", "admin")
    web.post("/recruit/statuses", choices="진행중 | 합격 | 불합격 | 보류")
    assert "검토중" not in web.module.recruit.statuses()  # 이제 뺄 수 있다


def test_builtin_choice_field_has_no_edit_form(web):
    """지원자 정보 열의 선택지는 추출 스키마에 걸려 있어 못 고친다."""
    page = web.get("/fields")
    줄 = page.split("<td>현재_신분", 1)[1].split("</tr>", 1)[0]
    assert "/fields/choices" not in 줄
    assert "추출 스키마" in 줄


def test_custom_choice_field_can_be_edited(web):
    web.post("/fields/add", name="면접등급", type="선택", choices="A|B", scope="지원자 정보")
    web.post("/fields/choices", col="면접등급", choices="A | B | C")
    f = web.module.store.field("면접등급")
    assert f["선택지"] == "A | B | C"


def test_custom_choice_in_use_cannot_be_dropped(web, cid):
    web.post("/fields/add", name="합숙여부", type="선택", choices="가능|불가", scope="지원자 정보")
    web.post("/candidate/custom", id=cid, 항목="합숙여부", 새값="가능")
    web.post("/fields/choices", col="합숙여부", choices="불가")
    assert "가능" in web.module.store.field("합숙여부")["선택지"]


def test_custom_field_scope_is_recorded(web):
    web.post("/fields/add", name="면접관 메모", type="텍스트", scope="채용 현황")
    f = web.module.store.field("면접관 메모")
    assert f["구분"] == "채용 현황"
    구분맵 = {c: g for g, c, _ in web.module.열목록()}
    assert 구분맵["면접관 메모"] == "채용 현황"
    # 인재 Pool 표에는 **보기 전용**으로 나온다 (고치는 자리는 채용 현황)
    assert "면접관 메모" in web.module.표열()


def test_recruit_scoped_custom_column_is_editable_there(web, 채용cid):
    web.post("/fields/add", name="면접 일정", type="텍스트", scope="채용 현황")
    현재 = web.module.recruit.columns()
    web.post("/recruit/columns", col=[*현재, "면접 일정"], order="")
    page = web.get("/recruit")
    assert "사용자열_" in page                       # 열 이름을 폼에 실어 보낸다
    n = page.split("name='사용자열_", 1)[1].split("'", 1)[0]
    web.post("/recruit/save", **{f"사용자열_{n}": "면접 일정",
                                 f"사용자_{n}_{채용cid}": "2026-09-01 14:00"})
    assert web.module.store.custom_values(채용cid).get("면접 일정") == "2026-09-01 14:00"


def test_custom_field_can_be_renamed_without_losing_values(web, cid):
    web.post("/fields/add", name="옛이름", type="텍스트", scope="지원자 정보")
    web.post("/candidate/custom", id=cid, 항목="옛이름", 새값="지킬 값")
    web.post("/fields/columns", col_1="옛이름", rename_1="새이름",
             scope_1="지원자 정보", label_1="", order_1="")
    assert web.module.store.field("옛이름") is None
    assert web.module.store.custom_values(cid).get("새이름") == "지킬 값"


def test_custom_field_type_change_is_refused_when_values_would_break(web, cid):
    web.post("/fields/add", name="점수칸", type="텍스트", scope="지원자 정보")
    web.post("/candidate/custom", id=cid, 항목="점수칸", 새값="아주 좋음")
    with pytest.raises(ValueError):
        web.module.store.update_field("점수칸", 유형="숫자")
    assert web.module.store.field("점수칸")["유형"] == "텍스트"


def test_mail_placeholder_notes_explain_the_derived_ones(web):
    """{{이름}} 은 DB 열이 아니다. 뭘로 채워지는지 화면에 적혀 있어야 한다."""
    코드, _ = web.post("/mail/template/add", name="설명확인")
    tid = web.module.mailing.templates()[-1].id
    page = web.get(f"/mail/template?id={tid}")
    assert "한글_이름, 비어 있으면 영문_이름" in page


# --- 인재 Pool → 채용 현황 ------------------------------------------------------
def test_pool_candidate_is_not_in_the_recruit_table(web, cid):
    """등록만 된 사람은 인재 Pool 에만 있다. 채용 현황은 뽑고 있는 사람만."""
    assert cid in web.get("/")
    assert cid not in web.get("/recruit")


def test_starting_recruitment_moves_them_over(web, cid):
    web.post("/candidates/start", id=cid)
    assert cid in web.get("/recruit")
    assert web.module.recruit.get(cid).시작함


def test_stopping_keeps_the_progress(web, cid):
    """내려도 진행 상황은 지우지 않는다 — 다시 올리면 그대로 이어진다."""
    web.post("/candidates/start", id=cid)
    web.module.recruit.set_stage(cid, "서류 검토", "합격", "admin")
    web.post("/candidates/stop", id=cid)
    assert cid not in web.get("/recruit")
    assert web.module.recruit.get(cid).단계상태["서류 검토"] == "합격"
    web.post("/candidates/start", id=cid)
    assert cid in web.get("/recruit")


def test_bulk_start_from_the_pool(web):
    ids = []
    for _ in range(2):
        before = {r.지원자_ID for r in web.module.store.list_all()}
        web.post("/candidate/new")
        ids.append(({r.지원자_ID for r in web.module.store.list_all()} - before).pop())
    web.post("/candidates/start", ids=ids)
    현황 = web.get("/recruit")
    assert all(i in 현황 for i in ids)


def test_pool_table_shows_the_recruit_state(web, cid):
    page = web.get("/")
    assert "인재 Pool" in page
    web.post("/candidates/start", id=cid)
    assert "채용 중" in web.get("/")


def test_the_recruit_column_has_no_per_row_buttons(web, cid):
    """줄마다 단추를 두면 화면이 단추로 뒤덮이고 한 명씩만 처리하게 된다.

    같은 일을 표 위 묶음 단추가 이미 한다 — 체크하고 한 번에.
    """
    page = web.get("/")
    본문 = page.split("<main>", 1)[1]
    표 = 본문.split("<table", 1)[1].split("</table>", 1)[0]
    assert "startform" not in 표 and "stopform" not in 표
    assert "<button" not in 표          # 표 안에는 단추가 없다
    assert "formaction='/candidates/start'" in 본문   # 묶음 단추는 표 위에 그대로 있다


def test_field_worker_cannot_start_someone_elses_candidate(web, 현업, cid):
    c, _d, _p = 현업
    코드, _ = c.post_raw("/candidates/start", id=cid)
    assert 코드 in (200, 303)                       # 권한은 있지만
    assert not web.module.recruit.get(cid).시작함    # 배정 안 된 사람은 안 바뀐다


def test_existing_progress_counts_as_started_after_upgrade(tmp_path):
    """예전 DB 를 열면, 이미 손댄 사람은 채용 중으로 남아야 한다."""
    import sqlite3

    from cvtool.recruit import RecruitStore

    db = tmp_path / "recruit.db"
    con = sqlite3.connect(db)                       # 채용시작일시 없던 시절 모양
    con.executescript("""
        CREATE TABLE recruit (지원자_ID TEXT PRIMARY KEY, 부서_id INTEGER,
            project_id INTEGER, 비고 TEXT DEFAULT '', 갱신일시 TEXT DEFAULT '',
            갱신자 TEXT DEFAULT '');
        CREATE TABLE stages (지원자_ID TEXT, 단계 TEXT, 상태 TEXT DEFAULT '',
            갱신일시 TEXT DEFAULT '', 갱신자 TEXT DEFAULT '',
            PRIMARY KEY (지원자_ID, 단계));
        INSERT INTO recruit (지원자_ID, project_id) VALUES ('CV-OLD1', 3);
        INSERT INTO recruit (지원자_ID) VALUES ('CV-OLD2');
        INSERT INTO stages VALUES ('CV-OLD3', '서류 검토', '합격', '', '');
    """)
    con.commit()
    con.close()

    r = RecruitStore(db)
    시작한사람 = r.started()
    assert "CV-OLD1" in 시작한사람          # 과제 배정이 있었다
    assert "CV-OLD3" in 시작한사람          # 단계 상태가 있었다
    assert "CV-OLD2" not in 시작한사람      # 아무것도 없던 줄은 인재 Pool 로
    r.close()


# --- 인재 Pool: 필터·열·메일 -----------------------------------------------------
def test_select_all_only_takes_visible_rows(web, cid):
    """표 위 찾기 칸으로 걸러 놓고 전체선택을 누르면 **보이는 줄만** 골라야 한다.

    화면에 없는 사람까지 선택되면 그대로 메일이 나가거나 지워진다.
    """
    page = web.get("/")
    assert "selectVisible(this)" in page
    js = page.split("function selectVisible", 1)[1].split("function ", 1)[0]
    assert "classList.contains('hide')" in js


def test_export_follows_the_search_filter(web):
    """걸러 놓고 받았는데 전체가 나오면 엉뚱한 사람에게 자료가 나간다."""
    이름들 = ["필터대상", "다른사람"]
    for 이름 in 이름들:
        before = {r.지원자_ID for r in web.module.store.list_all()}
        web.post("/candidate/new")
        새 = ({r.지원자_ID for r in web.module.store.list_all()} - before).pop()
        web.cell(id=새, 항목="한글_이름", 새값=이름, 이전값="")
    전체 = web.raw("/export.xlsx")
    걸린것 = web.raw("/export.xlsx?q=" + urllib.parse.quote("필터대상"))
    assert len(걸린것) < len(전체)
    page = web.get("/?q=" + urllib.parse.quote("필터대상"))
    assert "/export.xlsx?q=" in page          # 화면 단추도 조건을 달고 간다


def test_pool_table_shows_recruit_and_mail_columns(web, cid):
    """한 사람에 대해 아는 것을 보려고 화면을 옮겨 다니지 않아도 되게."""
    열 = web.module.표열()
    for c in ("부서", "과제", "서류 검토", "최종상태", "비고", "메일_발송이력"):
        assert c in 열, c
    page = web.get("/")
    # 머리글은 밑줄 뒤에 <wbr> 를 넣어 줄바꿈을 허용한다
    assert "메일_<wbr>발송이력" in page


def test_recruit_columns_are_read_only_in_the_pool(web, cid):
    """고치는 자리는 채용 현황이다. 여기서 덮어쓰면 어긋난다."""
    page = web.get("/")
    for c in ("부서", "최종상태", "서류 검토"):
        assert f"data-col='{c}'" not in page


def test_mail_button_sends_the_selection_to_compose(web, cid):
    page = web.get("/")
    assert "formaction='/mail/compose'" in page


# --- 메일: 고른 사람에게만 -------------------------------------------------------
@pytest.fixture
def 템플릿(web, request):
    """테스트마다 **다른 이름**의 템플릿. 이름이 겹치면 만들기가 거부되고,
    이미 보낸 기록이 남아 있는 남의 템플릿을 물려받게 된다."""
    이름 = f"안내메일-{request.node.name[-20:]}"
    tid = web.module.mailing.add_template(이름, 만든이="admin")
    web.post("/mail/template/save", id=str(tid), name=이름,
             subject="결과 안내", body="{{이름}}님 합격입니다", cc="",
             imgmode="본문+첨부")
    return web.module.mailing.template(tid)


def test_mail_tab_cannot_send_to_candidates(web, 템플릿):
    """메일 탭에서는 시험 발송까지만. 누가 서류 합격인지 거기서는 모른다."""
    page = web.get(f"/mail/test?id={템플릿.id}")
    assert "여기서는 실제 지원자에게 못 보냅니다" in page
    assert "시험 발송" in page


def test_compose_needs_a_selection(web):
    코드, 본문 = web.post("/mail/compose", ids=[])
    assert "고른 사람이 없습니다" in 본문


def test_compose_shows_who_will_get_it(web, cid, 템플릿):
    """보낼 목록에 사람이 서고, 줄마다 작성창을 열 수 있어야 한다."""
    web.cell(id=cid, 항목="이메일", 새값="a@b.com", 이전값="")
    web.cell(id=cid, 항목="한글_이름", 새값="홍길동", 이전값="")
    코드, 본문 = web.post("/mail/compose", ids=[cid], template=str(템플릿.id))
    assert "보낼 목록" in 본문
    assert "홍길동" in 본문 and "a@b.com" in 본문
    assert "작성창 열기" in 본문
    assert "한 번에 보내기" in 본문                  # 예전 방식은 옵션으로 남는다


def test_sending_requires_typing_the_headcount(web, cid, 템플릿):
    """확인창은 안 읽고 누르지만, 숫자는 화면을 봐야 칠 수 있다."""
    web.cell(id=cid, 항목="이메일", 새값="a@b.com", 이전값="")
    web.cell(id=cid, 항목="한글_이름", 새값="홍길동", 이전값="")
    코드, 본문 = web.post("/mail/send", ids=[cid], template=str(템플릿.id),
                        confirm="")
    assert "그대로 쳐 넣어야" in 본문
    assert not web.module.mailing.history(cid)          # 안 나갔다

    코드, 본문 = web.post("/mail/send", ids=[cid], template=str(템플릿.id),
                        confirm="99")
    assert "그대로 쳐 넣어야" in 본문
    assert not web.module.mailing.history(cid)


def test_already_sent_and_rejected_still_block(web, cid, 템플릿):
    """대상을 고르는 방식이 바뀌어도 막는 규칙은 그대로여야 한다."""
    web.cell(id=cid, 항목="이메일", 새값="a@b.com", 이전값="")
    web.cell(id=cid, 항목="한글_이름", 새값="홍길동", 이전값="")
    web.module.mailing.record(cid, 템플릿, "a@b.com", "제목", "본문", "성공",
                              보낸이="admin")
    코드, 본문 = web.post("/mail/compose", ids=[cid], template=str(템플릿.id))
    assert "못 보내는 사람" in 본문
    # 그 상태로 보내려 해도 나갈 사람이 0명이라 숫자가 안 맞는다
    코드, 본문 = web.post("/mail/send", ids=[cid], template=str(템플릿.id),
                        confirm="1")
    assert "그대로 쳐 넣어야" in 본문


def test_the_send_form_does_not_shadow_window_confirm(web, cid, 템플릿):
    """폼 안에 name='confirm' 입력칸이 있으면 인라인 onsubmit 에서 그 칸이
    window.confirm 을 가린다. 실제로 'confirm is not a function' 이 났다."""
    web.cell(id=cid, 항목="이메일", 새값="a@b.com", 이전값="")
    web.cell(id=cid, 항목="한글_이름", 새값="홍길동", 이전값="")
    코드, 본문 = web.post("/mail/compose", ids=[cid], template=str(템플릿.id))
    폼 = 본문.split("action='/mail/send'", 1)[1].split("</form>", 1)[0]
    assert "name='confirm'" in 폼
    assert "window.confirm(" in 폼


# --- 검토를 그 자리에서 -----------------------------------------------------------
@pytest.fixture
def 검토cid(web, cid):
    """검토 사유가 여럿 달린 지원자."""
    rec = web.module.store.get(cid)
    rec.검토_필요 = "Y"
    rec.검토_사유 = ("현재_신분을 판단하지 못함"
                 " / 연구분야 키워드를 뽑지 못함 (확인 필요)")
    web.module.store.save(rec)
    return cid


def test_detail_shows_a_review_card(web, 검토cid):
    page = web.get(f"/candidate?id={urllib.parse.quote(검토cid)}")
    assert "id='검토'" in page
    assert "검토 필요" in page
    assert "확인함" in page


def test_review_marks_the_related_rows(web, 검토cid):
    """어느 항목을 봐야 하는지 표에서 바로 보여야 한다."""
    page = web.get(f"/candidate?id={urllib.parse.quote(검토cid)}")
    본문 = page.split("id='추출결과'", 1)[1]
    줄 = 본문.split("<tr class='needs'>")
    assert len(줄) >= 3                       # 현재_신분 · 연구분야_키워드
    assert "현재_신분" in 본문 and "p-검토필요" in 본문


def test_marking_one_item_done_keeps_the_flag_until_all_are_done(web, 검토cid):
    web.post("/candidate/review/done", id=검토cid, 사유="현재_신분을 판단하지 못함")
    rec = web.module.store.get(검토cid)
    assert rec.검토_필요 == "Y"               # 아직 하나 남았다
    web.post("/candidate/review/done", id=검토cid,
             사유="연구분야 키워드를 뽑지 못함 (확인 필요)")
    assert web.module.store.get(검토cid).검토_필요 == ""


def test_review_can_be_undone(web, 검토cid):
    for 사유 in ("현재_신분을 판단하지 못함", "연구분야 키워드를 뽑지 못함 (확인 필요)"):
        web.post("/candidate/review/done", id=검토cid, 사유=사유)
    assert web.module.store.get(검토cid).검토_필요 == ""
    web.post("/candidate/review/undo", id=검토cid, 사유="현재_신분을 판단하지 못함")
    assert web.module.store.get(검토cid).검토_필요 == "Y"


def test_upload_status_links_straight_to_the_review(web, 검토cid):
    """검토 필요로 끝난 줄에서 바로 그 지원자로 갈 수 있어야 한다."""
    web.module._set_status("샘플.pdf", "검토필요", "사유 어쩌고", cid=검토cid)
    page = web.get("/upload")
    assert f"/candidate?id={urllib.parse.quote(검토cid)}#검토" in page
    assert "검토 2건" in page


def test_reanalysis_clears_old_review_marks(web, 검토cid):
    web.post("/candidate/review/done", id=검토cid, 사유="현재_신분을 판단하지 못함")
    assert web.module.store.review_done(검토cid)
    web.module.store.clear_reviews(검토cid)
    assert not web.module.store.review_done(검토cid)


# --- 상세 화면 저장은 하나로 ------------------------------------------------------
def test_detail_has_one_save_button(web, cid):
    """줄마다 저장 단추가 있으면 하나 고치고 다른 칸으로 가면 앞의 수정이 날아간다."""
    page = web.get(f"/candidate?id={urllib.parse.quote(cid)}")
    본문 = page.split("<main>", 1)[1]
    assert 본문.count("action='/candidate/edit'") == 0
    assert 본문.count("action='/candidate/custom'") == 0
    assert 본문.count("id='saveform'") == 1


def test_saving_several_fields_at_once(web, cid):
    page = web.get(f"/candidate?id={urllib.parse.quote(cid)}")
    폼 = page.split("id='saveform'", 1)[1].split("</form>", 1)[0]
    번호 = {}
    for 조각 in 폼.split("name='항목_")[1:]:
        n = 조각.split("'", 1)[0]
        이름 = 조각.split("value='", 1)[1].split("'", 1)[0]
        번호[이름] = n
    끝 = 페이지끝(page)
    web.post("/candidate/save", id=cid, 끝=끝, **{
        f"항목_{번호['한글_이름']}": "한글_이름",
        f"이전_{번호['한글_이름']}": "",
        f"값_{번호['한글_이름']}": "홍길동",
        f"항목_{번호['이메일']}": "이메일",
        f"이전_{번호['이메일']}": "",
        f"값_{번호['이메일']}": "hong@x.com",
    })
    rec = web.module.store.get(cid)
    assert rec.한글_이름 == "홍길동" and rec.이메일 == "hong@x.com"


def 페이지끝(page: str) -> str:
    return page.split("name='끝' value='", 1)[1].split("'", 1)[0]


def test_a_bad_value_does_not_lose_the_good_ones(web, cid):
    """한 칸이 형식에 걸려도 나머지는 저장돼야 한다."""
    page = web.get(f"/candidate?id={urllib.parse.quote(cid)}")
    폼 = page.split("id='saveform'", 1)[1].split("</form>", 1)[0]
    번호 = {}
    for 조각 in 폼.split("name='항목_")[1:]:
        n = 조각.split("'", 1)[0]
        번호[조각.split("value='", 1)[1].split("'", 1)[0]] = n
    코드, 본문 = web.post("/candidate/save", id=cid, 끝=페이지끝(page), **{
        f"항목_{번호['한글_이름']}": "한글_이름",
        f"이전_{번호['한글_이름']}": "",
        f"값_{번호['한글_이름']}": "김철수",
        f"항목_{번호['생년월일']}": "생년월일",
        f"이전_{번호['생년월일']}": "",
        f"값_{번호['생년월일']}": "이건날짜가아님",
    })
    assert web.module.store.get(cid).한글_이름 == "김철수"
    assert "생년월일" in 본문                    # 무엇이 틀렸는지 알려준다


def test_the_reason_text_is_not_editable_in_the_detail(web, 검토cid):
    """검토 카드가 관리하는 값이다. 글을 고치면 '확인함' 표시와 짝이 안 맞는다."""
    page = web.get(f"/candidate?id={urllib.parse.quote(검토cid)}")
    폼 = page.split("id='saveform'", 1)[1].split("</form>", 1)[0]
    assert "value='검토_사유'" not in 폼


def test_redirect_headers_survive_korean_fragments(web, 검토cid):
    """헤더는 latin-1 로만 나간다. '#검토' 를 붙였다가 서버가 터진 적이 있다."""
    코드, _ = web.post_raw("/candidate/review/done", id=검토cid,
                          사유="현재_신분을 판단하지 못함")
    assert 코드 in (200, 303)


def test_long_column_names_may_wrap_in_the_header(web):
    """'저널_주저자_수' 같은 이름은 공백이 없어 한 낱말로 취급된다.
    끊을 자리를 안 주면 값이 한 글자뿐인 열을 통째로 넓혀 버린다."""
    assert web.module.머리글("저널_주저자_수") == "저널_<wbr>주저자_<wbr>수"
    assert web.module.머리글("이름") == "이름"


def test_column_widths_match_what_the_column_holds(web):
    """다 같은 너비면 어떤 건 남고 어떤 건 모자란다."""
    폭 = web.module.열폭
    assert 폭("저널_주저자_수") == "w-xs"          # 숫자 한두 자리
    assert 폭("박사_졸업") == "w-sm"               # 202602
    assert 폭("한글_이름") == "w-lg"
    assert 폭("경력_요약") == "w-xl"               # 길어서 줄바꿈까지 허용
    assert 폭("서류 검토") == "w-sm"


def test_only_one_excel_button_per_table(web, cid):
    """카드 위에도 있고 표 위에도 있어서 두 개였다."""
    for 길 in ("/", "/recruit"):
        page = web.get(길)
        본문 = page.split("<main>", 1)[1]
        assert "엑셀(.xlsx) 다운로드" not in 본문
        # 표 우상단 단추는 공용 스크립트가 만든다
        assert "txlsx" in page


def test_the_pool_table_carries_its_server_export_link(web, cid):
    """단추 하나로 서버가 만든 엑셀(전화번호 앞자리 0 보존)을 받는다."""
    page = web.get("/")
    assert "data-export='/export.xlsx" in page
    page = web.get("/?q=" + urllib.parse.quote("홍길동"))
    assert "data-export='/export.xlsx?q=" in page


def test_table_cells_are_cut_not_wrapped(web, cid):
    """긴 글은 줄바꿈이 아니라 … 로 잘려 보인다.

    한 줄이 길어지면 그 줄만 키가 커져서 표가 들쭉날쭉해지고 눈이 줄을 못
    따라간다. **자르는 건 보이는 것뿐이고 내용은 그대로다** — 복사·엑셀·검색은
    원래 글을 쓴다.
    """
    css = web.get("/").split("<style>", 1)[1].split("</style>", 1)[0]
    assert ".scroll table td{white-space:nowrap" in css
    assert "text-overflow:ellipsis" in css
    # 예전에는 긴 글 열이 줄바꿈했다
    assert "w-xl{max-width:380px;min-width:200px}" in css


def test_the_full_text_stays_in_the_cell(web, cid):
    """잘려 보여도 DOM 에는 전체가 있어야 복사·엑셀이 온전하다."""
    긴글 = "플라즈마 식각 | 박막 증착 | 반도체 공정 | 표면 분석 | MOSFET | 그래프 신경망"
    web.cell(id=cid, 항목="연구분야_키워드", 새값=긴글, 이전값="")
    page = web.get("/")
    assert html.escape(긴글) in page          # 자르지 않고 통째로 실린다


def test_confirmed_review_reasons_leave_the_table(web, 검토cid):
    """'확인함' 을 누르면 표에서 그 사유가 사라져야 한다.

    예전에는 전부 확인해도 원문이 표에 그대로 남아, 아직 볼 게 있는 것처럼
    보였다. **DB 원문은 그대로 두고 보이는 글만** 줄인다.

    표 전체가 아니라 그 지원자 한 줄만 불러서 본다 (`?q=` 는 지원자 ID 로도
    걸린다). 다른 사람 줄까지 끌고 오면 무엇 때문에 통과했는지 알 수 없다.
    """
    한줄 = "/?q=" + urllib.parse.quote(검토cid)
    assert "현재_신분을 판단하지 못함" in web.get(한줄)

    web.post("/candidate/review/done", id=검토cid, 사유="현재_신분을 판단하지 못함")
    표 = web.get(한줄)
    assert "현재_신분을 판단하지 못함" not in 표      # 확인한 것은 빠지고
    assert "연구분야 키워드를 뽑지 못함" in 표        # 남은 것만 남는다

    web.post("/candidate/review/done", id=검토cid,
             사유="연구분야 키워드를 뽑지 못함 (확인 필요)")
    표 = web.get(한줄)
    assert "연구분야 키워드를 뽑지 못함" not in 표
    assert web.module.review.DONE_MARK in 표          # 한 번 걸렸던 흔적은 남는다

    # 원문은 DB 에 그대로 있다 — 무엇을 확신 못 했는지의 기록이다
    rec = web.module.store.get(검토cid)
    assert "현재_신분을 판단하지 못함" in rec.검토_사유


def test_the_review_reason_column_cannot_be_typed_over(web, 검토cid):
    """사유 글자가 '확인함' 기록의 열쇠라, 고치면 짝이 어긋난다."""
    원문 = web.module.store.get(검토cid).검토_사유
    code, 답 = web.cell(id=검토cid, 항목="검토_사유", 새값="아무거나", 이전값=원문)
    assert code == 400
    assert "수정할 수 없습니다" in 답.get("error", "")


def test_excel_also_drops_the_confirmed_reasons(web, 검토cid):
    """화면과 엑셀이 어긋나면 둘 중 하나를 믿을 수 없게 된다."""
    for 사유 in ("현재_신분을 판단하지 못함", "연구분야 키워드를 뽑지 못함 (확인 필요)"):
        web.post("/candidate/review/done", id=검토cid, 사유=사유)
    표값 = web.module._표값맵()
    assert 표값[검토cid]["검토_사유"] == web.module.review.DONE_MARK


# --- 명칭: 내가 본 것과 아직 안 본 것 -------------------------------------------
def test_new_names_start_unconfirmed(web):
    """CV 에서 자동으로 들어온 표기는 **아직 사람이 안 본 것**이다."""
    나 = web.module.registry.observe("소속", "가나다대학교")
    assert not 나.확인
    페이지 = web.get("/names?kind=" + urllib.parse.quote("소속"))
    본문 = 페이지.split("가나다대학교", 1)[0]
    assert "needs" in 본문.rsplit("<tr", 1)[-1]       # 그 줄이 노랗게 표시된다


def test_saving_a_row_marks_it_confirmed(web):
    """값을 고쳐 저장하면 체크를 깜박해도 본 것으로 친다."""
    reg = web.module.registry
    나 = reg.observe("소속", "포항공과대학교")
    web.post("/names/save", kind="소속", id=나.id,
             **{f"표시명_{나.id}": "포항공대"})
    이후 = reg.get(나.id)
    assert 이후.확인 and 이후.확인자 == "admin"


def test_confirming_without_changing_anything(web):
    """LLM 이 넣은 값이 이미 맞을 때 — 체크만 켜서 본 것으로 남긴다."""
    reg = web.module.registry
    나 = reg.observe("학회", "ICML")
    _, body = web.post("/names/save", kind="학회·저널", id=나.id,
                       **{f"표시명_{나.id}": "ICML", f"확인_{나.id}": "on"})
    assert reg.get(나.id).확인
    assert "확인 표시를 바꿨습니다" in body

    # 체크를 다시 끄면 되돌아간다 (잘못 눌렀을 때)
    web.post("/names/save", kind="학회·저널", id=나.id,
             **{f"표시명_{나.id}": "ICML"})
    assert not reg.get(나.id).확인


def test_unconfirmed_rows_come_first_and_can_be_filtered(web):
    reg = web.module.registry
    본것 = reg.observe("소속", "AAA대학교")
    reg.confirm(본것.id, "admin")
    reg.observe("소속", "ZZZ대학교")             # 이름 순서로는 뒤인데 안 본 것

    페이지 = web.get("/names?kind=" + urllib.parse.quote("소속"))
    assert 페이지.index("ZZZ대학교") < 페이지.index("AAA대학교")

    할일 = web.get("/names?kind=" + urllib.parse.quote("소속") + "&todo=1")
    표 = 할일.split("<table>", 1)[1].split("</table>", 1)[0]   # 자동완성 목록 말고 표
    assert "ZZZ대학교" in 표 and "AAA대학교" not in 표


def test_the_tab_badge_counts_unseen_names(web):
    """등급을 안 매긴 것만 세면 소속·전공은 늘 0 이라 아무 표시가 없었다."""
    reg = web.module.registry
    나 = reg.observe("소속", "표시안됨대학교")
    딱지 = 'class="pill p-안본것"'          # 탭 옆 숫자 (CSS 정의와 헷갈리지 않게)
    assert 딱지 in web.get("/")
    reg.confirm(나.id, "admin")
    for 남은 in reg.list_all():
        reg.confirm(남은.id, "admin")
    assert 딱지 not in web.get("/")


# --- 업로드 · 메일 · 열 정리 ---------------------------------------------------
def test_the_upload_page_never_reloads_itself(web):
    """<meta refresh> 로 다시 그리면 고르던 파일이 풀린다.

    분석이 도는 동안 CV 를 하나 더 올리려고 파일을 고르면, 5초마다 오는
    새로고침이 <input type=file> 선택을 지워 버렸다. 표 안쪽만 갈아 끼운다.
    """
    web.module._set_status("도는중.pdf", "처리중")
    page = web.get("/upload")
    assert "http-equiv='refresh'" not in page
    assert "http-equiv=\"refresh\"" not in page
    assert "id='현황표'" in page and "/status/rows" in page
    assert "type='file'" in page              # 올리는 칸은 그대로 있다


def test_the_status_fragment_is_just_the_table(web):
    web.module._set_status("조각.pdf", "처리중")
    조각 = web.get("/status/rows")
    assert "id='현황표'" in 조각 and "조각.pdf" in 조각
    assert "<html" not in 조각                 # 페이지 전체가 아니다


def test_uploading_is_not_blocked_while_something_is_being_analyzed(web):
    """분석은 뒤에서 큐로 돈다. 올리는 길은 잠기지 않는다."""
    web.module._set_status("먼저.pdf", "처리중")
    before = web.module._jobs.qsize()
    코드, _ = web.post_raw("/upload")          # 파일 없이 보내도 막히지 않는다
    assert 코드 in (200, 303)
    assert web.module._jobs.qsize() == before


def test_empty_placeholders_no_longer_block_sending(web, cid):
    """'빈칸을 채워 보내 주세요' 메일은 빈칸이 있는 사람에게 보내야 한다."""
    rec = web.module.store.get(cid)
    rec.이메일 = "a@b.com"
    rec.한글_이름 = "홍길동"
    web.module.store.save(rec)
    tid = web.module.mailing.add_template(
        "빈칸요청", "{{한글_이름}}님 정보 요청",
        "박사 학교: {{박사_학교}}<br>전화: {{전화번호}}")

    _, body = web.post("/mail/compose", ids=cid, template=str(tid))
    assert "못 보내는 사람" not in body         # 막지 않는다
    assert "빈 항목" in body                    # 목록에서 눈에 띄게 알린다
    assert "작성창 열기" in body                 # 그래도 보낼 수 있다

    작성창 = web.get(f"/mail/draft?tpl={tid}&id={cid}")
    assert "빈 채로" in 작성창                   # 작성창에서 다시 알린다
    assert "보내기" in 작성창


def test_the_pool_table_has_no_name_guess_column(web, cid):
    """검토 사유에 같은 말이 이미 있다. 열까지 두면 자리만 차지한다."""
    assert "이름_추정여부" not in web.module.표열()
    assert "이름_추정여부" not in web.get("/")


def test_detail_page_can_send_mail(web, cid):
    """예전에는 여기서 이력만 볼 수 있어 인재 Pool 로 돌아가야 했다."""
    rec = web.module.store.get(cid)
    rec.이메일 = "a@b.com"
    web.module.store.save(rec)
    page = web.get(f"/candidate?id={urllib.parse.quote(cid)}")
    assert "이 지원자에게 메일 보내기" in page
    assert "action='/mail/compose'" in page
    assert "a@b.com" in page


def test_detail_page_says_why_it_cannot_send(web, cid):
    """단추만 없어지면 왜 없는지 알 수가 없다."""
    page = web.get(f"/candidate?id={urllib.parse.quote(cid)}")
    assert "이메일 주소가 없어 보낼 수 없습니다" in page


def test_the_mail_editor_can_edit_a_table_after_inserting_it(web):
    """표를 넣기만 하고 손댈 수 없으면, 열 하나 더 넣으려고 처음부터 다시 만들어야 한다."""
    tid = web.module.mailing.add_template("표편집", "제목", "<p>본문</p>")
    page = web.get(f"/mail/template?id={tid}")
    assert "id='rttablebar'" in page                 # 표 도구 자리가 있고
    for 기능 in ("rtRow(", "rtCol(", "rtColWidth(", "rtBorder(",
                "rtHeadRow(", "rtTableDel("):
        assert 기능 in page, 기능
    assert "RT_GRID_R = 10" in page                  # 6×6 이 아니다
    assert "id='rt-mr'" in page                      # 더 크면 숫자로 직접


def test_column_width_is_not_trapped_inside_a_fixed_table(web):
    """표 폭이 고정이면 열 하나를 넓힐 때 옆 열이 줄어들 뿐이다.

    엑셀과 같아야 한다 — **열을 넓히면 표가 따라 넓어진다.** 그래서 표 폭을
    고를 수 있고(창에 맞춤 / 열 너비에 맞춤 / 내용에 맞춤 / 폭 고정),
    열 너비는 % 뿐 아니라 px 로도 잡을 수 있다.
    """
    tid = web.module.mailing.add_template("열너비", "제목", "<p>본문</p>")
    page = web.get(f"/mail/template?id={tid}")
    assert "id='rt-tblw'" in page                    # 표 폭 고르기
    assert "value='fit'" in page                     # 열 합계를 따르는 길
    assert "id='rt-colu'" in page                    # px / % 단위
    assert "function rtSumToTable" in page           # 표 폭 = 열 폭의 합
    assert "function rtDragInit" in page             # 경계선 끌기
    # 지원자 표용 260px 상한이 메일 표까지 죄면 안 된다
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    assert ".rt-body table td" in css and "max-width:none" in css


# --- 표 항목은 DB 의 **모든** 열을 다룬다 --------------------------------------------
def test_every_column_that_shows_up_can_be_managed(web):
    """화면에 보이는데 표 항목에 없으면 이름을 바꾸거나 숨길 방법이 없다."""
    관리 = {c for _구분, c, _추가 in web.module.열목록()}
    assert not set(web.module.표열()) - 관리
    assert not (set(web.module.지원자열()) | {"지원자_ID"}) - 관리
    for 열 in ("채용", "지원자_ID", "메일_발송이력", "임팩트_팩터", "구글_스칼라_링크"):
        assert 열 in 관리, 열


def test_the_recruit_state_is_a_real_column_now(web, cid):
    """맨 앞에 박아 둔 칸이 아니라 여느 열과 같다 — 이름도 바꾸고 숨길 수도 있다."""
    page = web.get("/")
    assert "인재 Pool</span>" in page
    web.post("/candidates/start", id=cid)
    assert "채용 중</span>" in web.get("/")
    web.post("/fields/columns", col_1="채용", label_1="진행", order_1="")
    assert "진행" in web.get("/")


def test_the_impact_factor_follows_the_dictionary(web, cid):
    """IF 는 명칭 관리에서 사람이 넣는 값이라, 고치면 표도 곧바로 따라와야 한다."""
    from cvtool.schemas import Paper

    rec = web.module.store.get(cid)
    rec.논문 = [Paper(제목="a", 제출처="Nature", 유형="저널",
                    저자구분="주저자", 국내해외="해외")]
    web.module.store.save(rec)
    n = web.module.registry.observe("저널", "Nature", 국내해외="해외", 유형="저널")
    web.module.registry.classify(n.id, IF="64.8", 국내해외="해외", 유형="저널")
    assert web.module.store.get(cid).to_row(web.module.registry)["임팩트_팩터"] == "64.8"
    web.module.registry.classify(n.id, IF="10")
    assert web.module.store.get(cid).to_row(web.module.registry)["임팩트_팩터"] == "10"


def test_the_scholar_link_opens_instead_of_editing(web, cid):
    rec = web.module.store.get(cid)
    rec.구글_스칼라_링크 = "https://scholar.google.com/scholar?q=Gil+Dong+Hong+KAIST"
    web.module.store.save(rec)
    page = web.get("/")
    assert "구글 스칼라 ↗" in page
    assert "scholar.google.com/scholar?q=Gil+Dong+Hong+KAIST" in page


# --- 열 순서는 끌어서 정한다 -------------------------------------------------------
def test_the_field_list_is_in_the_order_the_table_shows(web, cid):
    """묶음별로 나눠 보여주면 화면에서 몇 번째 열인지 알 수가 없다."""
    page = web.get("/fields")
    본문 = page.split("id='colorder'", 1)[1]
    나온차례 = re.findall(r"data-col='([^']+)'", 본문)
    assert 나온차례[:len(web.module.표열())] == web.module.표열()
    # 아는 열은 하나도 빠지지 않는다
    assert len(나온차례) == len(web.module.열목록())


def test_moving_a_row_moves_the_column(web, cid):
    """순서 칸에 숫자를 치지 않고 줄을 옮긴다 — 열이 쉰 개면 그게 유일한 길이다."""
    옛순서 = web.module.표열()
    옮길것 = 옛순서[4]
    # 화면이 하는 일과 같다: 줄 차례대로 1..N 을 매겨 보낸다
    새차례 = [옮길것] + [c for c in 옛순서 if c != 옮길것]
    필드 = {}
    for i, col in enumerate(새차례, start=1):
        필드[f"col_{i}"] = col
        필드[f"order_{i}"] = str(i)
        필드[f"label_{i}"] = ""
    web.post("/fields/columns", **필드)
    assert web.module.표열()[0] == 옮길것


def test_the_order_field_is_not_typed_by_hand_anymore(web):
    page = web.get("/fields")
    assert "class='ordfield'" in page          # 숨은 칸이 자리를 받아 적는다
    assert "colMove(this" in page              # ↑ ↓
    assert "draggable='true'" in page          # 끌기
    assert "<th class='ctl'>순서</th>" not in page   # 숫자를 치는 칸은 없다


# --- 미리보기는 모든 블록에 -------------------------------------------------------
def test_preview_works_for_aggregate_formulas_too(web, cid):
    """숫자·축표 블록에는 미리보기가 아예 없었다 — 그래서 '안 보인다' 였다."""
    did = web.module.boards.add("미리보기", "admin")
    web.module.boards.add_block(did, "숫자", 제목="수")
    page = web.get(f"/dash/edit?id={did}")
    수식칸 = page.split("name='formula'", 1)[1][:200]
    assert "class='fx'" in 수식칸 and "data-kind='agg'" in 수식칸

    import json as _json
    답 = _json.loads(web.get("/dash/preview?kind=agg&line=" + urllib.parse.quote("=COUNT(지원자)")))
    전체 = len(web.module.store.list_all())      # 앞선 시험들이 만들어 둔 사람까지
    assert 답["text"] == str(전체) and not 답["error"]


def test_an_aggregate_preview_needs_no_candidate(web):
    """집계는 사람 하나를 고르지 않아도 계산된다."""
    import json as _json
    답 = _json.loads(web.get("/dash/preview?kind=agg&id=&line=" + urllib.parse.quote("=COUNT(지원자)")))
    assert not 답["error"]


def test_the_preview_box_is_a_span_so_it_survives_inside_a_paragraph(web):
    """<p> 안의 <div> 는 브라우저가 <p> 를 먼저 닫아 버려 형제가 아니게 된다."""
    did = web.module.boards.add("칸모양", "admin")
    web.module.boards.add_block(did, "숫자", 제목="수")
    page = web.get(f"/dash/edit?id={did}")
    assert "<span class='fxout muted'></span>" in page
    assert "<div class='fxout muted'></div>" not in page


def test_every_preview_box_fills_in_not_just_the_last_one(web, cid):
    """타이머가 하나뿐이면 칸들이 서로를 취소해서 마지막 하나만 살아남는다."""
    did = web.module.boards.add("여러칸", "admin")
    web.module.boards.add_block(did, "목록", 제목="표", 설정={
        "목록대상": "지원자",
        "목록열": [["이름", "=한글_이름"], ["학교", "=박사_학교"]],
    })
    page = web.get(f"/dash/edit?id={did}")
    assert "el.__fxt" in page          # 타이머는 칸마다 따로
    assert "var FXt" not in page       # 하나로 쓰던 것은 없앴다


def test_the_dashboard_table_can_be_styled(web, cid):
    """칸 구분이 안 되면 격자를 켠다 — 이름 옆 학력이 어디까지인지 눈으로 못 자른다."""
    did = web.module.boards.add("모양", "admin")
    bid = web.module.boards.add_block(did, "목록", 제목="표", 설정={
        "목록대상": "지원자", "목록열": [["이름", "=한글_이름", "90"]],
        "테두리": "격자", "줄무늬": True, "촘촘히": True,
    })
    보기 = web.get(f"/dash/view?id={did}")
    # 목록은 '내용에 맞춤' 이 기본이라 fit 이 함께 붙는다 (넘치면 가로 스크롤)
    assert "class='dtbl b-grid zebra tight fit'" in 보기
    assert "style='width:90px'" in 보기
    assert "table.dtbl.b-grid th,table.dtbl.b-grid td{border:1px solid #222}" in 보기

    편집 = web.get(f"/dash/edit?id={did}")
    assert "name='border'" in 편집 and "name='colwidth'" in 편집
    assert "name='zebra'" in 편집 and "name='headbg'" in 편집


def test_the_dashboard_width_can_be_changed(web):
    did = web.module.boards.add("폭", "admin")
    처음 = web.get(f"/dash/view?id={did}").split("<body", 1)[1][:40]
    assert "--mainw:1600px" in 처음
    web.post("/dash/rename", id=str(did), name="폭", desc="", width="넓게")
    뒤 = web.get(f"/dash/view?id={did}").split("<body", 1)[1][:40]
    assert "--mainw:100%" in 뒤
    assert web.module.boards.get(did).너비 == "넓게"


def test_other_pages_keep_the_normal_width(web, cid):
    """대시보드 폭 설정이 다른 화면까지 넓히면 안 된다."""
    본문시작 = web.get("/").split("<body", 1)[1][:40]
    assert "--mainw" not in 본문시작        # CSS 안의 기본값은 그대로 둔다


def test_conditional_colours_reach_the_screen(web, cid):
    """값에 따라 칠하기 — 줄 색도 <tr> 이 아니라 칸마다 칠해야 얼룩말에 안 덮인다."""
    rec = web.module.store.get(cid)
    rec.한글_이름 = "칠할사람"
    web.module.store.save(rec)
    did = web.module.boards.add("칠하기", "admin")
    web.module.boards.add_block(did, "목록", 제목="표", 설정={
        "목록대상": "지원자", "줄무늬": True,
        "목록열": [["이름", "=한글_이름", ""]],
        "조건서식": [{"조건": '=한글_이름="칠할사람"', "대상": "줄 전체",
                   "배경": "#dcfce7", "글자": ""}],
    })
    보기 = web.get(f"/dash/view?id={did}")
    assert "background:#dcfce7" in 보기
    assert "class='w-md painted'" in 보기 or "painted" in 보기
    # <tr> 에 걸지 않는다 (물려받게 하면 얼룩말·hover 가 덮는다)
    assert "<tr style=" not in 보기


def test_the_colour_rules_editor_is_there(web):
    did = web.module.boards.add("규칙칸", "admin")
    web.module.boards.add_block(did, "목록", 제목="표", 설정={
        "목록대상": "지원자", "목록열": [["이름", "=한글_이름", ""]]})
    편집 = web.get(f"/dash/edit?id={did}")
    for 칸 in ("cfwhen", "cfwhere", "cfbg", "cffg", "cffgmode"):
        assert f"name='{칸}'" in 편집, 칸
    assert "값에 따라 칠하기" in 편집
    assert ">줄 전체<" in 편집 and ">이름<" in 편집      # 대상 고르개에 열 이름


def test_saving_a_colour_rule_checks_the_formula(web):
    did = web.module.boards.add("규칙저장", "admin")
    bid = web.module.boards.add_block(did, "목록", 제목="표", 설정={
        "목록대상": "지원자", "목록열": [["이름", "=한글_이름", ""]]})
    공통 = {"id": str(bid), "title": "표", "listtarget": "지원자",
          "colhead": "이름", "colformula": "=한글_이름", "colwidth": ""}
    _, body = web.post("/dash/block/save", **공통,
                       cfwhen="=없는열", cfwhere="줄 전체",
                       cfbg="#dcfce7", cffg="#000000", cffgmode="기본")
    assert "색칠 조건이 잘못됐습니다" in body
    assert not web.module.boards.block(bid).조건서식

    web.post("/dash/block/save", **공통,
             cfwhen='=한글_이름="홍"', cfwhere="줄 전체",
             cfbg="#dcfce7", cffg="#b91c1c", cffgmode="직접")
    규칙 = web.module.boards.block(bid).조건서식
    assert 규칙 == [{"조건": '=한글_이름="홍"', "대상": "줄 전체",
                  "배경": "#dcfce7", "글자": "#b91c1c"}]


def test_the_default_text_colour_is_not_saved(web):
    """'기본' 으로 두면 색을 저장하지 않는다 — 화면 테마를 따라가게."""
    did = web.module.boards.add("기본글자", "admin")
    bid = web.module.boards.add_block(did, "목록", 제목="표", 설정={
        "목록대상": "지원자", "목록열": [["이름", "=한글_이름", ""]]})
    web.post("/dash/block/save", id=str(bid), title="표", listtarget="지원자",
             colhead="이름", colformula="=한글_이름", colwidth="",
             cfwhen='=한글_이름="홍"', cfwhere="줄 전체",
             cfbg="#dcfce7", cffg="#b91c1c", cffgmode="기본")
    assert web.module.boards.block(bid).조건서식[0]["글자"] == ""


def test_every_block_kind_has_the_draft_form(web):
    """목록에만 붙일 이유가 없었다 — 빈 화면은 어느 블록이든 부담스럽다."""
    did = web.module.boards.add("초안전부", "admin")
    for 종류 in web.module.BLOCK_KINDS:
        web.module.boards.add_block(did, 종류, 제목=종류)
    page = web.get(f"/dash/edit?id={did}")
    assert page.count("action='/dash/block/draft'") == len(web.module.BLOCK_KINDS)
    # 종류마다 다른 보기를 준다 — 빈 칸에 '무엇을 적으라는 거지' 가 없게
    for 보기 in ("부서별로 단계마다", "최종 합격한 사람 수", "한 장씩"):
        assert 보기 in page


def test_a_wide_list_scrolls_sideways_instead_of_cutting(web, cid):
    """열이 많으면 화면 폭을 나눠 갖느라 전부 잘렸다. 가로로 넘기게 둔다."""
    did = web.module.boards.add("넓은표", "admin")
    web.module.boards.add_block(did, "목록", 제목="표", 설정={
        "목록대상": "지원자",
        "목록열": [[f"열{i}", "=한글_이름", ""] for i in range(12)],
    })
    보기 = web.get(f"/dash/view?id={did}")
    assert "fit'" in 보기.split("<table ", 1)[1][:80]
    css = 보기.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "table.dtbl.fit{width:auto;min-width:100%}" in css
    assert ".scroll{overflow:auto" in css            # 담는 칸이 스크롤한다


def test_the_shape_editor_is_on_every_table_block(web):
    """리스트에만 붙일 이유가 없었다."""
    did = web.module.boards.add("모양전부", "admin")
    for 종류 in ("목록", "축표", "표", "프로필"):
        web.module.boards.add_block(did, 종류, 제목=종류)
    page = web.get(f"/dash/edit?id={did}")
    assert page.count("name='border'") == 4
    assert page.count("name='tablewidth'") == 4


# --- 내부 메일 · 작성창 방식 발송 --------------------------------------------
#
# 채용 단계에 따라 지원자가 아니라 **내부로** 나가는 메일이 있다 (면접관에게
# CV 송부 등). 그리고 이제는 인원수를 쳐 넣는 대신 **한 통씩 작성창을 열어**
# 내용을 보고 고쳐서 보낸다.
@pytest.fixture
def 내부템플릿(web, request):
    """테스트마다 다른 이름 — 겹치면 남의 발송 기록을 물려받는다."""
    이름 = f"면접관 CV 송부-{request.node.name[-20:]}"
    tid = web.module.mailing.add_template(
        이름, "{{한글_이름}} 지원자 CV", "<p>첨부 확인 부탁드립니다.</p>",
        받는대상="내부", CV첨부=True)
    return web.module.mailing.template(tid)


def test_internal_mail_needs_no_applicant_address(web, cid, 내부템플릿):
    """지원자 주소를 몰라도 면접관에게는 보낼 수 있어야 한다."""
    web.cell(id=cid, 항목="한글_이름", 새값="홍길동", 이전값="")
    _, 본문 = web.post("/mail/compose", ids=[cid], template=str(내부템플릿.id))
    assert "못 보내는 사람" not in 본문
    assert "작성창에서 입력" in 본문                 # 받는 사람은 작성창에서
    assert "한 번에 보내기가 없습니다" in 본문        # 한 통씩만 보낸다


def test_draft_window_shows_everything_that_will_go(web, cid, 내부템플릿):
    web.cell(id=cid, 항목="한글_이름", 새값="홍길동", 이전값="")
    창 = web.get(f"/mail/draft?tpl={내부템플릿.id}&id={cid}")
    assert "홍길동 지원자 CV" in 창                  # 자리표시자가 채워진 제목
    assert "name='to'" in 창 and "name='subject'" in 창
    assert "본문칸" in 창                            # 본문을 고칠 수 있다
    assert "붙어서 나갈 파일" in 창                   # 첨부가 다 보인다
    assert "<header>" not in 창                      # 창이라 탭이 없다


def test_sending_one_keeps_what_i_edited(web, cid, 내부템플릿):
    """작성창에서 고친 그대로 나가고, 고친 그대로 기록에 남아야 한다."""
    web.cell(id=cid, 항목="한글_이름", 새값="홍길동", 이전값="")
    코드, 본문 = web.post(
        "/mail/send/one", tpl=str(내부템플릿.id), id=cid, send="1",
        to="interviewer@corp.com", cc="", subject="[면접] 홍길동 CV 송부",
        body="<p>내일 면접 건입니다.</p>")
    assert 코드 == 200 and "보냄표시" in 본문          # 부모 목록을 고치고 닫힌다

    (기록,) = web.module.mailing.history(cid)
    assert 기록["받는사람"] == "interviewer@corp.com"
    assert 기록["제목"] == "[면접] 홍길동 CV 송부"
    assert "내일 면접 건입니다" in 기록["본문"]
    assert 기록["받는대상"] == "내부"


def test_sending_one_needs_an_address(web, cid, 내부템플릿):
    코드, 본문 = web.post("/mail/send/one", tpl=str(내부템플릿.id), id=cid,
                        send="1", to="", subject="제목", body="<p>글</p>")
    assert "받는 사람을 적으세요" in 본문
    assert not web.module.mailing.history(cid)       # 안 나갔다


def test_changing_the_attachment_boxes_does_not_send(web, cid, 내부템플릿):
    """첨부 체크만 바꾼 것은 다시 그리기지 발송이 아니다."""
    코드, 본문 = web.post("/mail/send/one", tpl=str(내부템플릿.id), id=cid,
                        to="a@corp.com", subject="제목", body="<p>글</p>")
    assert "메일 쓰기" in 본문                        # 작성창을 다시 그렸다
    assert not web.module.mailing.history(cid)


def test_the_cv_is_actually_attached(web, cid, 내부템플릿):
    """CV 첨부를 켜면 그 지원자의 원본이 붙어서 나가야 한다."""
    store = web.module.store
    store.store_file(cid, "홍길동_이력서.txt", "CV 내용".encode())
    rec = store.get(cid)
    rec.원본_파일명 = "홍길동_이력서.txt"
    store.save(rec, 저장_파일명=f"{cid}.txt")

    붙는것, 오류 = web.module._지원자자료(cid, True, False)
    assert not 오류
    assert [n for n, _ in 붙는것] == ["홍길동_이력서.txt"]
    assert 붙는것[0][1] == "CV 내용".encode()

    창 = web.get(f"/mail/draft?tpl={내부템플릿.id}&id={cid}")
    assert "홍길동_이력서.txt" in 창                  # 작성창에 보인다


def test_the_history_tells_applicant_mail_from_internal(web, cid, 내부템플릿):
    web.cell(id=cid, 항목="한글_이름", 새값="홍길동", 이전값="")
    web.post("/mail/send/one", tpl=str(내부템플릿.id), id=cid, send="1",
             to="interviewer@corp.com", subject="제목", body="<p>글</p>")
    기록 = web.get("/mail/log")
    assert "interviewer@corp.com" in 기록
    assert "내부" in 기록


def test_the_cv_bytes_really_leave_the_building(web, cid, 내부템플릿, monkeypatch):
    """CV 첨부가 **실제 요청에 실려 나가는지**를 가짜 메일 서버로 확인한다.

    화면에 파일 이름이 보이는 것과, 그 파일이 진짜 나가는 것은 다른 이야기다.
    """
    import base64
    import dataclasses
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from cvtool import config as config_mod

    받은것: list[str] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            받은것.append(self.rfile.read(n).decode("utf-8"))
            out = _json.dumps({"resultCode": "SUCCESS"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    주소 = f"http://127.0.0.1:{srv.server_address[1]}/api/send"

    store = web.module.store
    store.store_file(cid, "이력서.txt", "진짜 CV 내용".encode())
    rec = store.get(cid)
    rec.원본_파일명 = "이력서.txt"
    store.save(rec, 저장_파일명=f"{cid}.txt")

    실제 = dataclasses.replace(
        config_mod.settings, mail_api_url=주소, mail_api_token="tok",
        mail_api_system_id="CVTOOL", mail_api_user_id="admin", mail_dry_run=False)
    # mailer 는 구현 모듈(mail.py)로 넘겨주는 얇은 껍데기라, 그쪽 설정을 바꾼다
    from cvtool.clients import mail as 구현
    monkeypatch.setattr(구현, "settings", 실제)
    try:
        코드, _본문 = web.post("/mail/send/one", tpl=str(내부템플릿.id), id=cid,
                             send="1", to="interviewer@corp.com",
                             subject="CV 송부", body="<p>보냅니다</p>", cv="1")
        assert 코드 == 200
        assert len(받은것) == 1
        보낸것 = _json.loads(받은것[0])
    finally:
        srv.shutdown()

    문자열 = _json.dumps(보낸것, ensure_ascii=False)
    assert "이력서.txt" in 문자열
    assert base64.b64encode("진짜 CV 내용".encode()).decode() in 문자열


# --- 예시 표 · 수식 자동완성 ------------------------------------------------------
#
# 열 이름과 함수를 전부 외우고 있어야 쓸 수 있는 도구는 아무도 안 쓴다.
def test_the_draft_form_takes_an_example_table(web):
    did = web.module.boards.add("예시표", "admin")
    web.module.boards.add_block(did, "목록", 제목="목록")
    page = web.get(f"/dash/edit?id={did}")
    assert "name='예시'" in page                     # 붙여넣을 칸이 있다
    assert "엑셀에서 복사해" in page
    assert "값은 예시로만 보고" in page               # 값을 베끼지 않는다고 알린다


def test_the_edit_page_carries_the_autocomplete_list(web):
    """서버에 다시 묻지 않게 목록을 화면에 한 번 심는다."""
    import json as _json

    did = web.module.boards.add("자동완성", "admin")
    web.module.boards.add_block(did, "목록", 제목="목록")
    page = web.get(f"/dash/edit?id={did}")
    자료 = _json.loads(page.split("window.수식목록 = ", 1)[1].split(";</script>", 1)[0])

    assert "박사_학교" in 자료["열"] and "박사_전공" in 자료["열"]
    assert "TEXTJOIN" in 자료["행함수"] and "IFERROR" in 자료["행함수"]
    assert "COUNT" in 자료["집계함수"] and 자료["대상"] == ["지원자", "채용"]
    assert "쓸 수 있는 열 이름 전부" in page          # 훑어보는 자리도 있다


def test_the_function_list_is_not_copied_by_hand(web):
    """화면 목록과 실제 함수가 어긋나면, 있는 함수를 없다고 알려 주게 된다."""
    from cvtool import expr

    import json as _json
    did = web.module.boards.add("함수목록", "admin")
    web.module.boards.add_block(did, "목록", 제목="목록")
    page = web.get(f"/dash/edit?id={did}")
    자료 = _json.loads(page.split("window.수식목록 = ", 1)[1].split(";</script>", 1)[0])
    assert set(자료["행함수"]) == set(expr.FUNC_NAMES)
