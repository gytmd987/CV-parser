"""표에서 바로 고치기 (/api/cell) 통합 테스트.

웹 모듈은 import 시점에 DATA_DIR 에 DB 를 만들기 때문에, 임시 폴더를 가리키게
환경변수를 먼저 세우고 reload 한 뒤 진짜 HTTP 서버를 띄워서 확인한다.
브라우저가 하는 것과 같은 요청을 보내야 낙관적 잠금·형식 검사가 실제로
동작하는지 알 수 있기 때문이다.
"""

from __future__ import annotations

import importlib
import json
import os
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
                with self._opener.open(req) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:            # 4xx 도 본문을 봐야 한다
                return e.code, e.read().decode("utf-8", "replace")

        def cell(self, **fields):
            code, body = self.post("/api/cell", **fields)
            return code, json.loads(body)

        def get(self, path: str) -> str:
            with self._opener.open(self.base + path) as r:
                return r.read().decode("utf-8", "replace")

        def raw(self, path: str) -> bytes:
            with self._opener.open(self.base + path) as r:
                return r.read()

        def post_raw(self, path: str, **fields):
            body = urllib.parse.urlencode(fields, doseq=True, encoding="utf-8").encode()
            req = urllib.request.Request(self.base + path, data=body)
            with self._opener.open(req) as r:
                return r.status, r.read()

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


def test_recruit_table_does_not_edit_candidate_fields(web, cid):
    """채용 현황은 채용 상태만 고친다. 지원자 정보를 여기서 고치면 실수로 덮어쓴다."""
    page = web.get("/recruit")
    assert "data-col=" not in page
    assert "id='recruitform'" in page          # 채용 상태는 한 폼으로 저장


# --- 명칭 관리 화면 ----------------------------------------------------------
@pytest.fixture
def names(web):
    """소속 3건을 넣고 {표시명: id} 를 돌려준다."""
    reg = web.module.registry
    reg._conn.execute("DELETE FROM name_aliases")
    reg._conn.execute("DELETE FROM names WHERE 종류='소속'")
    reg._conn.commit()
    made = [reg.observe("소속", n) for n in ("포항공과대학교", "포항공대", "서울대학교")]
    return {i.표시명: i.id for i in made}


def test_names_page_keeps_navigation(web):
    """예전에는 이 화면만 me 를 넘기지 않아 상단 메뉴가 통째로 사라졌다."""
    page = web.get("/names?kind=" + urllib.parse.quote("학교"))
    assert "/history" in page and "/org" in page



def test_merge_column_is_not_clipped(web, names):
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
    변경 = [e for e in 이력 if e.항목 == "표시명"]
    assert len(변경) == 1 and 변경[0].새값 == "SNU"      # 안 바뀐 줄은 이력이 안 남는다


def test_save_tells_what_changed(web, names):
    """저장하고 나면 무엇을 바꿨는지 화면에 남아야 한다."""
    nid = names["서울대학교"]
    code, body = web.post("/names/save", kind="소속", id=nid,
                          **{f"표시명_{nid}": "SNU"})
    assert "class='done'" in body                    # 리다이렉트를 따라간 결과 화면
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


def test_company_names_normalize_together(web):
    """(주) 같은 괄호 표기는 저절로 묶인다."""
    reg = web.module.registry
    a = reg.observe("소속", "(주)가나다소프트")
    b = reg.observe("소속", "가나다소프트")
    assert a.id == b.id


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


def test_recruit_has_separate_dept_and_project_columns(web, cid, org):
    page = web.get("/recruit")
    assert f"name='부서_{cid}'" in page
    assert f"name='과제_{cid}'" in page
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


def test_copy_includes_headers(web):
    """머리글이 빠지면 엑셀에 붙였을 때 무슨 열인지 알 수 없다."""
    page = web.get("/")
    assert "twithhead" in page                     # 머리글 포함 토글
    assert "if(withHead !== false) out.push(" in page


# --- 명칭 관리: 원래 표기를 감추지 않는다 ---------------------------------------
def test_names_page_shows_the_original_spelling(web, names):
    page = web.get("/names?kind=" + urllib.parse.quote("소속"))
    assert "<th>원래 표기</th>" in page and "<th>매칭 키</th>" in page
    for 원문 in ("포항공과대학교", "포항공대", "서울대학교"):
        assert 원문 in page


def test_renaming_keeps_the_original_visible(web, names):
    nid = names["포항공과대학교"]
    web.post("/names/save", kind="소속", id=nid, **{f"표시명_{nid}": "포항공대"})
    page = web.get("/names?kind=" + urllib.parse.quote("소속"))
    assert "포항공과대학교" in page          # 고쳐도 원래 표기가 남아 있다
    assert "이름 겹침" in page               # 같은 이름을 쓰는 줄이 있다고 알려준다


def test_overlapping_names_are_reported_not_merged(web, names):
    fields = {"kind": "소속", "id": list(names.values())}
    for nid in names.values():
        fields[f"표시명_{nid}"] = "포항공대"
    code, body = web.post("/names/save", **fields)
    assert "같은 이름을 쓰는 항목" in body
    assert len(web.module.registry.list_all("소속")) == 3      # 합치지 않는다


def test_merge_ui_is_gone(web, names):
    page = web.get("/names?kind=" + urllib.parse.quote("소속"))
    assert "mergeform" not in page
    assert "여기로 묶기" not in page
