"""대시보드 표 너비 — 표 폭 = 열 폭의 합."""

from __future__ import annotations

import importlib
import os
import threading
import urllib.error
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
    mod = importlib.reload(importlib.import_module("cvtool.web.app"))
    mod.bootstrap_admin()
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()))

    class Client:
        module = mod
        base = f"http://127.0.0.1:{port}"

        def post(self, path: str, **fields):
            body = urllib.parse.urlencode(fields, doseq=True, encoding="utf-8").encode()
            req = urllib.request.Request(self.base + path, data=body)
            try:
                with opener.open(req, timeout=20) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")

        def get(self, path: str) -> str:
            with opener.open(self.base + path, timeout=20) as r:
                return r.read().decode("utf-8", "replace")

    c = Client()
    c.post("/login", userid="admin", password="pw1234")
    c.post("/candidate/new")
    # 프로필 카드는 채울 값이 하나라도 있어야 나온다
    cid = mod.store.list_all()[0].지원자_ID
    c.post("/api/cell", id=cid, 항목="한글_이름", 새값="홍길동", 이전값="")
    yield c
    server.shutdown()


# --- px 읽기 ------------------------------------------------------------------
def test_쓸_수_있는_값만_너비로_친다(web):
    _px = web.module._px
    assert _px("120") == 120 and _px(120) == 120 and _px("120.0") == 120
    for 못쓰는값 in ("", None, "0", 0, "-5", "자동", "abc"):
        assert _px(못쓰는값) == 0


def test_하나라도_비면_합으로_안_세운다(web):
    합 = web.module._열너비합
    assert 합(["120", "80"]) == 200
    assert 합(["120", ""]) == 0
    assert 합(["120", "0"]) == 0
    assert 합([]) == 0


# --- 표 모양 ------------------------------------------------------------------
def _블록(web, 종류, 설정):
    from cvtool.dashboards import Block
    return Block(id=1, dashboard_id=1, 순서=0, 종류=종류, 제목="", 설정=설정)


def test_전부_정하면_표_폭이_합이_된다(web):
    b = _블록(web, "목록", {})
    모양 = web.module._표모양(b, ["120", "80", "200"])
    assert "fixed" in 모양 and "width:400px" in 모양


def test_하나라도_비면_지금_모양_그대로(web):
    b = _블록(web, "목록", {})
    모양 = web.module._표모양(b, ["120", "", "200"])
    assert "fixed" not in 모양 and "width:" not in 모양


def test_너비를_아예_안_정하면_그대로(web):
    b = _블록(web, "목록", {})
    assert "fixed" not in web.module._표모양(b, [])


# --- 화면 --------------------------------------------------------------------
def _표태그(보기: str) -> str:
    """보기 화면의 `<table ...>` 여는 태그. **CSS 에도 'fixed' 가 있어서**
    페이지 전체를 훑으면 늘 걸린다."""
    i = 보기.index("<table class='dtbl")
    return 보기[i:보기.index(">", i) + 1]


def _대시보드(web, 종류, 설정, 이름):
    did = web.module.boards.add(이름, "admin")
    web.module.boards.add_block(did, 종류, 제목="표", 설정=설정)
    return did


def test_목록_표_폭이_열_폭의_합이다(web):
    did = _대시보드(web, "목록", {
        "목록대상": "지원자",
        "목록열": [["가", "=한글_이름", "150"], ["나", "=이메일", "250"]],
    }, "목록합")
    태그 = _표태그(web.get(f"/dash/view?id={did}"))
    assert "fixed" in 태그 and "width:400px" in 태그


def test_한_열을_비우면_합으로_안_세운다(web):
    did = _대시보드(web, "목록", {
        "목록대상": "지원자",
        "목록열": [["가", "=한글_이름", "150"], ["나", "=이메일", ""]],
    }, "목록빈칸")
    보기 = web.get(f"/dash/view?id={did}")
    assert "fixed" not in _표태그(보기)
    assert "width:150px" in 보기            # 정한 열은 그대로 붙는다


def test_축표도_열_너비가_선다(web):
    did = _대시보드(web, "축표", {
        "행축": "직접 입력", "열축": "직접 입력",
        "행": ["가"], "열": ["A", "B"], "칸수식": "1",
        "열너비": {"": "100", "A": "120", "B": "180"},
    }, "축표너비")
    보기 = web.get(f"/dash/view?id={did}")
    assert "fixed" in _표태그(보기) and "width:400px" in _표태그(보기)
    for px in ("width:100px", "width:120px", "width:180px"):
        assert px in 보기


def test_자유표도_열_너비가_선다(web):
    did = _대시보드(web, "표", {
        "행": ["가"], "열": ["A", "B"], "칸": {"가\tA": "1", "가\tB": "2"},
        "열너비": {"": "90", "A": "110", "B": "130"},
    }, "자유표너비")
    태그 = _표태그(web.get(f"/dash/view?id={did}"))
    assert "fixed" in 태그 and "width:330px" in 태그


def test_열이_하나_늘어도_나머지_너비가_안_밀린다(web):
    """이름을 열쇠로 담는 까닭 — 자리 번호였으면 통째로 한 칸씩 밀린다."""
    설정 = {"행축": "직접 입력", "열축": "직접 입력", "행": ["가"],
           "열": ["A", "B"], "칸수식": "1",
           "열너비": {"": "100", "A": "120", "B": "180"}}
    did = _대시보드(web, "축표", 설정, "축표늘림")
    b = web.module.boards.blocks(did)[0]
    web.module.boards.save_block(b.id, 설정={**설정, "열": ["새열", "A", "B"]})
    보기 = web.get(f"/dash/view?id={did}")
    # 새 열은 너비가 없으니 합으로 안 세우지만, A·B 는 정한 그대로다
    assert "fixed" not in _표태그(보기)
    assert "width:120px" in 보기 and "width:180px" in 보기


def test_프로필_라벨_칸_너비(web):
    did = _대시보드(web, "프로필", {
        "대상": "=LIST(지원자, 열=지원자_ID)", "머리": "=한글_이름",
        "줄": [["이름", "=한글_이름"]], "열너비": {"": "140"},
    }, "프로필너비")
    # 값이 빈 줄은 카드에 안 나오므로 값이 있는 열로 시험한다
    assert "width:140px" in web.get(f"/dash/view?id={did}")


# --- 저장 --------------------------------------------------------------------
def test_열_너비를_저장했다_다시_열면_그대로(web):
    did = _대시보드(web, "축표", {
        "행축": "직접 입력", "열축": "직접 입력",
        "행": ["가"], "열": ["A", "B"], "칸수식": "1",
    }, "축표저장")
    b = web.module.boards.blocks(did)[0]
    web.post("/dash/block/save", id=b.id, rowaxis="직접 입력", colaxis="직접 입력",
             rows="가", cols="A, B", cellformula="1",
             colwname=["", "A", "B"], colw=["100", "120", "0"])
    저장된 = web.module.boards.blocks(did)[0].열너비
    assert 저장된 == {"": "100", "A": "120"}      # 0 은 «안 정함» 으로 떨어진다

    편집 = web.get(f"/dash/edit?id={did}")
    assert "name='colwname'" in 편집 and "name='colw'" in 편집
    assert "value='100'" in 편집 and "value='120'" in 편집


def test_편집_화면에_열_너비_칸이_있다(web):
    for 종류, 설정 in (
        ("축표", {"행축": "직접 입력", "열축": "직접 입력", "행": ["가"], "열": ["A"],
                "칸수식": "1"}),
        ("표", {"행": ["가"], "열": ["A"]}),
        ("프로필", {"대상": "=LIST(지원자, 열=지원자_ID)", "줄": [["a", "=한글_이름"]]}),
    ):
        did = _대시보드(web, 종류, 설정, f"칸있나{종류}")
        편집 = web.get(f"/dash/edit?id={did}")
        assert "name='colw'" in 편집, 종류
        assert "열 너비" in 편집, 종류


def test_합계를_보여준다(web):
    did = _대시보드(web, "목록", {
        "목록대상": "지원자", "목록열": [["가", "=한글_이름", "150"]],
    }, "합계보기")
    편집 = web.get(f"/dash/edit?id={did}")
    assert "class='wsum muted'" in 편집
    assert "function wsum(" in 편집


def test_넓은_표는_가로로_스크롤한다(web):
    """합이 화면보다 커도 된다 — min-width:100% 를 풀어야 그렇게 된다."""
    보기 = web.get("/dash")
    assert "table.dtbl.fixed{table-layout:fixed;min-width:0}" in 보기
    assert "table.dtbl.fixed th,table.dtbl.fixed td{max-width:none}" in 보기
