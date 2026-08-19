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
            body = urllib.parse.urlencode(fields, encoding="utf-8").encode()
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


def test_edit_cell_registers_name_in_registry(web, cid):
    """표에서 손으로 넣은 학교도 명칭 사전에 올라와야 나중에 묶을 수 있다."""
    web.cell(id=cid, 항목="박사_학교", 새값="포항공과대학교(POSTECH)", 이전값="")
    found = web.module.registry.lookup("학교", "포항공과대학교")
    assert found is not None and found.표시명 == "포항공과대학교(POSTECH)"


def test_edited_cell_shows_registry_display_name(web, cid):
    """표시명을 바꾸면 표에는 대표명이, 편집칸에는 원문이 남는다."""
    web.cell(id=cid, 항목="석사_학교", 새값="한국과학기술원", 이전값="")
    found = web.module.registry.lookup("학교", "한국과학기술원")
    web.module.registry.classify(found.id, 표시명="KAIST")
    row = web.module.store.get(cid).to_row(web.module.registry)
    assert row["석사_학교"] == "KAIST"
    assert web.module.store.get(cid).석사_학교 == "한국과학기술원"


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


def test_recruit_table_cells_are_editable(web, cid):
    page = web.get("/recruit")
    assert "data-col=" in page and "/api/cell" in page
