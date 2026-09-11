"""집계와 계산을 섞어 쓰기 — 네 자리가 같은 문법을 본다.

예전에는 숫자·축표·자유표가 `formula.run` 만 불러서 `=함수(대상, 조건...)` 을
**통째로 한 줄**로만 받았다. `=COUNT(지원자)/2` 가 안 됐다. 시트에서는 되는데
숫자 블록에서는 안 되니 문법이 두 가지처럼 보였다.
"""

from __future__ import annotations

import pytest

from cvtool import expr as E
from cvtool import formula as F
from cvtool.dashboards import Block, render_table
from cvtool.sheet import 계산


@pytest.fixture
def rows():
    return F.Rows(
        지원자=[{"부서": "A", "한글_이름": "가", "저널_수": "2", "최종상태": "합격"},
              {"부서": "A", "한글_이름": "나", "저널_수": "4", "최종상태": "불합격"},
              {"부서": "B", "한글_이름": "다", "저널_수": "6", "최종상태": "합격"}],
        채용=[],
    )


아는열 = {"부서", "한글_이름", "저널_수", "최종상태"}


# --- 이미 저장된 수식이 그대로여야 한다 (가장 중요한 회귀) ----------------------
@pytest.mark.parametrize("식,기대", [
    ("=COUNT(지원자)", "3"),
    ('=COUNT(지원자, 부서="A")', "2"),
    ('=PCT(지원자, 부서="A")', "66.7%"),        # 글과 값이 다른 것
    ('=LIST(지원자, 부서="A")', "가, 나"),       # 값이 목록인 것
    ("=AVG(지원자, 저널_수)", "4.0"),           # 소수 한 자리로 굳어 있다
    ("=MAX(지원자, 저널_수)", "6"),
])
def test_통째로_집계_하나면_예전_그대로(rows, 식, 기대):
    """빠른 길이 살아 있나. expr 을 거치면 이 모양들이 무너진다."""
    글, _값 = 계산(식, rows, 아는열)
    assert 글 == 기대


def test_LIST_는_값도_목록으로_돌려준다(rows):
    """format_cell 이 목록을 보고 형식을 안 입힌다 — 그 약속이 안 깨져야 한다."""
    _글, 값 = 계산('=LIST(지원자, 부서="A")', rows, 아는열)
    assert 값 == ["가", "나"]


# --- 섞어 쓰기 --------------------------------------------------------------
@pytest.mark.parametrize("식,기대", [
    ("=COUNT(지원자)/2", "1.5"),
    ("=ROUND(AVG(지원자, 저널_수), 1)", "4"),
    ('=COUNT(지원자, 부서="A") / COUNT(지원자) * 100', "66.6666666667"),
    ('="합계 "&COUNT(지원자)&"명"', "합계 3명"),
    ('=IF(COUNT(지원자)>2, "많음", "적음")', "많음"),
])
def test_집계와_계산이_섞인다(rows, 식, 기대):
    글, _값 = 계산(식, rows, 아는열)
    assert 글 == 기대


def test_수식이_아니면_글자_그대로(rows):
    assert 계산("그냥 글자", rows, 아는열) == ("그냥 글자", None)


# --- 축표·자유표도 같은 길을 쓴다 ----------------------------------------------
def test_자유표에서도_섞인다(rows):
    b = Block(id=1, dashboard_id=1, 순서=1, 종류="표", 제목="",
              설정={"행": ["가"], "열": ["나"],
                  "칸": {"가\t나": "=COUNT(지원자)/2"}})
    결과 = render_table(b, rows, {})
    assert 결과.행 == [("가", ["1.5"])]
    assert 결과.오류 == []


def test_자유표의_옛_수식도_그대로(rows):
    b = Block(id=1, dashboard_id=1, 순서=1, 종류="표", 제목="",
              설정={"행": ["가"], "열": ["나"],
                  "칸": {"가\t나": '=PCT(지원자, 부서="A")'}})
    assert render_table(b, rows, {}).행 == [("가", ["66.7%"])]


# --- 오류는 **친절한 쪽** 말을 쓴다 --------------------------------------------
def test_대상을_잘못_쓰면_대상_이야기를_한다(rows):
    """섞인 길로 흘러가면 "모르는 열입니다" 가 되어 대상을 잘못 쓴 줄 모른다."""
    with pytest.raises(F.FormulaError) as exc:
        계산("=COUNT(없는대상)", rows, 아는열)
    assert "모르는 대상" in str(exc.value)


def test_없는_열은_여전히_막힌다(rows):
    with pytest.raises(F.FormulaError) as exc:
        계산('=COUNT(지원자, 없는열="x")', rows, 아는열)
    assert "없는열" in str(exc.value)


def test_섞인_식의_오류는_그쪽_말을_쓴다(rows):
    """`=COUNT(지원자)/0` 에 "=함수(대상, 조건...) 모양이어야" 는 엉뚱한 말이다."""
    with pytest.raises(E.ExprError) as exc:
        계산("=COUNT(지원자)/0", rows, 아는열)
    assert "0 으로 나눌 수 없습니다" in str(exc.value)


def test_모르는_함수는_쓸_수_있는_이름을_보여준다(rows):
    with pytest.raises(F.FormulaError) as exc:
        계산("=NOPE(지원자)", rows, 아는열)
    assert "COUNTIFS" in str(exc.value)       # 별칭까지 알려준다
