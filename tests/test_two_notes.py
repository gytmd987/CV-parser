"""비고는 둘이다 — 지원자 «비고» 와 채용 «채용_비고»."""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

import pytest

from cvtool.recruit import RECRUIT_COLUMNS, RecruitStore
from cvtool.schemas import COLUMNS, CVRecord


# --- 나뉘어 있나 -------------------------------------------------------------
def test_지원자_비고는_지원자_열이다():
    assert "비고" in COLUMNS
    assert "비고" in CVRecord.model_fields
    assert CVRecord(지원자_ID="T", 비고="메모").to_row()["비고"] == "메모"


def test_채용_비고는_채용_열이다():
    assert "채용_비고" in RECRUIT_COLUMNS
    assert "비고" not in RECRUIT_COLUMNS


def test_LLM_은_비고를_못_채운다():
    """추출 스키마에 없어야 사람이 적은 메모를 덮어쓸 수 없다."""
    from cvtool import schemas

    for 이름 in dir(schemas):
        if 이름.startswith("SECTION_"):
            assert "비고" not in json.dumps(getattr(schemas, 이름), ensure_ascii=False)


def test_채용_비고는_채용_DB_에_남는다(tmp_path):
    rc = RecruitStore(tmp_path / "r.db")
    rc.set_note("A", "2차 면접 조율중", "hr1")
    assert rc.get("A").채용_비고 == "2차 면접 조율중"
    assert not hasattr(rc.get("A"), "비고")


# --- 이관 --------------------------------------------------------------------
def test_쓰던_채용_현황_열_설정이_옮겨진다(tmp_path):
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.executescript(
        "CREATE TABLE view_columns (열이름 TEXT PRIMARY KEY,"
        " 순서 INTEGER DEFAULT 99, 표시 INTEGER DEFAULT 0);"
        "INSERT INTO view_columns VALUES ('최종상태',0,1),('비고',1,1);"
    )
    con.commit()
    con.close()

    assert RecruitStore(p).columns() == ["최종상태", "채용_비고"]
    # 두 번째부터는 안 건드린다 — 그 사이에 올린 지원자 '비고' 를 끌어가면 안 된다
    RecruitStore(p).set_columns(["최종상태", "채용_비고", "비고"])
    assert RecruitStore(p).columns() == ["최종상태", "채용_비고", "비고"]


def test_쓰던_열_설정이_옮겨진다(tmp_path):
    from cvtool.store import CandidateStore

    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.executescript(
        "CREATE TABLE column_config (열이름 TEXT PRIMARY KEY, 표시이름 TEXT DEFAULT '',"
        " 숨김 INTEGER DEFAULT 0, 순서 INTEGER DEFAULT 0);"
        "INSERT INTO column_config VALUES ('비고','메모',0,5);"
    )
    con.commit()
    con.close()

    cfg = CandidateStore(p).column_config()
    assert "비고" not in cfg
    assert cfg["채용_비고"]["표시이름"] == "메모"      # 정해 둔 이름·순서는 그대로
    assert cfg["채용_비고"]["순서"] == 5

    CandidateStore(p).set_column("비고", 표시이름="지원자 메모")
    다시 = CandidateStore(p).column_config()
    assert 다시["비고"]["표시이름"] == "지원자 메모"    # 두 번째엔 안 끌어간다
    assert 다시["채용_비고"]["표시이름"] == "메모"


def _옛대시보드(path, 설정: dict) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE dashboards (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " 이름 TEXT NOT NULL UNIQUE, 설명 TEXT DEFAULT '', 만든이 TEXT DEFAULT '',"
        " 만든일시 TEXT DEFAULT '', 수정일시 TEXT DEFAULT '');"
        "CREATE TABLE blocks (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " dashboard_id INTEGER NOT NULL, 순서 INTEGER DEFAULT 0, 종류 TEXT NOT NULL,"
        " 제목 TEXT DEFAULT '', 설정_json TEXT DEFAULT '{}');"
    )
    con.execute(
        "INSERT INTO blocks (dashboard_id,종류,제목,설정_json) VALUES (1,'목록',?,?)",
        ("비고 현황", json.dumps(설정, ensure_ascii=False)),
    )
    con.commit()
    con.close()


def _블록(path) -> tuple[str, dict]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT 제목, 설정_json FROM blocks").fetchone()
    con.close()
    return r["제목"], json.loads(r["설정_json"])


def test_대시보드_수식_안의_비고가_옮겨진다(tmp_path):
    from cvtool.dashboards import DashboardStore

    p = tmp_path / "d.db"
    _옛대시보드(p, {
        "수식": '=COUNT(지원자, 비고<>"")',
        "칸수식": "=비고",
        "목록조건": '=비고<>""',
        "칸": {"a\tb": "=비고"},
        "목록열": [["비고", "=비고", "120"], ["이름", "=한글_이름", ""]],
    })
    DashboardStore(p)
    제목, 설정 = _블록(p)
    assert 설정["수식"] == '=COUNT(지원자, 채용_비고<>"")'
    assert 설정["칸수식"] == "=채용_비고"
    assert 설정["목록조건"] == '=채용_비고<>""'
    assert 설정["칸"]["a\tb"] == "=채용_비고"
    assert 설정["목록열"][0][1] == "=채용_비고"


def test_사람이_쓴_글은_안_건드린다(tmp_path):
    from cvtool.dashboards import DashboardStore

    p = tmp_path / "d.db"
    _옛대시보드(p, {
        "글": "비고를 꼭 적어 주세요",
        "행": ["비고"],
        "목록열": [["비고", "=한글_이름", ""]],
    })
    DashboardStore(p)
    제목, 설정 = _블록(p)
    assert 제목 == "비고 현황"                 # 제목
    assert 설정["글"] == "비고를 꼭 적어 주세요"   # 글
    assert 설정["행"] == ["비고"]              # 축 이름표
    assert 설정["목록열"][0][0] == "비고"       # 머리글


def test_대시보드_이관은_한_번만_돈다(tmp_path):
    from cvtool.dashboards import DashboardStore

    p = tmp_path / "d.db"
    _옛대시보드(p, {"수식": "=비고"})
    DashboardStore(p)
    assert _블록(p)[1]["수식"] == "=채용_비고"

    con = sqlite3.connect(p)            # 사람이 새로 지원자 비고 수식을 썼다
    con.execute("UPDATE blocks SET 설정_json=?", (json.dumps({"수식": "=비고"}),))
    con.commit()
    con.close()
    DashboardStore(p)
    assert _블록(p)[1]["수식"] == "=비고"       # 두 번째엔 안 끌어간다


def test_비고를_안_쓰는_수식은_그대로다(tmp_path):
    from cvtool.dashboards import DashboardStore

    p = tmp_path / "d.db"
    _옛대시보드(p, {"수식": "=COUNT(지원자, 채용_비고<>\"\")", "칸수식": "=중복_메모"})
    DashboardStore(p)
    설정 = _블록(p)[1]
    assert 설정["수식"] == '=COUNT(지원자, 채용_비고<>"")'   # 이미 새 이름
    assert 설정["칸수식"] == "=중복_메모"                  # 낱말 경계를 지킨다


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


def test_인재_Pool_표에서_비고를_고친다(web, cid):
    코드, _ = web.post("/api/cell", id=cid, 항목="비고", 새값="첫 줄\n둘째 줄", 이전값="")
    assert 코드 == 200
    assert web.module.store.get(cid).비고 == "첫 줄\n둘째 줄"


def test_상세_화면에서도_비고를_고친다(web, cid):
    web.post("/candidate/save", id=cid, 끝="1", 항목_1="비고", 이전_1="", 값_1="상세에서")
    assert web.module.store.get(cid).비고 == "상세에서"


def test_상세_화면에_비고_칸이_있다(web, cid):
    쪽 = web.get("/candidate?id=" + urllib.parse.quote(cid))
    assert "채용 이야기는 채용 현황의 채용_비고 에" in 쪽


def test_채용을_안_시작한_사람도_비고를_적는다(web, cid):
    assert not web.module.recruit.get(cid).시작함
    web.post("/api/cell", id=cid, 항목="비고", 새값="풀에만 있는 사람", 이전값="")
    assert web.module.store.get(cid).비고 == "풀에만 있는 사람"


def test_둘은_서로_안_섞인다(web, cid):
    web.module.recruit.start(cid, "admin")
    web.post("/api/cell", id=cid, 항목="비고", 새값="사람 메모", 이전값="")
    web.post("/recruit/save", **{f"채용비고_{cid}": "채용 메모"})
    assert web.module.store.get(cid).비고 == "사람 메모"
    assert web.module.recruit.get(cid).채용_비고 == "채용 메모"
    # 하나를 고쳐도 다른 하나는 그대로
    web.post("/recruit/save", **{f"채용비고_{cid}": "채용 메모 2"})
    assert web.module.store.get(cid).비고 == "사람 메모"


def test_채용_비고는_인재_Pool_에서_보기_전용이다(web, cid):
    쪽 = web.get("/")
    assert "data-col='채용_비고'" not in 쪽      # 편집칸이 아니다
    assert "채용_비고" in 쪽                     # 열은 보인다
    코드, _ = web.post("/api/cell", id=cid, 항목="채용_비고", 새값="x", 이전값="")
    assert 코드 == 400                          # 지원자 레코드에 없는 항목


def test_채용_현황_표에_채용_비고_칸이_있다(web, cid):
    web.module.recruit.start(cid, "admin")
    쪽 = web.get("/recruit")
    assert f"채용비고_{cid}" in 쪽
    assert f"name='비고_{cid}'" not in 쪽


def test_변경_이력에_어느_쪽인지_남는다(web, cid):
    web.module.recruit.start(cid, "admin")
    web.post("/recruit/save", **{f"채용비고_{cid}": "이력용"})
    이력 = web.module.audit.for_candidate(cid)
    assert any(e.항목 == "채용_비고" and e.새값 == "이력용" for e in 이력)


def test_엑셀_양식에_비고가_들어간다(web):
    from cvtool import bulk

    assert "비고" in bulk.양식열(web.module.store)
    assert "채용_비고" not in bulk.양식열(web.module.store)


def test_수식에_둘_다_쓸_수_있다(web):
    이름 = web.module.대시보드_열()
    assert "비고" in 이름 and "채용_비고" in 이름


def test_탭은_메일이_대시보드보다_앞이다(web):
    쪽 = web.get("/recruit")
    머리 = 쪽[쪽.index("<header"):쪽.index("</header>")]
    assert 머리.index(">메일") < 머리.index(">대시보드<")
