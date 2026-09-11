"""더한 함수들.

이름이 겹치는 것들(SUM·MIN·MAX·COUNT·AVERAGE)은 **첫 인자가 대상이냐**로
갈린다 — `COUNT(A1:A5)` 는 칸을 세고 `COUNT(지원자)` 는 사람을 센다.
"""

from __future__ import annotations

import pytest

from cvtool import expr as E
from cvtool import formula as F
from cvtool import sheet as S


@pytest.fixture
def rows():
    return F.Rows(
        지원자=[{"부서": "A", "최종상태": "합격", "저널_수": "2"},
              {"부서": "A", "최종상태": "불합격", "저널_수": "4"},
              {"부서": "B", "최종상태": "합격", "저널_수": "6"}],
        채용=[],
    )


아는열 = {"부서", "최종상태", "저널_수"}
칸 = {"A1": "3", "A2": "", "A3": "7", "A4": "글자"}


# --- 엑셀 이름의 조건 집계 -----------------------------------------------------
def test_엑셀_이름이_원래_이름과_같은_답을_낸다(rows):
    for 별칭, 원래 in (("COUNTIF", "COUNT"), ("COUNTIFS", "COUNT"),
                    ("SUMIFS", "SUM"), ("AVERAGEIFS", "AVG"), ("AVERAGE", "AVG")):
        꼬리 = ", 저널_수" if 원래 in ("SUM", "AVG") else ""
        assert (F.run(f'={별칭}(지원자{꼬리}, 부서="A")', rows, 아는열)
                == F.run(f'={원래}(지원자{꼬리}, 부서="A")', rows, 아는열))


def test_COUNTIFS_는_조건을_여러_개_받는다(rows):
    """원래부터 AND 였다. 익숙한 이름을 얹었을 뿐이다."""
    글, 값 = F.run('=COUNTIFS(지원자, 부서="A", 최종상태="합격")', rows, 아는열)
    assert (글, 값) == ("1", 1)


def test_부를_수_있는_이름에_별칭이_들어_있다():
    """자동완성이 이 목록을 그대로 보여준다 — 두 군데 적지 않는다."""
    assert set(F.FUNCTIONS) <= set(F.CALLABLE)
    for n in ("COUNTIF", "COUNTIFS", "SUMIF", "SUMIFS", "AVERAGE", "AVERAGEIFS"):
        assert n in F.CALLABLE


# --- 범위 셈 ----------------------------------------------------------------
@pytest.mark.parametrize("식,기대", [
    ("=AVERAGE(A1,A2,A3)", "5"),        # 빈 칸은 건너뛴다 (3+7)/2
    ("=MEDIAN(A1,A2,A3)", "5"),
    ("=COUNT(A1,A2,A3,A4)", "2"),       # 숫자만
    ("=COUNTA(A1,A2,A3,A4)", "3"),      # 빈 칸 빼고 전부
])
def test_범위_셈(식, 기대):
    assert E.evaluate(식, 칸) == 기대


def test_빈_칸은_평균을_안_끌어내린다():
    """0 으로 치면 평균이 내려앉는다. 엑셀도 건너뛴다."""
    assert E.evaluate("=AVERAGE(A1,A2,A3)", 칸) == "5"
    assert E.evaluate("=AVERAGE(A1,A3)", 칸) == "5"


def test_셀_숫자가_없으면_말해_준다():
    with pytest.raises(E.ExprError) as exc:
        E.evaluate("=AVERAGE(A2,A4)", 칸)
    assert "셀 숫자가 없습니다" in str(exc.value)


# --- 숫자 -------------------------------------------------------------------
@pytest.mark.parametrize("식,기대", [
    ("=ROUNDUP(2.31,1)", "2.4"), ("=ROUNDUP(2.0,1)", "2"),
    ("=ROUNDDOWN(2.39,1)", "2.3"), ("=ROUNDDOWN(-2.31,1)", "-2.4"),
    ("=MOD(7,3)", "1"), ("=POWER(2,10)", "1024"), ("=SQRT(16)", "4"),
])
def test_숫자_함수(식, 기대):
    assert E.evaluate(식, {}) == 기대


def test_0_으로_나누기와_음수_제곱근은_막는다():
    for 식, 말 in (("=MOD(5,0)", "0"), ("=SQRT(-1)", "음수")):
        with pytest.raises(E.ExprError) as exc:
            E.evaluate(식, {})
        assert 말 in str(exc.value)


# --- 글자·판단 ---------------------------------------------------------------
@pytest.mark.parametrize("식,기대", [
    ('=FIND("공학","기계공학과")', "3"),
    ('=FIND("없음","가나다")', "0"),            # 못 찾으면 0 (엑셀은 오류)
    ('=FIND("ABC","xxabcyy")', "0"),          # 대소문자를 가린다
    ('=SEARCH("ABC","xxabcyy")', "3"),        # 안 가린다
    ('=REPLACE("20260101",5,2,"12")', "20261201"),
    ('=EXACT("가","가")', "TRUE"),
    ('=EXACT("가","나")', "FALSE"),
    ('=SWITCH("을","갑","1등","을","2등","기타")', "2등"),
    ('=SWITCH("병","갑","1등","을","2등","기타")', "기타"),
    ("=ISNUMBER(A1)", "TRUE"), ("=ISNUMBER(A4)", "FALSE"),
    ("=ISTEXT(A4)", "TRUE"), ("=ISTEXT(A1)", "FALSE"),
    ("=ISTEXT(A2)", "FALSE"),                 # 빈 칸은 글자가 아니다
])
def test_글자와_판단_함수(식, 기대):
    assert E.evaluate(식, 칸) == 기대


def test_못_찾으면_0_이라_IF_로_이어_쓸_수_있다():
    """오류를 내면 칸 하나가 통째로 `?` 가 되어 IF 를 쓸 수가 없다."""
    assert E.evaluate('=IF(FIND("공학","기계공학과")>0,"공학","아님")', {}) == "공학"
    assert E.evaluate('=IF(FIND("공학","경영학과")>0,"공학","아님")', {}) == "아님"


# --- 이름이 겹치는 것은 첫 인자로 갈린다 -----------------------------------------
@pytest.mark.parametrize("이름,칸답,사람답", [
    ("COUNT", "2", "3"), ("SUM", "10", "12"),
    ("MIN", "3", "2"), ("MAX", "7", "6"), ("AVERAGE", "5", "4.0"),
])
def test_첫_인자가_대상이냐로_갈린다(rows, 이름, 칸답, 사람답):
    칸들 = {"A1": {"글": "3"}, "A2": {"글": ""}, "A3": {"글": "7"},
          "B1": {"글": f"={이름}(A1:A3)"},
          "B2": {"글": f"={이름}(지원자, 저널_수)" if 이름 != "COUNT"
                 else f"={이름}(지원자)"}}
    값, 오류 = S.값들(칸들, rows, 아는열)
    assert 오류 == []
    assert 값["B1"] == 칸답, f"{이름} 칸 셈"
    assert 값["B2"] == 사람답, f"{이름} 사람 셈"


def test_새_이름이_자동완성_목록에_다_들어간다():
    """목록을 따로 적지 않는다 — FUNCS 표가 곧 목록이다."""
    for n in ("AVERAGE", "MEDIAN", "COUNTA", "ROUNDUP", "ROUNDDOWN", "MOD",
              "POWER", "SQRT", "FIND", "SEARCH", "REPLACE", "EXACT",
              "SWITCH", "ISNUMBER", "ISTEXT", "COUNT"):
        assert n in E.FUNC_NAMES, n
