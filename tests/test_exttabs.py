"""외부 탭 — 다른 시스템 화면을 탭 줄에 붙인다."""

from __future__ import annotations

import urllib.parse


def test_관리자가_외부_탭을_붙이면_탭_줄에_나온다(web_client):
    c = web_client
    _코드, 화면 = c.post("/admin/tabs/save", 이름="과제 관리", 주소="ftp://x", 열기="안")
    assert "http://" in 화면                                   # 주소가 틀리면 알려 준다
    c.post("/admin/tabs/save", 이름="과제 관리", 주소="http://10.0.0.5:8080", 열기="안")
    c.post("/admin/tabs/save", 이름="문서함", 주소="http://10.0.0.6:9000/docs", 열기="새창")
    첫 = c.get("/")
    assert "href='/ext?id=1'>과제 관리</a>" in 첫
    assert "href='http://10.0.0.6:9000/docs' target='_blank'" in 첫
    화면 = c.get("/ext?id=1")
    assert "<iframe id='외부' src='http://10.0.0.5:8080'" in 화면
    assert "href='/ext?id=1' class=on>과제 관리" in 화면          # 그 탭에 불이 켜진다

    c.post("/admin/tabs/move", id="2", 쪽="위")
    assert c.get("/").index("문서함") < c.get("/").index("과제 관리")
    c.post("/admin/tabs/save", id="1", 이름="과제", 주소="http://10.0.0.5:8081", 열기="안")
    assert "src='http://10.0.0.5:8081'" in c.get("/ext?id=1")
    c.post("/admin/tabs/delete", id="1")
    c.post("/admin/tabs/delete", id="2")
    assert "/ext?id=" not in c.get("/")


def test_현업에게는_외부_탭이_안_보인다(web_client):
    c = web_client
    m = c.module
    c.post("/admin/tabs/save", 이름="비밀탭", 주소="http://10.0.0.7:1", 열기="안")
    m.auth.create_user("hyun2", "현업이", "pw1234", "현업", 생성자="admin")
    h = c.new().login("hyun2", "pw1234")
    assert "비밀탭" not in h.get("/recruit")
    tid = next(t["id"] for t in m.외부탭들() if t["이름"] == "비밀탭")
    try:
        h.get(f"/ext?id={tid}")
        막힘 = False
    except urllib.error.HTTPError as e:
        막힘 = e.code == 403
    assert 막힘
