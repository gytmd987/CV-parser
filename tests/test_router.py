"""주소표(`cvtool/web/router.py`) — 권한을 안 적거나 틀리게 적으면 등록·기동이 안 된다."""

from __future__ import annotations

import json

import pytest

from cvtool.web.router import Router, 공개, 로그인만, 통과


def _함수(self, me, path):
    return None


def test_권한을_안_적으면_등록이_안_된다():
    with pytest.raises(TypeError):
        Router().route("GET", "/x")(_함수)          # 권한= 이 없다


def test_모르는_권한이면_점검에서_터진다():
    r = Router()
    r.route("GET", "/a", 권한="대시보드_조회")(_함수)
    r.route("GET", "/b", 권한=("대시보드_조회", "없는권한"))(_함수)
    with pytest.raises(ValueError, match="없는권한"):
        r.점검({"대시보드_조회": ()})
    r2 = Router()
    r2.route("GET", "/c", 권한=로그인만)(_함수)
    r2.route("GET", "/d", 권한=공개)(_함수)
    r2.점검({})                                     # 로그인만·공개는 이름이 아니다


def test_다른_함수가_같은_주소를_잡으면_터진다():
    r = Router()
    r.route("GET", "/a", 권한=로그인만)(_함수)
    r.route("GET", "/a", 권한=로그인만)(_함수)       # 같은 함수 (다시 읽기) — 괜찮다

    def 딴것(self, me, path):
        return None
    with pytest.raises(ValueError, match="두 번"):
        r.route("GET", "/a", 권한=로그인만)(딴것)


def test_묶음은_하나라도_있으면_통과():
    can = lambda me, 이름: 이름 in me
    assert 통과({"B"}, ("A", "B"), can)
    assert not 통과({"C"}, ("A", "B"), can)
    assert 통과(set(), 로그인만, can)


def test_앱의_모든_주소가_권한을_적었다(web_client):
    from cvtool.auth import _PERMISSIONS

    길들 = web_client.module.라우터.all()
    assert len(길들) > 100
    web_client.module.라우터.점검(_PERMISSIONS)     # 모르는 권한 없음


# --- 현업이 다른 현업 계정을 건드리던 구멍 ------------------------------------------
@pytest.fixture(scope="module")
def 현업둘(web_client):
    m = web_client.module
    m.auth.create_user("hy1", "현업1", "pw1234", "현업")
    m.auth.create_user("hy2", "현업2", "pw1234", "현업")
    return web_client.new().login("hy1", "pw1234")


def test_현업은_다른_현업_계정을_끄지_못한다(web_client, 현업둘):
    code, _ = 현업둘.post("/users/toggle", id="hy2")
    assert code == 403
    assert web_client.module.auth.get_user("hy2").활성


def test_현업은_다른_현업_계정을_지우지_못한다(web_client, 현업둘):
    code, _ = 현업둘.post("/users/delete", id="hy2")
    assert code == 403
    assert web_client.module.auth.get_user("hy2") is not None


def test_채용담당자는_여전히_현업_계정을_끌_수_있다(web_client, 현업둘):
    m = web_client.module
    m.auth.create_user("hrx", "담당", "pw1234", "채용담당자")
    hr = web_client.new().login("hrx", "pw1234")
    hr.post("/users/toggle", id="hy2")
    assert not m.auth.get_user("hy2").활성


def test_fetch_주소는_권한이_없으면_JSON_으로_답한다(web_client, 현업둘):
    code, 몸 = 현업둘.post("/api/cell", id="x", 항목="비고", 새값="a", 이전값="")
    assert code == 403 and json.loads(몸)["error"] == "수정 권한이 없습니다."
