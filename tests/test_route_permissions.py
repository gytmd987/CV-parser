"""**모든 주소**가 현업에게 열려 있는지 닫혀 있는지 한 군데에 적어 둔다.

과제 매칭 탭이 현업에게 보이던 일이 있었다. 화면에서만 감추거나 라우트마다
권한 검사를 하나씩 붙이는 방식은, 새 화면을 만들 때 빠뜨리면 그대로 구멍이
된다. 그래서 여기서 두 가지를 강제한다.

1. **분류 강제** — `cvtool/web/app.py` 에 있는 모든 `if path == "..."` 이
   아래 표에 있어야 한다. 새 주소를 만들고 표에 안 적으면 테스트가 깨진다.
   깨지면 "권한을 어떻게 할지 정하라"는 뜻이다.
2. **실제 확인** — `거부` 로 적은 주소는 현업 계정으로 진짜 요청해서 403 이
   오는지 본다. 표만 고치고 코드를 안 고치면 여기서 걸린다.
"""

from __future__ import annotations

import importlib
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "cvtool" / "web" / "app.py"

#: 로그인 전에도 되는 주소
공개 = "공개"
#: 현업이 써도 되는 주소
허용 = "허용"
#: 현업에게 403 이어야 하는 주소
거부 = "거부"
#: 배정된 과제의 지원자만 — 남의 지원자면 403 (별도 테스트에서 확인)
자기것만 = "자기것만"
#: 권한 검사가 없어도 되는 주소 (보낸 데이터를 그대로 돌려주는 것 등)
무관 = "무관"

GET_정책 = {
    "/": 허용,                       # 현업은 채용 현황으로 리다이렉트
    "/login": 공개,
    "/logout": 공개,
    "/favicon.ico": 공개,
    "/recruit": 허용,
    "/recruit/export.xlsx": 허용,
    "/recruit/columns": 거부,
    "/dash": 거부,
    "/dash/view": 거부,
    "/dash/edit": 거부,
    "/dash/preview": 거부,           # 수식 미리보기 — 남의 지원자 값이 나온다
    "/candidate": 자기것만,
    "/candidate/file": 자기것만,
    "/attachment": 자기것만,
    "/export.xlsx": 거부,
    "/fields": 거부,
    "/history": 거부,
    "/mail": 거부,
    "/mail/attachment": 거부,
    "/mail/image": 거부,
    "/mail/log": 거부,
    "/mail/draft": 거부,
    "/mail/test": 거부,
    "/mail/template": 거부,
    "/match": 거부,                  # 회사 연구 과제 전체가 보인다
    "/match/curate": 거부,
    "/names": 거부,
    "/org": 거부,
    "/org/edit": 거부,
    "/status/rows": 거부,            # 업로드 현황 조각 (지원자 추가 권한과 같다)
    "/upload": 거부,
    "/users": 거부,
}

POST_정책 = {
    "/login": 공개,
    "/api/cell": 거부,
    "/attachment/add": 거부,
    "/attachment/delete": 거부,
    "/candidate/custom": 거부,
    "/candidate/delete": 거부,
    "/candidate/edit": 거부,
    "/candidate/save": 거부,
    "/candidate/review/done": 거부,
    "/candidate/review/undo": 거부,
    "/candidate/new": 거부,
    "/candidate/reanalyze": 거부,
    "/candidate/year": 거부,
    "/candidates/delete": 거부,
    "/candidates/purge": 거부,
    "/dash/add": 거부,
    "/dash/rename": 거부,
    "/dash/copy": 거부,
    "/dash/delete": 거부,
    "/dash/block/add": 거부,
    "/dash/block/draft": 거부,        # 말로 만드는 목록 표 초안
    "/dash/block/save": 거부,
    "/dash/block/move": 거부,
    "/dash/block/delete": 거부,
    "/candidates/start": 허용,       # 현업은 자기 과제 지원자만 (라우트에서 거른다)
    "/candidates/stop": 허용,
    "/fields": 거부,
    "/fields/add": 거부,
    "/fields/columns": 거부,
    "/fields/delete": 거부,
    "/fields/choices": 거부,
    "/mail/attachment/add": 거부,
    "/mail/image/add": 거부,
    "/mail/image/delete": 거부,
    "/mail/attachment/delete": 거부,
    "/mail/compose": 거부,
    "/mail/send": 거부,
    "/mail/send/one": 거부,
    "/mail/test": 거부,
    "/mail/template/add": 거부,
    "/mail/template/delete": 거부,
    "/mail/template/save": 거부,
    "/mail/test": 거부,
    "/match/all": 거부,
    "/match/curate": 거부,
    "/match/curate/reset": 거부,
    "/match/one": 거부,
    "/names/forget": 거부,
    "/names/save": 거부,
    "/names/tiers": 거부,
    "/org/dept/add": 거부,
    "/org/dept/delete": 거부,
    "/org/dept/rename": 거부,
    "/org/project/add": 거부,
    "/org/project/delete": 거부,
    "/org/project/rename": 거부,
    "/recruit/columns": 거부,
    "/recruit/save": 허용,
    "/recruit/statuses": 거부,
    "/status/clear": 거부,
    "/table.xlsx": 무관,             # 보낸 표를 그대로 엑셀로 바꿔 돌려줄 뿐
    "/upload": 거부,
    "/users/add": 거부,
    "/users/delete": 거부,
    "/users/toggle": 거부,
}


def _찾기(덩어리: str) -> set[str]:
    """`if path == "..."` 와 `if path in ("...", "...")` 를 모두 잡는다."""
    찾은것 = set(re.findall(r'if path == "([^"]+)"', 덩어리))
    for 묶음 in re.findall(r'if path in \(([^)]*)\)', 덩어리):
        찾은것 |= set(re.findall(r'"([^"]+)"', 묶음))
    return 찾은것


def _routes() -> tuple[set[str], set[str]]:
    src = APP.read_text(encoding="utf-8")
    g, p = src.index("def do_GET"), src.index("def do_POST")
    return _찾기(src[g:p]), _찾기(src[p:]) | {"/api/cell"}


def test_every_get_route_is_classified():
    gets, _ = _routes()
    빠진것 = gets - set(GET_정책)
    없는것 = set(GET_정책) - gets
    assert not 빠진것, (
        f"새 GET 주소에 현업 권한을 정하지 않았습니다: {sorted(빠진것)}\n"
        "tests/test_route_permissions.py 의 GET_정책 에 허용/거부/자기것만 중 하나로 적으세요."
    )
    assert not 없는것, f"없어진 GET 주소가 정책에 남아 있습니다: {sorted(없는것)}"


def test_every_post_route_is_classified():
    _, posts = _routes()
    빠진것 = posts - set(POST_정책)
    없는것 = set(POST_정책) - posts
    assert not 빠진것, (
        f"새 POST 주소에 현업 권한을 정하지 않았습니다: {sorted(빠진것)}\n"
        "tests/test_route_permissions.py 의 POST_정책 에 허용/거부 중 하나로 적으세요."
    )
    assert not 없는것, f"없어진 POST 주소가 정책에 남아 있습니다: {sorted(없는것)}"


# --- 진짜로 막히는지 -----------------------------------------------------------
@pytest.fixture(scope="module")
def 서버(tmp_path_factory):
    data = tmp_path_factory.mktemp("routeperm")
    os.environ["CVTOOL_DATA_DIR"] = str(data)
    os.environ["CVTOOL_ADMIN_PASSWORD"] = "pw1234"
    os.environ["CVTOOL_ADMIN_ID"] = "admin"
    os.environ["CVTOOL_PROJECTS_JSON"] = str(data / "과제.json")
    (data / "과제.json").write_text(
        '[{"project_name": "과제 하나", "core_tech": "신호처리"}]', encoding="utf-8"
    )
    import cvtool.config

    importlib.reload(cvtool.config)          # 과제 파일 경로를 다시 읽게 한다
    mod = importlib.reload(importlib.import_module("cvtool.web.app"))
    mod.bootstrap_admin()
    did = mod.auth.add_department("차세대공정")
    pid = mod.auth.add_project(did, "식각")
    mod.auth.create_user("hyun", "현업이", "pw1234", "현업", 생성자="admin")
    mod.auth.assign("hyun", pid)

    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    op.open(base + "/login",
            urllib.parse.urlencode({"userid": "hyun", "password": "pw1234"}).encode())
    yield base, op, mod
    server.shutdown()
    os.environ.pop("CVTOOL_PROJECTS_JSON", None)


def _code(op, url: str, body: bytes | None = None) -> int:
    try:
        req = urllib.request.Request(url, data=body)
        with op.open(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize("경로", sorted(p for p, v in GET_정책.items() if v == 거부))
def test_field_worker_is_refused_on_every_closed_get(서버, 경로):
    base, op, _ = 서버
    assert _code(op, base + 경로) == 403, 경로


@pytest.mark.parametrize("경로", sorted(p for p, v in POST_정책.items() if v == 거부))
def test_field_worker_is_refused_on_every_closed_post(서버, 경로):
    base, op, _ = 서버
    assert _code(op, base + 경로, b"") == 403, 경로


def test_field_worker_does_not_see_the_match_tab(서버):
    """설정이 켜져 있어도 현업 화면에는 과제 매칭 탭이 없어야 한다."""
    base, op, mod = 서버
    assert mod.settings.projects_json, "이 테스트는 과제 파일이 설정돼 있어야 의미가 있다"
    with op.open(base + "/recruit") as r:
        머리 = r.read().decode("utf-8", "replace").split("<main>", 1)[0]
    assert "href='/match'" not in 머리


def test_field_worker_does_not_see_the_match_card(서버):
    """자기 과제 지원자를 열어도 과제 매칭 결과는 안 보여야 한다."""
    base, op, mod = 서버
    rec = mod.store.create_blank()
    did = mod.auth.departments()[0]["id"]
    pid = mod.auth.projects()[0]["id"]
    mod.recruit.set_assignment(rec.지원자_ID, did, pid, "admin")
    with op.open(base + "/candidate?id=" + urllib.parse.quote(rec.지원자_ID)) as r:
        본문 = r.read().decode("utf-8", "replace")
    assert "연구 과제 매칭" not in 본문
    mod.store.delete(rec.지원자_ID)
