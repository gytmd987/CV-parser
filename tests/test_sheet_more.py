"""시트: 칸 자리 함수 · 행열 끼우기/빼기 · 테두리 · 다시 계산 (저장 안 함)."""

from __future__ import annotations

import io
import json
import zipfile
from xml.etree import ElementTree

import pytest

from cvtool import expr as E
from cvtool import sheet as S
from cvtool.dashboards import (
    Block,
    render_sheet,
    시트_다듬기,
    시트_칸스타일,
    시트_행열,
)
from cvtool.export import build_sheet_xlsx
from cvtool.formula import Rows

빈줄 = Rows(지원자=[], 채용=[])
표 = {"A1": {"글": "사과"}, "A2": {"글": "배"}, "A3": {"글": "감"},
     "B1": {"글": "10"}, "B2": {"글": "20"}, "B3": {"글": "30"}}


def _값(칸: dict) -> tuple[dict, list]:
    return S.값들({**표, **칸}, 빈줄)


# --- 칸 자리 함수 --------------------------------------------------------------
@pytest.mark.parametrize("수식, 기대", [
    ("=ROW()", "4"),                          # D4 칸
    ("=COLUMN()", "4"),
    ("=ROW(B7)", "7"),
    ("=COLUMN(C1)", "3"),
    ("=ROWS(A1:A3)", "3"),
    ("=COLUMNS(A1:B1)", "2"),
    ("=INDEX(A1:B3, 2, 2)", "20"),
    ("=INDEX(B1:B3, 3)", "30"),               # 한 열짜리는 번호 하나
    ("=MATCH(\"감\", A1:A3, 0)", "3"),
    ("=VLOOKUP(\"배\", A1:B3, 2, FALSE)", "20"),
    ("=VLOOKUP(25, B1:B3, 1)", "20"),         # 대충 찾기: 25 이하 중 가장 큰 것
    ("=INDEX(B1:B3, MATCH(\"감\", A1:A3, 0))", "30"),
    ("=CHOOSE(2, \"가\", \"나\", \"다\")", "나"),
    ("=IFERROR(VLOOKUP(\"없음\", A1:B3, 2, FALSE), \"-\")", "-"),
])
def test_칸_자리_함수(수식, 기대):
    값, 오류 = _값({"D4": {"글": 수식}})
    assert 오류 == [] and 값["D4"] == 기대


def test_집계와_섞어_쓴다():
    값, _ = _값({"D1": {"글": "=COUNT(지원자) + ROW()"}})
    assert 값["D1"] == "1"


def test_범위_밖_INDEX_는_말해_준다():
    값, 오류 = _값({"D1": {"글": "=INDEX(A1:B3, 4, 1)"}})
    assert 값["D1"] == "?" and "범위 밖" in 오류[0]


def test_목록_표에서_COLUMN_은_열_번호():
    assert E.evaluate("=COLUMN()", {E.열번호_키: 3}) == "3"
    with pytest.raises(E.ExprError, match="시트에서"):
        E.evaluate("=INDEX(1, 2)", {})


def test_PERIOD_WRAP_N():
    값들 = {"시작": "202203", "끝": "재직중", "빈": "", "수": "3"}
    assert E.evaluate("=PERIOD(시작, 끝)", 값들) == "'22.3~현재"
    assert E.evaluate('=WRAP(PERIOD(시작, 끝), "(", ")")', 값들) == "('22.3~현재)"
    assert E.evaluate('=WRAP(빈, "(", ")")', 값들) == ""
    assert E.evaluate("=N(빈) + N(수)", 값들) == "3"


def test_자동완성에는_속_함수가_안_보인다():
    assert "_INDEX" in E.FUNC_NAMES and "_INDEX" not in E.PUBLIC_FUNC_NAMES
    for 이름 in ("ROW", "COLUMN", "INDEX", "MATCH", "VLOOKUP", "PERIOD", "WRAP"):
        assert 이름 in E.PUBLIC_FUNC_NAMES


# --- 행·열 끼우기/빼기 -----------------------------------------------------------
def test_행을_끼우면_칸과_참조가_밀린다():
    m = {"행수": 3, "열수": 2, "칸": {"A1": {"글": "1"}, "A2": {"글": "2"},
                                   "A3": {"글": "=A1+A2"}, "B3": {"글": "=SUM($A$1:A3)"}},
         "행높이": {"2": "40"}}
    새 = 시트_행열(m, "행삽입", 1)
    assert 새["행수"] == 4
    assert 새["칸"]["A3"]["글"] == "2"
    assert 새["칸"]["A4"]["글"] == "=A1+A3"
    assert 새["칸"]["B4"]["글"] == "=SUM($A$1:A4)"
    assert 새["행높이"] == {"3": "40"}


def test_뺀_줄을_가리키던_참조는_참조오류():
    m = {"행수": 3, "열수": 1, "칸": {"A1": {"글": "1"}, "A2": {"글": "2"},
                                   "A3": {"글": "=A1+A2"}}}
    새 = 시트_행열(m, "행삭제", 1)
    assert 새["칸"]["A2"]["글"] == f"=A1+{S.밖_표시}"


def test_열도_된다_병합도_늘어난다():
    m = {"행수": 2, "열수": 3, "칸": {"A1": {"글": "x", "가로병합": 3}, "C2": {"글": "=A1"}}}
    새 = 시트_행열(m, "열삽입", 1)
    assert 새["열수"] == 4 and 새["칸"]["A1"]["가로병합"] == 4
    assert 새["칸"]["D2"]["글"] == "=A1"


def test_마지막_한_줄은_못_뺀다():
    m = {"행수": 1, "열수": 1, "칸": {}}
    assert 시트_행열(m, "행삭제", 0)["행수"] == 1


# --- 테두리 ---------------------------------------------------------------------
def test_테두리는_정해진_모양만_받는다():
    d = 시트_다듬기({"칸": {"A1": {"테두리": {"위": "굵게", "왼쪽": "x;color:red"},
                                  "테두리색": "red;display:none"}}})
    assert d["시트칸"]["A1"] == {"테두리": {"위": "굵게"}}
    assert 시트_칸스타일(d["시트칸"]["A1"]) == "border-top:2px solid #222222"


def test_테두리가_엑셀에도_실린다():
    b = Block(1, 1, 0, "시트", "t", {"행수": 2, "열수": 2, "시트칸": {
        "A1": {"글": "1", "테두리": {"아래": "이중", "오른쪽": "얇게"}, "테두리색": "#ff0000"}}})
    데이터 = build_sheet_xlsx(render_sheet(b, 빈줄))
    with zipfile.ZipFile(io.BytesIO(데이터)) as z:
        스타일 = z.read("xl/styles.xml").decode()
        ElementTree.fromstring(스타일)                    # 깨지지 않은 XML
        ElementTree.fromstring(z.read("xl/worksheets/sheet1.xml"))
    assert '<bottom style="double"><color rgb="FFFF0000"/></bottom>' in 스타일
    assert '<right style="thin"><color rgb="FFFF0000"/></right>' in 스타일


def test_격자숨김은_저장된다():
    assert 시트_다듬기({"격자숨김": 1})["격자숨김"] is True
    assert Block(1, 1, 0, "시트", "", {"격자숨김": True}).시트격자숨김


# --- 웹: 다시 계산은 저장하지 않는다 --------------------------------------------
def test_다시_계산은_저장하지_않고_값과_표를_돌려준다(web_client):
    web = web_client
    mod = web.module
    did = mod.boards.add("시트 계산", "admin")
    bid = mod.boards.add_block(did, "시트", 제목="S")
    모델 = {"행수": 3, "열수": 2, "칸": {"A1": {"글": "5"}, "A2": {"글": "=A1*2"}}}
    code, 몸 = web.post("/dash/sheet/calc", id=bid, sheet=json.dumps(모델))
    j = json.loads(몸)
    assert code == 200 and "data-cell='A2'" in j["html"] and ">10<" in j["html"]
    assert mod.boards.block(bid).시트칸 == {}             # 저장 안 됐다

    code, 몸 = web.post("/dash/sheet/calc", id=bid, sheet=json.dumps(모델),
                        행열="행삽입", 위치="0", 개수="1")
    j = json.loads(몸)
    assert j["sheet"]["행수"] == 4 and j["sheet"]["칸"]["A3"]["글"] == "=A2*2"

    web.post("/dash/sheet/save", id=bid, sheet=json.dumps(모델), 끝="1")
    assert mod.boards.block(bid).시트칸["A2"]["글"] == "=A1*2"


def test_보기에서_격자를_숨기면_머리글이_없다(web_client):
    web = web_client
    mod = web.module
    did = mod.boards.add("시트 보기", "admin")
    bid = mod.boards.add_block(did, "시트", 제목="S",
                               설정={"격자숨김": True, "시트열너비": {"A": "150"},
                                    "시트칸": {"A1": {"글": "x"}}})
    쪽 = web.get(f"/dash/view?id={did}")
    자리 = 쪽[쪽.index("table class='sheet plain'"):][:600]
    assert "<th" not in 자리 and "width:150px" in 자리
    편집 = web.get(f"/dash/edit?id={did}")
    assert "data-col='A'" in 편집 and "data-sheet='테두리'" in 편집
    assert bid
