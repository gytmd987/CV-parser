"""시트 블록 — 담기·거르기·그리기, 그리고 엑셀.

격자·서식·병합이 **브라우저가 보낸 JSON** 으로 온다. 값이 그대로 style 속성과
격자 크기가 되므로, 서버는 그것을 믿지 않고 다시 거른다.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from cvtool.dashboards import (SHEET_MAX_COLS, SHEET_MAX_ROWS, Block,
                               render_sheet, 시트_다듬기, 시트_칸스타일)
from cvtool.export import build_sheet_xlsx
from cvtool.xlsx_read import read_sheet


def _블록(설정) -> Block:
    return Block(id=1, dashboard_id=1, 순서=0, 종류="시트", 제목="시트", 설정=설정)


# --- 거르기 ----------------------------------------------------------------
def test_격자_밖의_칸은_버린다():
    설정 = 시트_다듬기({"행수": 2, "열수": 2,
                   "칸": {"A1": {"글": "안"}, "Z9": {"글": "밖"}}})
    assert "A1" in 설정["시트칸"] and "Z9" not in 설정["시트칸"]


def test_색처럼_안_생긴_값은_버린다():
    """이 값이 그대로 style 속성에 들어간다."""
    설정 = 시트_다듬기({"행수": 1, "열수": 1,
                   "칸": {"A1": {"글": "x", "배경": "red",
                               "글자": "#123456"}}})
    assert "배경" not in 설정["시트칸"]["A1"]
    assert 설정["시트칸"]["A1"]["글자"] == "#123456"


def test_크기는_사이로_자른다():
    for 넣은것, 기대 in ((9999, 48), (1, 8), ("열둘", 8), (0, None), ("", None)):
        설정 = 시트_다듬기({"행수": 1, "열수": 1, "칸": {"A1": {"글": "x", "크기": 넣은것}}})
        assert 설정["시트칸"]["A1"].get("크기") == 기대


def test_모르는_글꼴과_정렬은_버린다():
    설정 = 시트_다듬기({"행수": 1, "열수": 1,
                   "칸": {"A1": {"글": "x", "글꼴": "Comic Sans",
                               "정렬": "justify"}}})
    assert "글꼴" not in 설정["시트칸"]["A1"]
    assert "정렬" not in 설정["시트칸"]["A1"]


def test_격자를_벗어나는_병합은_자른다():
    설정 = 시트_다듬기({"행수": 2, "열수": 3,
                   "칸": {"B1": {"글": "x", "가로병합": 99, "세로병합": 99}}})
    assert 설정["시트칸"]["B1"]["가로병합"] == 2      # B, C 까지만
    assert 설정["시트칸"]["B1"]["세로병합"] == 2


def test_격자_크기도_사이로_자른다():
    설정 = 시트_다듬기({"행수": 9999, "열수": 9999})
    assert (설정["행수"], 설정["열수"]) == (SHEET_MAX_ROWS, SHEET_MAX_COLS)
    비었을때 = 시트_다듬기({})
    assert (비었을때["행수"], 비었을때["열수"]) == (10, 6)


def test_엉뚱한_것이_와도_나머지는_받는다():
    """색 하나 잘못 왔다고 한참 꾸며 둔 시트를 통째로 되돌리면 안 된다."""
    설정 = 시트_다듬기({"행수": 2, "열수": 2, "칸": {
        "A1": {"글": "살아남는다", "굵게": 1},
        "A2": "이건 dict 가 아니다",
        "!!": {"글": "주소가 아니다"},
    }})
    assert 설정["시트칸"]["A1"] == {"글": "살아남는다", "굵게": 1}
    assert set(설정["시트칸"]) == {"A1"}


def test_아무_것도_안_담긴_칸은_아예_안_담는다():
    설정 = 시트_다듬기({"행수": 1, "열수": 1, "칸": {"A1": {"글": "", "크기": 0}}})
    assert 설정["시트칸"] == {}


# --- 그리기 ----------------------------------------------------------------
def test_병합에_덮인_칸은_안_그린다():
    b = _블록(시트_다듬기({"행수": 2, "열수": 3,
                     "칸": {"A1": {"글": "제목", "가로병합": 3}}}))
    결과 = render_sheet(b, [])
    assert [a for a, *_ in 결과.행[0]] == ["A1"]          # B1·C1 은 덮였다
    assert 결과.행[0][0][3] == 3                          # colspan
    assert [a for a, *_ in 결과.행[1]] == ["A2", "B2", "C2"]


def test_세로_병합도_덮는다():
    b = _블록(시트_다듬기({"행수": 3, "열수": 2,
                     "칸": {"A1": {"글": "쭉", "세로병합": 3}}}))
    결과 = render_sheet(b, [])
    assert [a for a, *_ in 결과.행[1]] == ["B2"]
    assert [a for a, *_ in 결과.행[2]] == ["B3"]


def test_칸_스타일이_고른_대로_나온다():
    스타일 = 시트_칸스타일({"배경": "#fff4cc", "글자": "#cc0000", "굵게": 1,
                    "기울임": 1, "크기": 18, "글꼴": "명조", "정렬": "center"})
    for 조각 in ("background:#fff4cc", "color:#cc0000", "font-weight:700",
              "font-style:italic", "font-size:18px", "text-align:center"):
        assert 조각 in 스타일
    assert "Batang" in 스타일


def test_행과_열을_줄여도_값이_안_밀린다():
    """시트의 열은 이름이 아니라 **자리**다. 줄이면 밖으로 나간 칸만 빠진다."""
    설정 = 시트_다듬기({"행수": 3, "열수": 3,
                   "칸": {"A1": {"글": "가"}, "C3": {"글": "다"}}})
    작게 = 시트_다듬기({**{"행수": 2, "열수": 2},
                   "칸": 설정["시트칸"]})
    assert "A1" in 작게["시트칸"] and "C3" not in 작게["시트칸"]


# --- 엑셀 ------------------------------------------------------------------
def _엑셀(설정) -> bytes:
    return build_sheet_xlsx(render_sheet(_블록(설정), []), "보고")


def test_엑셀로_받으면_값이_그대로다():
    설정 = 시트_다듬기({"행수": 2, "열수": 2, "칸": {
        "A1": {"글": "10"}, "B1": {"글": "20"}, "A2": {"글": "=A1+B1"}}})
    # 빈 칸도 <c> 로 나간다 (서식만 걸린 칸이 사라지면 안 된다). 뒤쪽 빈 줄은
    # 읽는 쪽이 털어 낸다.
    assert read_sheet(_엑셀(설정)) == [["10", "20"], ["30", ""]]


def test_엑셀에_색과_굵기가_들어간다():
    설정 = 시트_다듬기({"행수": 1, "열수": 1, "칸": {
        "A1": {"글": "x", "배경": "#FFF4CC", "글자": "#CC0000",
               "굵게": 1, "기울임": 1, "밑줄": 1}}})
    with zipfile.ZipFile(io.BytesIO(_엑셀(설정))) as z:
        styles = z.read("xl/styles.xml").decode()
    for 조각 in ("FFFFF4CC", "FFCC0000", "<b/>", "<i/>", "<u/>"):
        assert 조각 in styles, 조각


def test_엑셀에_병합이_들어간다():
    설정 = 시트_다듬기({"행수": 2, "열수": 3,
                   "칸": {"A1": {"글": "제목", "가로병합": 3, "세로병합": 2}}})
    with zipfile.ZipFile(io.BytesIO(_엑셀(설정))) as z:
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
    assert 'ref="A1:C2"' in sheet


def test_서식을_하나도_안_쓴_시트도_열린다():
    """빈 fonts/fills 로 깨지면 안 된다."""
    설정 = 시트_다듬기({"행수": 2, "열수": 2, "칸": {"A1": {"글": "그냥"}}})
    데이터 = _엑셀(설정)
    assert read_sheet(데이터) == [["그냥", ""]]
    with zipfile.ZipFile(io.BytesIO(데이터)) as z:
        assert z.read("xl/styles.xml").decode().count("<font>") >= 1


def test_열_너비가_엑셀에도_간다():
    설정 = 시트_다듬기({"행수": 1, "열수": 2, "칸": {"A1": {"글": "x"}},
                   "열너비": {"A": "210"}})
    with zipfile.ZipFile(io.BytesIO(_엑셀(설정))) as z:
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
    assert "<cols>" in sheet and 'min="1"' in sheet
