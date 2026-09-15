"""집계 조건의 **값 쪽**에 함수를 쓸 수 있다.

`=COUNTIFS(입사월=9)` 는 2 가 나오는데 `=COUNTIFS(입사월=MONTH(TODAY()))` 는
0 이 나왔다. `formula` 가 값 쪽을 글자로만 읽어서 「MONTH(TODAY()) 라는 글자」를
찾았기 때문이다. **오류도 없이 0** 이라 아무도 틀린 줄 몰랐다.

`=MONTH(TODAY())` 를 따로 적으면 9 가 나오는데 조건 안에 넣으면 안 되니, 같은
수식이 자리에 따라 다르게 돌고 있었다.

여기서 못 박는 성질은 셋이다.
  - 괄호가 있는 값은 **계산한다** (칸 주소도 그 안에서 풀린다)
  - 괄호가 없는 값은 **글자 그대로다** (`번호=10-2020-0012345` 가 뺄셈이 되면 안 된다)
  - 계산하다 터지면 **말해 준다** (조용히 글자로 되돌리면 또 0 이다)
"""

from __future__ import annotations

import pytest

from cvtool import formula as F
from cvtool import sheet as S
from cvtool.timeutil import now_kst


@pytest.fixture
def rows():
    이번달 = f"{now_kst().month}"
    지원자 = [
        {"입사월": 이번달, "부서": "소재분석", "번호": "10-2020-0012345"},
        {"입사월": 이번달, "부서": "소재분석", "번호": ""},
        {"입사월": "99", "부서": "차세대공정", "번호": ""},
    ]
    return F.Rows(지원자=지원자, 채용=지원자[:1])


아는열 = {"입사월", "부서", "번호"}


def _값(식, rows, 값찾기=None):
    return S.계산(식, rows, 아는열, 값찾기=값찾기)[0]


# --- 신고하신 것 -------------------------------------------------------------
def test_조건_값의_함수가_계산된다(rows):
    """신고하신 바로 그 경우. 0 이 아니라 2 여야 한다."""
    assert _값("=COUNTIFS(입사월=MONTH(TODAY()))", rows) == "2"


def test_숫자를_바로_적은_것과_같은_답이다(rows):
    직접 = _값(f"=COUNTIFS(입사월={now_kst().month})", rows)
    함수 = _값("=COUNTIFS(입사월=MONTH(TODAY()))", rows)
    assert 직접 == 함수 == "2"


def test_칸이_없는_자리에서도_돈다(rows):
    """숫자·축표·자유표 블록에도 함수는 있다 (값찾기가 없는 자리)."""
    assert _값("=COUNTIFS(입사월=MONTH(TODAY()))", rows, 값찾기=None) == "2"


def test_더_큰_식_안에서도_돈다(rows):
    assert _값("=COUNTIFS(입사월=MONTH(TODAY()))/2", rows) == "1"


# --- 안 건드리는 것 -----------------------------------------------------------
def test_괄호가_없으면_글자_그대로다(rows):
    """`10-2020-0012345` 가 뺄셈이 되면 멀쩡한 값이 조용히 딴 것을 찾는다."""
    assert _값("=COUNTIFS(번호=10-2020-0012345)", rows) == "1"


def test_따옴표_없는_낱말도_글자_그대로다(rows):
    assert _값("=COUNTIFS(부서=소재분석)", rows) == "2"


def test_따옴표로_감싸면_계산하지_않는다(rows):
    """`부서=\"MONTH(TODAY())\"` 는 그 글자를 찾겠다는 뜻이다."""
    assert _값('=COUNTIFS(부서="MONTH(TODAY())")', rows) == "0"


def test_열_쪽은_안_건드린다(rows):
    """열 이름 자리에 괄호가 있어도 계산하지 않는다 — 열은 열이다."""
    with pytest.raises(Exception):
        S.계산("=COUNTIFS(MONTH(TODAY())=9)", rows, 아는열)


# --- 칸과 함께 ---------------------------------------------------------------
def test_값_안의_칸도_풀린다(rows):
    칸 = {"A1": "99"}                     # 셋째 사람만 99 월이다
    assert _값("=COUNTIFS(입사월=VALUE(A1))", rows, 값찾기=칸.get) == "1"
    칸["A1"] = f"{now_kst().month}"
    assert _값("=COUNTIFS(입사월=VALUE(A1))", rows, 값찾기=칸.get) == "2"


def test_칸_주소_하나는_그대로_칸이다(rows):
    """괄호 규칙이 생겨도 옛 길(`부서=A3`)은 그대로 돈다."""
    assert _값("=COUNTIFS(부서=A3)", rows, 값찾기=lambda a: "소재분석") == "2"


def test_자기_칸을_함수로_가리키면_순환_참조다(rows):
    값, 오류 = S.값들({"A1": {"글": "=COUNTIFS(입사월=VALUE(A1))"}}, rows, 아는열)
    assert 값["A1"] == "?"
    assert "순환 참조" in 오류[0]


# --- 터지면 말해 준다 ---------------------------------------------------------
def test_계산하다_터지면_까닭을_말한다(rows):
    with pytest.raises(S.SheetError) as e:
        S.계산("=COUNTIFS(입사월=MONTH(NOPE()))", rows, 아는열)
    assert "조건 값을 계산하지 못했습니다" in str(e.value)
    assert "NOPE" in str(e.value)


def test_틀린_칸은_물음표와_까닭을_남긴다(rows):
    값, 오류 = S.값들({"B3": {"글": "=COUNTIFS(입사월=MONTH(NOPE()))"}}, rows, 아는열)
    assert 값["B3"] == "?"
    assert "B3: 조건 값을 계산하지 못했습니다" in 오류[0]
