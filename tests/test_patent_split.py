"""특허 — 등록을 국내·해외로, 같은 발명은 한 건으로.

옛 열은 `특허_등록_수` · `특허_출원_수` 였다. 두 가지가 틀렸다.

  - **출원을 실적으로 셌다.** 출원은 아직 아무것도 아니다.
  - **같은 발명을 나라 수만큼 셌다.** 같은 특허를 한국·미국·중국에 등록하면
    이력서에 세 줄로 적힌다. 그걸 3건으로 세면 발명 하나가 셋으로 부푼다.

이제 `특허_등록_국내_수` · `특허_등록_해외_수` 이고, 국내·해외 **안에서** 제목이
같은 것을 한 건으로 묶는다 (미국 + 중국 = 해외 1).
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

from cvtool import edit
from cvtool.schemas import COLUMNS, COUNT_COLUMNS, CVRecord, Patent


def _사람(*특허들: Patent) -> CVRecord:
    return CVRecord(지원자_ID="X", 특허=list(특허들))


def _등록(제목: str, 지역: str, 국가: str = "") -> Patent:
    return Patent(제목=제목, 상태="등록", 국내해외=지역, 국가=국가)


# --- 세는 법 ----------------------------------------------------------------
def test_같은_발명을_세_나라에_내면_국내1_해외1():
    """신고하신 바로 그 경우다."""
    rec = _사람(_등록("자동 정렬 방법", "국내", "한국"),
              _등록("자동 정렬 방법", "해외", "미국"),
              _등록("자동 정렬 방법", "해외", "중국"))
    assert rec.특허_수() == {"특허_등록_국내_수": 1, "특허_등록_해외_수": 1}


def test_다른_발명은_따로_센다():
    rec = _사람(_등록("가 방법", "해외", "미국"), _등록("나 방법", "해외", "미국"))
    assert rec.특허_수()["특허_등록_해외_수"] == 2


def test_출원은_어느_열에도_안_들어간다():
    rec = _사람(Patent(제목="가", 상태="출원", 국내해외="국내"),
              Patent(제목="나", 상태="출원", 국내해외="해외"))
    assert rec.특허_수() == {"특허_등록_국내_수": 0, "특허_등록_해외_수": 0}
    assert rec.특허_출원_건수() == 2          # 상세 화면에는 보여준다


def test_국내해외를_모르면_안_센다():
    """추측해서 넣으면 아무도 틀린 줄 모른다. 검토 필요로 올릴 일이다."""
    rec = _사람(_등록("가", "불명"))
    assert rec.특허_수() == {"특허_등록_국내_수": 0, "특허_등록_해외_수": 0}


def test_띄어쓰기와_대소문자만_맞춘다():
    rec = _사람(_등록("Auto  Align METHOD", "해외"), _등록("auto align method", "해외"))
    assert rec.특허_수()["특허_등록_해외_수"] == 1


def test_제목이_비면_줄마다_센다():
    """제목 없는 두 줄을 같은 발명으로 단정하면 있는 실적이 사라진다."""
    rec = _사람(_등록("", "국내"), _등록("", "국내"))
    assert rec.특허_수()["특허_등록_국내_수"] == 2


def test_같은_제목이라도_국내와_해외는_따로_센다():
    rec = _사람(_등록("가", "국내"), _등록("가", "해외"))
    assert rec.특허_수() == {"특허_등록_국내_수": 1, "특허_등록_해외_수": 1}


# --- 열 ---------------------------------------------------------------------
def test_옛_열은_사라지고_새_열이_들어왔다():
    assert "특허_출원_수" not in COLUMNS
    assert "특허_등록_수" not in COLUMNS
    assert "특허_등록_국내_수" in COLUMNS and "특허_등록_해외_수" in COLUMNS
    # 계산 열이라 사람이 표에서 직접 못 고친다
    assert {"특허_등록_국내_수", "특허_등록_해외_수"} <= set(COUNT_COLUMNS)
    assert {"특허_등록_국내_수", "특허_등록_해외_수"} <= edit.READONLY_FIELDS


def test_표_한_줄에_새_열이_들어간다():
    row = _사람(_등록("가", "국내"), _등록("가", "해외"), _등록("나", "해외")).to_row()
    assert row["특허_등록_국내_수"] == "1"
    assert row["특허_등록_해외_수"] == "2"
    assert "특허_등록_수" not in row and "특허_출원_수" not in row


def test_0_은_빈칸이다():
    """표가 0 으로 도배되면 안 읽힌다 (다른 개수 열과 같은 규칙)."""
    assert _사람().to_row()["특허_등록_국내_수"] == ""


# --- 손으로 고치기 -----------------------------------------------------------
def test_특허_한_줄_검사():
    pt = edit.validate_patent({"제목": " 가 방법 ", "상태": "등록", "연도": "2024",
                               "번호": "10-1234567", "국가": "한국",
                               "국내해외": "국내"})
    assert pt.제목 == "가 방법" and pt.상태 == "등록" and pt.국내해외 == "국내"
    assert pt.국가 == "한국" and pt.번호 == "10-1234567"


def test_제목과_번호가_다_비면_버린다():
    """화면 맨 아래의 추가용 빈 줄이다. 저장할 때마다 빈 특허가 쌓이면 안 된다."""
    assert edit.validate_patent({"상태": "등록", "국내해외": "국내"}) is None


def test_번호만_적은_줄도_받는다():
    assert edit.validate_patent({"번호": "10-1234567"}) is not None


def test_빈_값은_기본값을_살린다():
    pt = edit.validate_patent({"제목": "가"})
    assert pt.상태 == "불명" and pt.국내해외 == "불명"


@pytest.mark.parametrize("한줄, 말", [
    ({"제목": "가", "연도": "24"}, "4자리"),
    ({"제목": "가", "상태": "출원중"}, "상태"),
    ({"제목": "가", "국내해외": "미국"}, "국내해외"),
])
def test_어긋난_값은_저장을_거부한다(한줄, 말):
    with pytest.raises(edit.ValidationError) as e:
        edit.validate_patent(한줄)
    assert 말 in str(e.value)


# --- 검토 사유 ---------------------------------------------------------------
def test_나라를_모르는_등록_특허는_검토_필요로_올라간다():
    """조용히 개수에서 빠지면 아무도 모른다."""
    from cvtool.extract import _assemble

    rec = _assemble(
        {"research": {"특허": [{"제목": "가", "상태": "등록", "국내해외": "불명"}],
                      "연구분야_키워드": ["가"]}},
        [], 지원자_ID="X", 원본_파일명="")
    assert "국내/해외를 판단하지 못함" in rec.검토_사유


def test_사유가_새_열을_가리킨다():
    from cvtool import review

    assert review.columns_for("특허 1건의 등록/출원 여부를 판단하지 못함") == [
        "특허_등록_국내_수", "특허_등록_해외_수"]


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
                return r.geturl(), r.read().decode("utf-8", "replace")

        def get(self, path: str) -> str:
            with opener.open(self.base + path, timeout=20) as r:
                return r.read().decode("utf-8", "replace")

    c = Client()
    c.post("/login", userid="admin", password="pw1234")
    yield c
    server.shutdown()


@pytest.fixture
def 사람(web):
    """새로 만든 사람의 ID. **list_all()[0] 을 쓰면 안 된다** — 시험마다
    한 명씩 쌓이므로 그 자리가 다른 사람일 수 있다."""
    있던것 = {r.지원자_ID for r in web.module.store.list_all()}
    web.post("/candidate/new")
    새것 = [r.지원자_ID for r in web.module.store.list_all()
          if r.지원자_ID not in 있던것]
    assert len(새것) == 1
    return 새것[0]


def test_특허를_손으로_넣고_고치고_지운다(web, 사람):
    store = web.module.store
    # 두 줄 넣기 — 같은 발명을 한국·미국에
    web.post("/candidate/patents", id=사람, 끝="3",
             특허제목_1="정렬 방법", 특허상태_1="등록", 특허국가_1="한국", 특허국내해외_1="국내",
             특허연도_1="2024", 특허번호_1="10-111",
             특허제목_2="정렬 방법", 특허상태_2="등록", 특허국가_2="미국", 특허국내해외_2="해외",
             특허연도_2="2024", 특허번호_2="US-222",
             특허제목_3="", 특허번호_3="")                 # 추가용 빈 줄은 안 들어간다
    rec = store.get(사람)
    assert len(rec.특허) == 2
    assert rec.to_row()["특허_등록_국내_수"] == "1"
    assert rec.to_row()["특허_등록_해외_수"] == "1"

    # 불명으로 고치면 개수에서 빠진다
    web.post("/candidate/patents", id=사람, 끝="2",
             특허제목_1="정렬 방법", 특허상태_1="등록", 특허국가_1="한국", 특허국내해외_1="불명",
             특허제목_2="정렬 방법", 특허상태_2="등록", 특허국가_2="미국", 특허국내해외_2="해외")
    assert store.get(사람).to_row()["특허_등록_국내_수"] == ""

    # 한 줄 지우기
    web.post("/candidate/patents", id=사람, 끝="2", 특허del_1="1",
             특허제목_2="정렬 방법", 특허상태_2="등록", 특허국가_2="미국", 특허국내해외_2="해외")
    assert len(store.get(사람).특허) == 1


def test_어긋난_줄은_저장을_막고_까닭을_말한다(web, 사람):
    주소, _ = web.post("/candidate/patents", id=사람, 끝="1",
                     특허제목_1="가", 특허연도_1="24")
    assert "err=" in 주소
    assert web.module.store.get(사람).특허 == []


def test_특허_목록_저장_단추가_상세_화면에_있다(web, 사람):
    화면 = web.get(f"/candidate?id={urllib.parse.quote(사람)}")
    assert "/candidate/patents" in 화면
    assert "특허 목록 저장" in 화면
    assert "국내/해외" in 화면
