"""자동 채우기 · 복사 — 수식의 칸 참조가 자리만큼 밀린다.

엑셀과 같은 규칙이다. `=SUM(A1:C1)` 을 한 줄 아래로 채우면 `=SUM(A2:C2)` 가
된다. `$` 가 붙은 쪽은 안 밀린다.

**미는 일은 서버가 한다.** 브라우저에서 밀면 규칙이 두 벌이 되어, 화면이 만든
수식과 서버가 계산하는 수식이 조용히 갈라진다.
"""

from __future__ import annotations

import pytest

from cvtool import formula as F
from cvtool import sheet as S
from cvtool.dashboards import 시트_다듬기, 시트_채우기


# --- $ 고정 ------------------------------------------------------------------
@pytest.mark.parametrize("글, 행차, 열차, 나올것", [
    ("=SUM(A1:C1)", 1, 0, "=SUM(A2:C2)"),          # 아래로
    ("=SUM(A1:C1)", 0, 1, "=SUM(B1:D1)"),          # 옆으로
    ("=SUM(A1:C1)", 2, 2, "=SUM(C3:E3)"),
    ("=SUM($A$1:$C$1)", 5, 5, "=SUM($A$1:$C$1)"),  # 둘 다 고정 — 안 움직인다
    ("=$A1", 2, 3, "=$A3"),                        # 열만 고정
    ("=A$1", 2, 3, "=D$1"),                        # 행만 고정
    ("=A1+$B$2*C3", 1, 1, "=B2+$B$2*D4"),
    ("=A1", 0, 0, "=A1"),                          # 안 옮기면 그대로
])
def test_참조가_자리만큼_밀린다(글, 행차, 열차, 나올것):
    assert S.옮기기(글, 행차, 열차) == 나올것


def test_수식이_아니면_글자_그대로다():
    assert S.옮기기("A1 을 보라", 1, 0) == "A1 을 보라"
    assert S.옮기기("123", 1, 0) == "123"


def test_따옴표_안은_안_건드린다():
    assert S.옮기기('="A1 을 보라"&A1', 1, 0) == '="A1 을 보라"&A2'


def test_열_이름에_붙은_것은_주소가_아니다():
    """`부서A1` 은 열 이름일 수 있다. 앞뒤에 낱말 글자가 붙으면 안 민다."""
    assert S.옮기기("=부서A1", 1, 0) == "=부서A1"
    assert S.옮기기('=COUNT(지원자, 부서="A1")', 1, 0) == '=COUNT(지원자, 부서="A1")'


def test_격자_밖으로_나가면_참조_오류다():
    """조용히 0 행에 붙여 두면 그럴듯한 딴 칸을 가리키게 된다."""
    assert S.옮기기("=A1", -1, 0) == "=#참조!"
    assert S.옮기기("=A1", 0, -1) == "=#참조!"
    assert S.옮기기("=A1+B2", -1, 0) == "=#참조!+B1"


def test_참조_오류는_계산할_때_말해_준다():
    rows = F.Rows(지원자=[], 채용=[])
    값, 오류 = S.값들({"A1": {"글": "=#참조!+1"}}, rows, set())
    assert 값["A1"] == "?"
    assert "격자 밖" in 오류[0]


# --- 계산할 때는 $ 가 없는 것과 같다 -------------------------------------------
def test_고정_표시는_계산을_안_바꾼다():
    rows = F.Rows(지원자=[], 채용=[])
    칸 = {"A1": {"글": "2"}, "B1": {"글": "3"},
         "C1": {"글": "=SUM($A$1:B1)"}, "D1": {"글": "=SUM(A1:B1)"}}
    값, 오류 = S.값들(칸, rows, set())
    assert 값["C1"] == 값["D1"] == "5"
    assert not 오류


def test_고정떼기():
    assert S.고정떼기("=SUM($A$1:$C$1)+$B2") == "=SUM(A1:C1)+B2"
    assert S.고정떼기('="$A$1 이라고 적음"') == '="$A$1 이라고 적음"'


def test_자리는_고정_표시를_무시한다():
    assert S.자리("$A$1") == S.자리("A1") == (0, 0)
    assert S.자리("$AB$12") == S.자리("AB12")


# --- 모델에 깔기 --------------------------------------------------------------
@pytest.fixture
def 모델():
    return {"행수": 6, "열수": 5,
            "칸": {"A1": {"글": "1"}, "B1": {"글": "2"}, "C1": {"글": "3"},
                  "D1": {"글": "=SUM(A1:C1)", "굵게": 1, "배경": "#ffcc00"}}}


def test_아래로_채우면_줄마다_제_줄을_더한다(모델):
    칸 = 시트_채우기(모델, "D1", "D1:D4")["칸"]
    assert 칸["D2"]["글"] == "=SUM(A2:C2)"
    assert 칸["D3"]["글"] == "=SUM(A3:C3)"
    assert 칸["D4"]["글"] == "=SUM(A4:C4)"


def test_서식도_따라온다(모델):
    칸 = 시트_채우기(모델, "D1", "D1:D2")["칸"]
    assert 칸["D2"]["굵게"] == 1 and 칸["D2"]["배경"] == "#ffcc00"


def test_원본은_안_바뀐다(모델):
    칸 = 시트_채우기(모델, "D1", "D1:D4")["칸"]
    assert 칸["D1"]["글"] == "=SUM(A1:C1)"
    assert 모델["칸"]["D1"]["글"] == "=SUM(A1:C1)"      # 넘긴 dict 도 그대로


def test_한_줄을_여러_줄에_되풀이해_깐다(모델):
    칸 = 시트_채우기(모델, "A1:D1", "A2:D3")["칸"]
    assert 칸["A2"]["글"] == "1" and 칸["D2"]["글"] == "=SUM(A2:C2)"
    assert 칸["A3"]["글"] == "1" and 칸["D3"]["글"] == "=SUM(A3:C3)"


def test_빈_칸을_깔면_대상이_지워진다(모델):
    """엑셀도 빈 칸을 복사하면 빈 칸이 된다."""
    칸 = 시트_채우기(모델, "E1", "A1:C1")["칸"]
    assert "A1" not in 칸 and "B1" not in 칸 and "C1" not in 칸


def test_격자_밖은_안_깐다(모델):
    칸 = 시트_채우기(모델, "D1", "D1:D99")["칸"]
    assert "D6" in 칸 and "D7" not in 칸          # 행수 6


def test_병합은_안_따라간다(모델):
    모델["칸"]["A1"]["가로병합"] = 2
    칸 = 시트_채우기(모델, "A1", "A2")["칸"]
    assert "가로병합" not in 칸["A2"]


def test_주소가_아니면_아무것도_안_한다(모델):
    assert 시트_채우기(모델, "없음", "A2") == 모델


def test_깐_뒤에도_다듬기를_지난다(모델):
    """서버는 채운 결과도 믿지 않는다 — 격자 밖 칸·이상한 색은 여전히 걸러진다."""
    설정 = 시트_다듬기(시트_채우기(모델, "D1", "D1:D4"))
    assert 설정["시트칸"]["D3"]["글"] == "=SUM(A3:C3)"
    assert all("!" not in a for a in 설정["시트칸"])


# --- 채운 수식이 실제로 계산된다 ------------------------------------------------
def test_채운_수식이_줄마다_다른_값을_낸다():
    rows = F.Rows(지원자=[], 채용=[])
    모델 = {"행수": 3, "열수": 3,
          "칸": {"A1": {"글": "1"}, "B1": {"글": "2"}, "C1": {"글": "=SUM(A1:B1)"},
                "A2": {"글": "10"}, "B2": {"글": "20"},
                "A3": {"글": "100"}, "B3": {"글": "200"}}}
    칸 = 시트_채우기(모델, "C1", "C1:C3")["칸"]
    값, 오류 = S.값들(칸, rows, set())
    assert (값["C1"], 값["C2"], 값["C3"]) == ("3", "30", "300")
    assert not 오류


def test_고정한_칸은_채워도_같은_칸을_본다():
    rows = F.Rows(지원자=[], 채용=[])
    모델 = {"행수": 3, "열수": 2,
          "칸": {"A1": {"글": "10"}, "A2": {"글": "20"}, "A3": {"글": "30"},
                "B1": {"글": "=A1/$A$1*100"}}}
    칸 = 시트_채우기(모델, "B1", "B1:B3")["칸"]
    assert 칸["B3"]["글"] == "=A3/$A$1*100"
    값, _ = S.값들(칸, rows, set())
    assert (값["B1"], 값["B2"], 값["B3"]) == ("100", "200", "300")


# --- 라우트 ------------------------------------------------------------------
@pytest.fixture(scope="module")
def web(tmp_path_factory):
    import importlib
    import json
    import os
    import threading
    import urllib.parse
    import urllib.request
    from http.cookiejar import CookieJar
    from http.server import ThreadingHTTPServer

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
        dumps = staticmethod(json.dumps)

        def post(self, path: str, **fields):
            body = urllib.parse.urlencode(fields, doseq=True,
                                          encoding="utf-8").encode()
            req = urllib.request.Request(self.base + path, data=body)
            with opener.open(req, timeout=20) as r:
                return r.read().decode("utf-8", "replace")

        def get(self, path: str) -> str:
            with opener.open(self.base + path, timeout=20) as r:
                return r.read().decode("utf-8", "replace")

    c = Client()
    c.post("/login", userid="admin", password="pw1234")
    yield c
    server.shutdown()


def test_저장하면서_채운다(web):
    boards = web.module.boards
    did = boards.add("채우기판")
    bid = boards.add_block(did, "시트", 제목="시트")
    모델 = {"행수": 4, "열수": 4,
          "칸": {"A1": {"글": "1"}, "B1": {"글": "2"},
                "C1": {"글": "=SUM(A1:B1)"}, "A2": {"글": "10"}, "B2": {"글": "20"}}}
    web.post("/dash/sheet/save", id=str(bid), title="시트",
             sheet=web.dumps(모델), 채울원본="C1", 채울대상="C1:C3")
    칸 = boards.block(bid).설정["시트칸"]
    assert 칸["C2"]["글"] == "=SUM(A2:B2)"
    assert 칸["C3"]["글"] == "=SUM(A3:B3)"


def test_채우라고_안_하면_그대로_저장된다(web):
    boards = web.module.boards
    did = boards.add("안채우기판")
    bid = boards.add_block(did, "시트", 제목="시트")
    모델 = {"행수": 3, "열수": 3, "칸": {"C1": {"글": "=SUM(A1:B1)"}}}
    web.post("/dash/sheet/save", id=str(bid), title="시트",
             sheet=web.dumps(모델), 채울원본="", 채울대상="")
    assert set(boards.block(bid).설정["시트칸"]) == {"C1"}


def test_채우기_손잡이와_안내가_화면에_있다(web):
    boards = web.module.boards
    did = boards.add("손잡이판")
    boards.add_block(did, "시트", 제목="시트")
    화면 = web.get(f"/dash/edit?id={did}")
    assert "채울원본" in 화면 and "채울대상" in 화면
    assert "fillgrip" in 화면
    assert "Ctrl+D" in 화면
