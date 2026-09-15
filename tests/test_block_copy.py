"""블록 복제 — 닮은 블록 하나 더.

대시보드 통째 복제(`copy`)만 있어서, 표 하나를 닮은 것으로 하나 더 만들려면
열·수식·너비를 처음부터 다시 적어야 했다. 시트 블록이면 칸 백 개다.

여기서 지키는 성질은 셋이다.
  - **바로 아래에** 놓인다 (맨 끝이 아니다 — 블록이 열 개면 ↑ 를 아홉 번 누른다)
  - 설정이 **통째로** 따라온다
  - 복제본을 고쳐도 **원본이 안 바뀐다**
"""

from __future__ import annotations

import importlib
import os
import threading
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

import pytest

from cvtool.dashboards import DashboardStore


@pytest.fixture
def store(tmp_path):
    return DashboardStore(tmp_path / "dash.db")


# --- 저장소 ------------------------------------------------------------------
def test_복제본은_바로_아래에_놓인다(store):
    did = store.add("현황판")
    가 = store.add_block(did, "글", 제목="가")
    나 = store.add_block(did, "글", 제목="나")
    다 = store.add_block(did, "글", 제목="다")
    새 = store.copy_block(가)
    assert [x.id for x in store.blocks(did)] == [가, 새, 나, 다]


def test_설정이_통째로_따라온다(store):
    did = store.add("현황판")
    설정 = {"열": ["한글_이름", "부서"], "너비": {"한글_이름": "120"},
          "수식": {"부서": '=IF(부서="","-",부서)'}}
    bid = store.add_block(did, "표", 제목="지원자 표", 설정=설정)
    새 = store.copy_block(bid)
    복 = store.block(새)
    assert 복.종류 == "표"
    assert 복.설정 == 설정


def test_제목에_복제가_붙는다(store):
    """똑같은 제목이 나란히 두 개면 어느 쪽을 고치는지 알 수 없다."""
    did = store.add("현황판")
    bid = store.add_block(did, "글", 제목="안내")
    assert store.block(store.copy_block(bid)).제목 == "안내 복제"


def test_제목이_비어_있으면_종류로_짓는다(store):
    did = store.add("현황판")
    bid = store.add_block(did, "글", 제목="")
    assert store.block(store.copy_block(bid)).제목 == "글 복제"


def test_시트_칸을_고쳐도_다른_쪽은_그대로다(store):
    """설정이 JSON 으로 오가므로 두 블록이 같은 dict 를 나눠 쓰지 않는다."""
    did = store.add("현황판")
    bid = store.add_block(did, "시트", 제목="시트",
                          설정={"행수": 2, "열수": 2,
                              "시트칸": {"A1": {"글": "원본", "배경": "#ff0000"}}})
    새 = store.copy_block(bid)

    설정 = store.block(새).설정
    설정["시트칸"]["A1"]["글"] = "복제본"
    store.save_block(새, 설정=설정)

    assert store.block(bid).설정["시트칸"]["A1"]["글"] == "원본"
    assert store.block(새).설정["시트칸"]["A1"]["글"] == "복제본"
    # 색은 그대로 따라왔다
    assert store.block(새).설정["시트칸"]["A1"]["배경"] == "#ff0000"


def test_없는_블록이면_0_이고_아무것도_안_만든다(store):
    did = store.add("현황판")
    store.add_block(did, "글", 제목="가")
    assert store.copy_block(9999) == 0
    assert len(store.blocks(did)) == 1


def test_복제본을_또_복제해도_순서가_안_엉킨다(store):
    did = store.add("현황판")
    가 = store.add_block(did, "글", 제목="가")
    나 = store.add_block(did, "글", 제목="나")
    첫 = store.copy_block(가)
    둘 = store.copy_block(첫)
    assert [x.id for x in store.blocks(did)] == [가, 첫, 둘, 나]
    assert [x.순서 for x in store.blocks(did)] == [1, 2, 3, 4]   # 틈이 없다
    # 순서가 제대로면 ↑↓ 도 그대로 돈다
    store.move_block(둘, -1)
    assert [x.id for x in store.blocks(did)] == [가, 둘, 첫, 나]


def test_복제는_그_대시보드_안에서만_일어난다(store):
    이쪽 = store.add("이쪽")
    저쪽 = store.add("저쪽")
    bid = store.add_block(이쪽, "글", 제목="가")
    store.add_block(저쪽, "글", 제목="남")
    store.copy_block(bid)
    assert len(store.blocks(이쪽)) == 2
    assert len(store.blocks(저쪽)) == 1


# --- 라우트 ------------------------------------------------------------------
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
            body = urllib.parse.urlencode(fields, doseq=True,
                                          encoding="utf-8").encode()
            req = urllib.request.Request(self.base + path, data=body)
            with opener.open(req, timeout=20) as r:
                return r.status, r.geturl(), r.read().decode("utf-8", "replace")

        def get(self, path: str) -> str:
            with opener.open(self.base + path, timeout=20) as r:
                return r.read().decode("utf-8", "replace")

    c = Client()
    c.post("/login", userid="admin", password="pw1234")
    yield c
    server.shutdown()


def test_복제_단추가_블록을_하나_늘린다(web):
    boards = web.module.boards
    did = boards.add("복제판")
    bid = boards.add_block(did, "숫자", 제목="채용 중",
                           설정={"수식": "=COUNT(채용)"})
    상태, 주소, _ = web.post("/dash/block/copy", id=str(bid))
    assert 상태 == 200
    assert f"/dash/edit?id={did}" in 주소       # 고치던 화면으로 돌아온다
    제목들 = [x.제목 for x in boards.blocks(did)]
    assert 제목들 == ["채용 중", "채용 중 복제"]
    assert boards.blocks(did)[1].설정 == {"수식": "=COUNT(채용)"}


def test_없는_블록을_복제해도_안_터진다(web):
    상태, _, _ = web.post("/dash/block/copy", id="9999")
    assert 상태 == 200


@pytest.mark.parametrize("종류", ["글", "시트"])
def test_복제_단추가_고치기_화면에_있다(web, 종류):
    """시트는 폼이 따로라 도구막대도 자기 것을 쓴다 — 두 군데 다 있어야 한다."""
    boards = web.module.boards
    did = boards.add(f"단추판 {종류}")
    bid = boards.add_block(did, 종류, 제목="안내")
    화면 = web.get(f"/dash/edit?id={did}")
    assert "/dash/block/copy" in 화면
    assert f"<button class='sec' title='이 블록을 바로 아래에" in 화면
    assert str(bid) in 화면
