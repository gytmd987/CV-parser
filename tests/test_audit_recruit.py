"""채용 단계를 바꾼 것도 그 지원자 이력에 보여야 한다."""

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

    class Client:
        module = mod
        base = f"http://127.0.0.1:{port}"

        def __init__(self):
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(CookieJar()))

        def post(self, path: str, **fields):
            body = urllib.parse.urlencode(fields, doseq=True, encoding="utf-8").encode()
            req = urllib.request.Request(self.base + path, data=body)
            try:
                with self._opener.open(req, timeout=20) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")

        def get(self, path: str) -> str:
            with self._opener.open(self.base + path, timeout=20) as r:
                return r.read().decode("utf-8", "replace")

    c = Client()
    c.post("/login", userid="admin", password="pw1234")
    yield c
    server.shutdown()


@pytest.fixture
def cid(web):
    before = {r.지원자_ID for r in web.module.store.list_all()}
    web.post("/candidate/new")
    cid = ({r.지원자_ID for r in web.module.store.list_all()} - before).pop()
    web.module.recruit.start(cid, "admin")
    return cid


def test_단계_변경이_지원자_이력에_남는다(web, cid):
    web.post("/recruit/save", **{f"단계_{cid}_서류 검토": "합격"})
    이력 = web.module.audit.for_candidate(cid)
    걸린것 = [e for e in 이력 if e.대상종류 == "채용현황" and e.항목 == "서류 검토"]
    assert 걸린것 and 걸린것[0].새값 == "합격"


def test_상세_화면_이력에_단계가_보인다(web, cid):
    web.post("/recruit/save", **{f"단계_{cid}_전화 면접": "불합격"})
    쪽 = web.get("/candidate?id=" + urllib.parse.quote(cid))
    이력자리 = 쪽[쪽.index("변경 이력"):]
    assert "전화 면접: (빈칸) → 불합격" in 이력자리


def test_지원자_정보_수정도_같이_보인다(web, cid):
    web.post("/api/cell", id=cid, 항목="한글_이름", 새값="홍길동", 이전값="")
    web.post("/recruit/save", **{f"단계_{cid}_서류 검토": "보류"})
    종류들 = {e.대상종류 for e in web.module.audit.for_candidate(cid)}
    assert 종류들 == {"지원자", "채용현황"}


def test_다른_지원자_이력은_안_섞인다(web, cid):
    before = {r.지원자_ID for r in web.module.store.list_all()}
    web.post("/candidate/new")
    다른cid = ({r.지원자_ID for r in web.module.store.list_all()} - before).pop()
    web.module.recruit.start(다른cid, "admin")
    web.post("/recruit/save", **{f"단계_{cid}_서류 검토": "합격"})
    assert web.module.audit.for_candidate(다른cid) == [] or all(
        e.대상 == 다른cid for e in web.module.audit.for_candidate(다른cid))


def test_변경이력_화면에_채용현황_탭이_있다(web, cid):
    web.post("/recruit/save", **{f"단계_{cid}_기술 면접": "합격"})
    쪽 = web.get("/history?kind=" + urllib.parse.quote("채용현황"))
    assert "기술 면접" in 쪽
