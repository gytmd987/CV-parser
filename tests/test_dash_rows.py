"""대시보드 줄 — 열 목록과 어긋나지 않기 · `=ROW()`."""

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

from cvtool import expr
from cvtool.dashboards import ROW_TARGET, Block, render_list, render_profile
from cvtool.formula import Rows


# --- ROW() -------------------------------------------------------------------
def _목록(열, **설정) -> Block:
    바탕 = {"목록대상": "지원자", "목록열": 열}
    바탕.update(설정)
    return Block(id=1, dashboard_id=1, 순서=0, 종류="목록", 제목="", 설정=바탕)


@pytest.fixture
def rows():
    사람 = [
        {"지원자_ID": "A", "한글_이름": "가나", "저널_수": "3"},
        {"지원자_ID": "B", "한글_이름": "다라", "저널_수": "1"},
        {"지원자_ID": "C", "한글_이름": "마바", "저널_수": "2"},
    ]
    return Rows(지원자=사람, 채용=사람)


def test_ROW_는_1부터_차례로(rows):
    r = render_list(_목록([["No.", "=ROW()", ""], ["이름", "=한글_이름", ""]]), rows)
    assert r.오류 == []
    assert [줄[0] for 줄 in r.행] == ["1", "2", "3"]


def test_ROW_는_정렬한_뒤의_차례다(rows):
    b = _목록([["No.", "=ROW()", ""], ["이름", "=한글_이름", ""]],
             목록정렬="=저널_수", 목록내림차순=True)
    r = render_list(b, rows)
    assert [줄[1] for 줄 in r.행] == ["가나", "마바", "다라"]   # 3, 2, 1편
    assert [줄[0] for 줄 in r.행] == ["1", "2", "3"]           # 번호는 다시 1부터


def test_ROW_는_자른_뒤에도_1부터(rows):
    b = _목록([["No.", "=ROW()", ""]], 목록최대=2)
    r = render_list(b, rows)
    assert [줄[0] for 줄 in r.행] == ["1", "2"]
    assert r.전체 == 3


def test_ROW_는_거른_뒤의_차례다(rows):
    b = _목록([["No.", "=ROW()", ""], ["이름", "=한글_이름", ""]],
             목록조건='=한글_이름<>"가나"')
    r = render_list(b, rows)
    assert [줄[0] for 줄 in r.행] == ["1", "2"]
    assert [줄[1] for 줄 in r.행] == ["다라", "마바"]


def test_ROW_를_다른_값과_이어_쓴다(rows):
    r = render_list(_목록([["", '=ROW() & ". " & 한글_이름', ""]]), rows)
    assert [줄[0] for 줄 in r.행] == ["1. 가나", "2. 다라", "3. 마바"]


def test_행_고르기에_ROW_를_쓰면_알아듣게_막는다(rows):
    r = render_list(_목록([["이름", "=한글_이름", ""]], 목록조건="=ROW()<=2"), rows)
    assert r.오류 and "목록 표의 열에서만" in r.오류[0]


def test_색칠_조건에서는_쓸_수_있다(rows):
    b = _목록([["이름", "=한글_이름", ""]],
             조건서식=[{"조건": "=ROW()=1", "대상": ROW_TARGET, "배경": "#ffeeee"}])
    r = render_list(b, rows)
    assert r.행색[0] and not r.행색[1]


def test_프로필에서도_센다():
    사람 = [{"지원자_ID": "A", "한글_이름": "가나"},
           {"지원자_ID": "B", "한글_이름": "다라"}]
    b = Block(id=1, dashboard_id=1, 순서=0, 종류="프로필", 제목="",
              설정={"대상": "=LIST(지원자, 열=지원자_ID)",
                   "머리": '=ROW() & ". " & 한글_이름', "줄": []})
    값 = {r["지원자_ID"]: r for r in 사람}
    r = render_profile(b, Rows(지원자=사람, 채용=사람), 값.get)
    assert [머리 for 머리, _줄 in r.사람] == ["1. 가나", "2. 다라"]


def test_ROW_는_열이_아니다():
    assert expr.columns("=ROW()") == []
    assert "ROW" in expr.FUNC_NAMES          # 자동완성에 뜨는 근거


# --- 열 목록과 줄이 어긋나지 않는다 -----------------------------------------------
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
    yield c
    server.shutdown()


@pytest.fixture
def 두사람(web):
    before = {r.지원자_ID for r in web.module.store.list_all()}
    web.post("/candidate/new")
    web.post("/candidate/new")
    새것 = sorted({r.지원자_ID for r in web.module.store.list_all()} - before)
    return 새것


def test_쓸_수_있다고_한_열은_모든_줄에_있다(web, 두사람):
    """열 목록과 줄이 어긋나면 «모르는 열» 이 '값이 없다' 는 뜻이 돼 버린다."""
    이름들 = web.module.대시보드_열()
    rows = web.module.대시보드_행()
    assert rows.지원자
    for 행 in rows.지원자:
        빠진것 = 이름들 - set(행)
        assert not 빠진것, f"줄에 없는 열: {sorted(빠진것)}"


def test_추가한_열에_값이_없어도_빈칸이다(web, 두사람):
    있는쪽, 없는쪽 = 두사람
    web.module.store.add_field("비고2", "텍스트", 구분="지원자 정보")
    web.module.store.set_custom(있는쪽, "비고2", "적어 둔 메모")

    rows = web.module.대시보드_행()
    b = _목록([["ID", "=지원자_ID", ""], ["비고2", "=비고2", ""]])
    r = render_list(b, rows, 아는열=web.module.대시보드_열())
    값 = {줄[0]: 줄[1] for 줄 in r.행}
    assert r.오류 == []                      # «모르는 열» 이 안 뜬다
    assert 값[있는쪽] == "적어 둔 메모"
    assert 값[없는쪽] == ""                   # `?` 가 아니라 빈칸


def test_한_사람짜리_길도_같다(web, 두사람):
    _있는쪽, 없는쪽 = 두사람
    값 = web.module._프로필값(없는쪽)
    for c in web.module.대시보드_열():
        assert c in 값, f"프로필 값에 없는 열: {c}"


def test_메일_발송이력도_줄에_담긴다(web, 두사람):
    rows = web.module.대시보드_행()
    assert all(web.module.MAIL_COLUMN in 행 for 행 in rows.지원자)
    r = render_list(_목록([["메일", f"=[{web.module.MAIL_COLUMN}]", ""]]), rows)
    assert r.오류 == []
    assert all(줄[0] == "" for 줄 in r.행)     # 아직 보낸 게 없다


def test_진짜_오타는_여전히_막힌다(web):
    with pytest.raises(expr.ExprError):
        expr.validate("=없는열이름", web.module.대시보드_열())
