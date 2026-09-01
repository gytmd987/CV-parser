"""줄바꿈 — 관리자가 켠 열에서만 여러 줄을 넣을 수 있다."""

from __future__ import annotations

import importlib
import os
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

import pytest

from cvtool import normalize as N
from cvtool.edit import ValidationError, custom_field_spec, field_spec, validate, validate_custom
from cvtool.export import build_xlsx
from cvtool.store import CandidateStore
from cvtool.xlsx_read import read_sheet


# --- 다듬기 ------------------------------------------------------------------
def test_줄은_살리고_줄_안은_text_와_같은_규칙():
    assert N.paragraph("a\tb\nc  d") == "a b\nc d"
    assert N.paragraph("a\r\nb\r\nc") == "a\nb\nc"


def test_한_줄이면_text_와_결과가_같다():
    for v in ("  한  줄  ", "a\tb", "", None):
        assert N.paragraph(v) == N.text(v)


def test_앞뒤_빈_줄은_버리고_이어진_빈_줄은_하나로():
    assert N.paragraph("\n\na\n\n\n\n\nb\n\n") == "a\n\nb"


# --- 어느 열에 켤 수 있나 -------------------------------------------------------
@pytest.fixture
def store(tmp_path):
    return CandidateStore(tmp_path / "c.db")


def test_경력_요약과_비고는_처음부터_켜져_있다(store):
    assert store.긴글열() == {"경력_요약", "비고"}


def test_켜고_끌_수_있다(store):
    store.set_column("연구분야_키워드", 긴글=True)
    store.set_column("경력_요약", 긴글=False)
    assert store.긴글열() == {"연구분야_키워드", "비고"}


def test_다른_설정을_고쳐도_긴글은_그대로다(store):
    store.set_column("경력_요약", 표시이름="경력")
    assert "경력_요약" in store.긴글열()
    store.set_column("연구분야_키워드", 긴글=True)
    store.set_column("연구분야_키워드", 숨김=True)
    assert "연구분야_키워드" in store.긴글열()


def test_쓰던_DB_에_긴글_열이_붙는다(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE column_config (열이름 TEXT PRIMARY KEY,"
        " 표시이름 TEXT DEFAULT '', 숨김 INTEGER DEFAULT 0, 순서 INTEGER DEFAULT 0)"
    )
    con.execute("INSERT INTO column_config (열이름, 표시이름) VALUES ('경력_요약','요약')")
    con.commit()
    con.close()

    s = CandidateStore(path)
    assert s.column_config()["경력_요약"]["표시이름"] == "요약"
    s.set_column("경력_요약", 긴글=True)
    assert "경력_요약" in s.긴글열()


# --- 검사 --------------------------------------------------------------------
def test_켠_열만_줄바꿈이_살아남는다():
    assert validate("경력_요약", "a\nb") == "a b"              # 안 켜면 지금 그대로
    assert validate("경력_요약", "a\nb", 긴글=True) == "a\nb"


def test_추가한_열도_마찬가지():
    정의 = {"이름": "메모", "유형": "텍스트"}
    assert validate_custom(정의, "a\nb") == "a b"
    assert validate_custom(정의, "a\nb", 긴글=True) == "a\nb"


def test_형식이_정해진_열에는_줄바꿈이_안_들어간다():
    """켜 달라고 해도 형식 검사가 먼저다. 저장된 값에 줄바꿈이 남으면 안 된다."""
    assert validate("생년월일", "19900101", 긴글=True) == "19900101"
    assert "\n" not in validate("생년월일", "1990\n0101", 긴글=True)
    assert "\n" not in validate_custom({"이름": "연월", "유형": "연월"},
                                      "2024\n03", 긴글=True)
    assert "\n" not in validate("이메일", "a@x.com\nb@x.com", 긴글=True)
    with pytest.raises(ValidationError):
        validate("현재_신분", "포닥\n박사", 긴글=True)      # 선택지 밖


def test_입력칸_모양():
    assert field_spec("경력_요약", 긴글=True).입력 == "긴글"
    assert field_spec("경력_요약").입력 == "text"
    # 형식이 정해진 열은 켜 달라 해도 그 형식이 이긴다
    assert field_spec("생년월일", 긴글=True).입력 == "yyyymmdd"
    assert field_spec("현재_신분", 긴글=True).입력 == "select"
    assert custom_field_spec({"이름": "메모", "유형": "텍스트"}, True).입력 == "긴글"
    assert custom_field_spec({"이름": "수", "유형": "숫자"}, True).입력 == "number"


# --- 엑셀 --------------------------------------------------------------------
def test_엑셀에_넣었다_읽으면_줄이_그대로다():
    data = build_xlsx([{"경력_요약": "첫 줄\n둘째 줄"}], ["경력_요약"])
    assert read_sheet(data)[1][0] == "첫 줄\n둘째 줄"


def test_줄바꿈이_든_칸에만_wrapText_가_붙는다():
    import io as _io
    import zipfile

    data = build_xlsx([{"a": "여러\n줄", "b": "한 줄"}], ["a", "b"])
    sheet = zipfile.ZipFile(_io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode()
    assert 'r="A2" s="3"' in sheet          # 줄바꿈이 있는 칸
    assert 'r="B2" s="3"' not in sheet      # 없는 칸은 그대로


# --- 화면 --------------------------------------------------------------------
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
def cid(web):
    before = {r.지원자_ID for r in web.module.store.list_all()}
    web.post("/candidate/new")
    return ({r.지원자_ID for r in web.module.store.list_all()} - before).pop()


def test_표에서_고치면_줄바꿈이_저장된다(web, cid):
    코드, 본문 = web.post("/api/cell", id=cid, 항목="경력_요약",
                        새값="첫 줄\n둘째 줄", 이전값="")
    assert 코드 == 200
    assert web.module.store.get(cid).경력_요약 == "첫 줄\n둘째 줄"


def test_안_켠_열은_지금처럼_뭉개진다(web, cid):
    web.post("/api/cell", id=cid, 항목="현재_소속_상세", 새값="가\n나", 이전값="")
    assert web.module.store.get(cid).현재_소속_상세 == "가 나"


def test_상세_화면에서_저장해도_줄이_산다(web, cid):
    web.post("/candidate/save", id=cid, 끝="1",
             항목_1="경력_요약", 이전_1="", 값_1="가\n나\n다")
    assert web.module.store.get(cid).경력_요약 == "가\n나\n다"


def test_표_항목에서_켜면_그_열도_여러_줄이_된다(web, cid):
    열목록 = [c for _g, c, _a in web.module.열목록()]
    i = 열목록.index("현재_소속_상세") + 1
    보냄 = {f"col_{i}": "현재_소속_상세", f"order_{i}": str(i), f"long_{i}": "on"}
    web.post("/fields/columns", **보냄)
    assert "현재_소속_상세" in web.module.store.긴글열()
    web.post("/api/cell", id=cid, 항목="현재_소속_상세", 새값="가\n나", 이전값="")
    assert web.module.store.get(cid).현재_소속_상세 == "가\n나"


def test_형식이_정해진_열에는_체크박스가_없다(web):
    쪽 = web.get("/fields")
    자리 = 쪽[쪽.index("data-col='생년월일'"):][:1400]
    assert "name='long_" not in 자리


def test_표에_여러_줄_칸이_textarea_로_뜬다(web, cid):
    web.post("/api/cell", id=cid, 항목="경력_요약", 새값="가\n나", 이전값="")
    쪽 = web.get("/")
    자리 = 쪽[쪽.index("data-col='경력_요약'"):][:400]
    assert "data-kind='긴글'" in 자리
    assert "&#10;" in 자리            # 속성 안 줄바꿈은 글자 참조로 담는다
