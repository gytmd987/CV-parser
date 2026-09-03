"""복사·엑셀에서 줄바꿈이 살아남는다."""

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

from cvtool.xlsx_read import read_sheet


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

        def post_raw(self, path: str, **fields):
            body = urllib.parse.urlencode(fields, doseq=True, encoding="utf-8").encode()
            req = urllib.request.Request(self.base + path, data=body)
            with opener.open(req, timeout=20) as r:
                return r.read()

        def get(self, path: str) -> str:
            with opener.open(self.base + path, timeout=20) as r:
                return r.read().decode("utf-8", "replace")

    c = Client()
    c.post_raw("/login", userid="admin", password="pw1234")
    yield c
    server.shutdown()


# --- TSV 가르기 ---------------------------------------------------------------
def test_감싼_칸_안의_줄바꿈은_한_칸으로_남는다(web):
    가르기 = web.module._tsv_줄들
    assert 가르기('A\tB\n1\t"가\n나"') == [["A", "B"], ["1", "가\n나"]]


def test_따옴표_두_개는_따옴표_한_개다(web):
    가르기 = web.module._tsv_줄들
    assert 가르기('A\n"따옴표 ""안"" 있음"') == [["A"], ['따옴표 "안" 있음']]


def test_감싼_칸_안의_탭도_한_칸이다(web):
    가르기 = web.module._tsv_줄들
    assert 가르기('A\tB\n1\t"가\t나"') == [["A", "B"], ["1", "가\t나"]]


def test_감싸지_않은_평범한_TSV_는_그대로(web):
    가르기 = web.module._tsv_줄들
    assert 가르기("A\tB\n1\t2\n3\t4") == [["A", "B"], ["1", "2"], ["3", "4"]]


def test_빈_줄은_버린다(web):
    가르기 = web.module._tsv_줄들
    assert 가르기("A\tB\n\n1\t2\n\n") == [["A", "B"], ["1", "2"]]


def test_칸_가운데_따옴표는_글자로_본다(web):
    """`3"모니터` 같은 값이 감싼 것으로 오해받으면 안 된다."""
    가르기 = web.module._tsv_줄들
    assert 가르기('A\n3"모니터') == [["A"], ['3"모니터']]


# --- 엑셀 --------------------------------------------------------------------
def test_엑셀로_받으면_줄바꿈이_살아_있다(web):
    tsv = 'No.\t비고\n1\t"추천으로 들어온 사람\n연락처는 김부장님"\n2\t평범'
    data = web.post_raw("/table.xlsx", name="목록", tsv=tsv)
    표 = read_sheet(data)
    assert 표[0] == ["No.", "비고"]
    assert 표[1] == ["1", "추천으로 들어온 사람\n연락처는 김부장님"]
    assert 표[2] == ["2", "평범"]


def test_줄바꿈이_든_칸은_엑셀에서_줄이_보인다(web):
    """wrapText 가 없으면 값에는 있는데 화면에는 한 줄로 붙어 나온다."""
    import io as _io
    import zipfile

    data = web.post_raw("/table.xlsx", name="목록", tsv='A\n"가\n나"')
    sheet = zipfile.ZipFile(_io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode()
    assert 'r="A2" s="3"' in sheet


def test_평범한_표는_예전과_같다(web):
    data = web.post_raw("/table.xlsx", name="표", tsv="가\t나\n1\t2")
    assert read_sheet(data) == [["가", "나"], ["1", "2"]]


# --- 화면 쪽 ------------------------------------------------------------------
def test_복사는_줄바꿈을_살리는_함수를_쓴다(web):
    """`cellText` 는 거르기·정렬용이라 한 줄로 접는다. 복사는 그걸 쓰면 안 된다."""
    쪽 = web.get("/dash")
    assert "function cellRaw(td)" in 쪽
    assert "function tsvField(v)" in 쪽
    # 복사·엑셀 세 길이 모두 cellRaw 를 탄다
    assert 쪽.count("tsvField(cellRaw(") >= 3
    # 여러 줄 칸은 <br> 로 그려져서 textContent 로는 줄이 안 잡힌다 — title 을 본다
    assert "td.classList.contains('multi') && td.title" in 쪽
