"""메일 발송 조건 — 보내야 하는 때를 정해 두고, 안 보낸 것을 찾는다."""

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

from cvtool.mailing import MailStore
from cvtool.recruit import RecruitStore


# --- 조건 목록 ---------------------------------------------------------------
@pytest.fixture
def rs(tmp_path):
    return RecruitStore(tmp_path / "r.db")


def test_조건은_채용시작과_단계별_상태를_모두_낸다(rs):
    조건 = rs.발송조건들()
    assert 조건[0] == "채용 시작"
    for 말 in ("서류 검토 합격", "서류 검토 불합격", "전화 면접 보류",
             "기술 면접 진행중", "HR 면접 불합격"):
        assert 말 in 조건


def test_마지막_단계_합격은_최종합격으로_나온다(rs):
    조건 = rs.발송조건들()
    assert "최종 합격" in 조건
    assert "HR 면접 합격" not in 조건       # 최종상태가 그렇게 부르지 않는다


def test_조건은_최종상태와_한_글자도_어긋나지_않는다(rs, tmp_path):
    """조건 목록과 실제 상태가 갈라지면 아무도 안 걸리는 조건이 생긴다."""
    from cvtool.recruit import STAGES
    조건 = set(rs.발송조건들())
    for 단계 in STAGES:
        for 상태 in rs.statuses():
            if not 상태:
                continue
            rs2 = RecruitStore(tmp_path / f"x{abs(hash(단계 + 상태))}.db")
            rs2.start("C-1", "admin")
            rs2.set_stage("C-1", 단계, 상태, "admin")
            assert rs2.get("C-1").최종상태 in 조건


def test_묶음은_시작과_단계별로_나뉘고_편_것과_같다(rs):
    묶음 = rs.발송조건묶음()
    assert [이름 for 이름, _ in 묶음] == ["시작", "서류 검토", "전화 면접",
                                    "기술 면접", "HR 면접"]
    assert 묶음[0][1] == ["채용 시작"]
    assert [c for _, 것들 in 묶음 for c in 것들] == rs.발송조건들()


def test_관리자가_상태를_바꾸면_조건도_따라_바뀐다(rs):
    rs.set_statuses(["", "합격", "불합격", "검토중"])
    조건 = rs.발송조건들()
    assert "서류 검토 검토중" in 조건
    assert "서류 검토 보류" not in 조건


# --- 템플릿에 담기 -----------------------------------------------------------
@pytest.fixture
def ms(tmp_path):
    return MailStore(tmp_path / "m.db")


def test_조건은_여러개_담기고_리스트로_나온다(ms):
    tid = ms.add_template("불합격", 발송조건="서류 검토 불합격\n기술 면접 불합격")
    assert ms.template(tid).조건들 == ["서류 검토 불합격", "기술 면접 불합격"]


def test_조건을_안_정하면_빈_목록이다(ms):
    tid = ms.add_template("안내")
    assert ms.template(tid).조건들 == []


def test_조건은_수정된다(ms):
    tid = ms.add_template("안내", 발송조건="채용 시작")
    ms.update_template(tid, 발송조건="최종 합격")
    assert ms.template(tid).조건들 == ["최종 합격"]


def test_다른_것을_고쳐도_조건은_그대로다(ms):
    tid = ms.add_template("안내", 발송조건="채용 시작")
    ms.update_template(tid, 제목="바뀐 제목")
    assert ms.template(tid).조건들 == ["채용 시작"]


def test_쓰던_DB_에_발송조건_열이_붙는다(tmp_path):
    """예전 판으로 쓰던 DB 를 그대로 이어 쓸 수 있어야 한다."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE templates (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " 이름 TEXT NOT NULL UNIQUE, 제목 TEXT DEFAULT '', 본문 TEXT DEFAULT '',"
        " 탈락메일 INTEGER DEFAULT 0, 만든이 TEXT DEFAULT '',"
        " 만든일시 TEXT DEFAULT '', 수정일시 TEXT DEFAULT '')"
    )
    con.execute("INSERT INTO templates (이름) VALUES ('옛 템플릿')")
    con.commit()
    con.close()

    ms = MailStore(path)
    t = ms.template_by_name("옛 템플릿")
    assert t is not None and t.조건들 == []
    ms.update_template(t.id, 발송조건="최종 합격")
    assert ms.template(t.id).조건들 == ["최종 합격"]


def test_이력은_템플릿별로_거를_수_있다(ms):
    a = ms.template(ms.add_template("A"))
    b = ms.template(ms.add_template("B"))
    ms.record("CV-1", a, "a@x.com", "제", "본", "성공")
    ms.record("CV-2", b, "b@x.com", "제", "본", "성공")
    assert [r["지원자_ID"] for r in ms.history(template_id=a.id)] == ["CV-1"]
    assert ms.count(a.id) == 1 and ms.count() == 2


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
        urllib.request.HTTPCookieProcessor(CookieJar())
    )

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
def 지원자(web):
    before = {r.지원자_ID for r in web.module.store.list_all()}
    web.post("/candidate/new")
    cid = ({r.지원자_ID for r in web.module.store.list_all()} - before).pop()
    web.module.recruit.start(cid, "admin")
    return cid


@pytest.fixture
def 템플릿(web, request):
    이름 = "조건" + request.node.name[-24:]
    tid = web.module.mailing.add_template(이름, "제목", "본문", 만든이="admin")
    return web.module.mailing.template(tid)


def _조건(web, tpl, *조건들):
    web.module.mailing.update_template(tpl.id, 발송조건="\n".join(조건들))
    return web.module.mailing.template(tpl.id)


def test_상태를_바꾸면_안보낸_목록에_나타난다(web, 지원자, 템플릿):
    tpl = _조건(web, 템플릿, "서류 검토 불합격")
    assert 지원자 not in web.module._안보낸것(tpl)
    web.module.recruit.set_stage(지원자, "서류 검토", "불합격", "admin")
    assert web.module._안보낸것(tpl)[지원자] == "서류 검토 불합격"


def test_보내면_안보낸_목록에서_사라진다(web, 지원자, 템플릿):
    tpl = _조건(web, 템플릿, "서류 검토 합격")
    web.module.recruit.set_stage(지원자, "서류 검토", "합격", "admin")
    assert 지원자 in web.module._안보낸것(tpl)
    web.module.mailing.record(지원자, tpl, "a@b.com", "제", "본", "성공")
    assert 지원자 not in web.module._안보낸것(tpl)


def test_탈락메일을_받은_사람은_안_센다(web, 지원자, 템플릿):
    탈락 = web.module.mailing.template(
        web.module.mailing.add_template("탈락" + 지원자[-6:], 탈락메일=True))
    web.module.mailing.record(지원자, 탈락, "a@b.com", "제", "본", "성공")
    tpl = _조건(web, 템플릿, "서류 검토 합격")
    web.module.recruit.set_stage(지원자, "서류 검토", "합격", "admin")
    assert 지원자 not in web.module._안보낸것(tpl)


def test_채용을_시작_안_한_사람은_안_센다(web, 템플릿):
    before = {r.지원자_ID for r in web.module.store.list_all()}
    web.post("/candidate/new")
    cid = ({r.지원자_ID for r in web.module.store.list_all()} - before).pop()
    tpl = _조건(web, 템플릿, "채용 시작")
    assert cid not in web.module._안보낸것(tpl)


def test_조건을_안_고른_템플릿은_배지에_안_센다(web, 지원자, 템플릿):
    web.module.recruit.set_stage(지원자, "서류 검토", "불합격", "admin")
    me = web.module.auth.get_user("admin")
    assert 템플릿.id not in web.module._안보낸수(me)


def test_상태를_되돌리면_목록에서_빠진다(web, 지원자, 템플릿):
    tpl = _조건(web, 템플릿, "서류 검토 불합격")
    web.module.recruit.set_stage(지원자, "서류 검토", "불합격", "admin")
    assert 지원자 in web.module._안보낸것(tpl)
    web.module.recruit.set_stage(지원자, "서류 검토", "진행중", "admin")
    assert 지원자 not in web.module._안보낸것(tpl)


def test_없는_조건은_저장할_때_걸러진다(web, 템플릿):
    web.post("/mail/template/save", id=템플릿.id, name=템플릿.이름,
             subject="제", body="본", when=["최종 합격", "아무 데도 없는 상태"])
    assert web.module.mailing.template(템플릿.id).조건들 == ["최종 합격"]


def test_편집_화면에_조건_체크박스가_있다(web, 템플릿):
    _조건(web, 템플릿, "최종 합격")
    쪽 = web.get(f"/mail/template?id={템플릿.id}")
    assert "보내야 하는 때" in 쪽
    assert "name='when' value='서류 검토 불합격'" in 쪽
    assert "value='최종 합격' checked" in 쪽


def test_발송이력은_템플릿별로_나뉜다(web, 지원자, 템플릿):
    tpl = _조건(web, 템플릿, "기술 면접 합격")
    web.module.recruit.set_stage(지원자, "기술 면접", "합격", "admin")
    쪽 = web.get(f"/mail/log?tpl={tpl.id}")
    assert "안 보낸 사람" in 쪽
    assert f"value='{지원자}'" in 쪽
    assert "/mail/compose" in 쪽


def test_조건이_없는_템플릿의_이력은_정하라고_안내한다(web, 템플릿):
    쪽 = web.get(f"/mail/log?tpl={템플릿.id}")
    assert "보내야 하는 때가 안 정해져 있습니다" in 쪽


def test_이력_주소가_이상해도_전체가_나온다(web):
    assert "보낸 기록" in web.get("/mail/log?tpl=abc")
    assert "보낸 기록" in web.get("/mail/log?tpl=99999")


def test_탭에_하위_목록이_붙는다(web):
    쪽 = web.get("/mail")
    assert "메일 템플릿 관리" in 쪽 and "메일 발송이력" in 쪽
    assert "부서·과제 편집" in 쪽 and "과제 정보 관리" in 쪽
    assert "class='tab'" in 쪽


def test_안_보낸_것이_있으면_메일_탭에_숫자가_붙는다(web, 지원자, 템플릿):
    _조건(web, 템플릿, "전화 면접 불합격")
    web.module.recruit.set_stage(지원자, "전화 면접", "불합격", "admin")
    쪽 = web.get("/recruit")
    assert "메일 <span class=\"pill p-안본것\">" in 쪽 or \
           '메일 <span class="pill p-안본것">' in 쪽


def test_현업은_배정_안_된_지원자를_세지_않는다(web, 지원자, 템플릿):
    tpl = _조건(web, 템플릿, "HR 면접 불합격")
    web.module.recruit.set_stage(지원자, "HR 면접", "불합격", "admin")
    web.post("/users/add", userid="hyunup", name="현업", password="pw1234",
             role="현업")
    현업 = web.module.auth.get_user("hyunup")
    assert web.module._안보낸것(tpl)[지원자] == "HR 면접 불합격"
    assert 지원자 not in web.module._안보낸것(tpl, 현업)
