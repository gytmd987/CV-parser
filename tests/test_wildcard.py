"""`*` 와 `?` — 조건에 쓰는 엑셀 와일드카드.

`=부서="*"` 라고 적으면 부서를 안 가리고 전부 나와야 한다. 엑셀에서 조건에
`*` 를 쓰면 그런 뜻이고(COUNTIF), 대시보드를 만드는 사람은 그걸 기대한다.
예전에는 `=` 가 글자를 그대로만 견줘서 아무것도 안 나왔다.
"""

from __future__ import annotations

from cvtool import expr, wildcard
from cvtool import formula as F
from cvtool.dashboards import Block, render_list


# --- 규칙 자체 -----------------------------------------------------------------
def test_star_is_any_number_of_characters():
    assert wildcard.like("차세대공정", "*")
    assert wildcard.like("차세대공정", "차세대*")
    assert wildcard.like("차세대공정", "*공정")
    assert wildcard.like("차세대공정", "*세대*")
    assert not wildcard.like("차세대공정", "소재*")


def test_star_alone_catches_the_empty_value_too():
    """'전체' 라고 적은 사람은 빈 칸도 포함해서 전체를 뜻한다."""
    assert wildcard.like("", "*")
    assert wildcard.like(None, "*")


def test_question_mark_is_exactly_one_character():
    assert wildcard.like("김철", "김?")
    assert not wildcard.like("김철수", "김?")
    assert wildcard.like("김철수", "김??")
    assert wildcard.like("김철수", "김?수")


def test_a_tilde_makes_the_star_an_ordinary_character():
    """별표 자체를 찾을 때. 엑셀도 `~` 를 쓴다."""
    assert wildcard.like("3*4", "3~*4")
    assert not wildcard.like("3x4", "3~*4")
    assert wildcard.like("왜?", "왜~?")
    assert wildcard.like("a~b", "a~~b")


def test_case_does_not_matter():
    assert wildcard.like("POSTECH", "*postech*")


def test_has_tells_a_real_wildcard_from_an_escaped_one():
    assert wildcard.has("*") and wildcard.has("김?") and wildcard.has("a*b")
    assert not wildcard.has("차세대공정")
    assert not wildcard.has("~*")
    assert not wildcard.has("'22.2~'26.8")        # 기간 표기는 패턴이 아니다


def test_the_pattern_is_not_a_regular_expression():
    """정규식을 그대로 열어 주면 안 된다."""
    assert not wildcard.like("aaa", "a+")
    assert wildcard.like("a+", "a+")
    assert not wildcard.like("서울대", "서.대")
    assert wildcard.like("서.대", "서.대")


# --- 행 문맥 (`expr`) -----------------------------------------------------------
값들 = {"부서": "차세대공정", "최종상태": "최종 합격", "한글_이름": "김철",
       "박사_학교": "", "메모": "3*4"}


def test_a_bare_star_is_everyone():
    """지난번에 안 되던 바로 그것."""
    assert expr.evaluate('=부서="*"', 값들) == "TRUE"


def test_an_empty_column_also_matches_a_bare_star():
    assert expr.evaluate('=박사_학교="*"', 값들) == "TRUE"


def test_a_pattern_in_the_middle_of_a_comparison():
    assert expr.evaluate('=최종상태="*합격"', 값들) == "TRUE"
    assert expr.evaluate('=최종상태="서류*"', 값들) == "FALSE"


def test_question_mark_works_in_a_formula_too():
    assert expr.evaluate('=한글_이름="김?"', 값들) == "TRUE"
    assert expr.evaluate('=한글_이름="김??"', 값들) == "FALSE"


def test_not_equal_flips_the_pattern():
    assert expr.evaluate('=부서<>"*"', 값들) == "FALSE"
    assert expr.evaluate('=부서<>"소재*"', 값들) == "TRUE"


def test_an_escaped_star_still_compares_as_a_character():
    assert expr.evaluate('=메모="3~*4"', 값들) == "TRUE"
    assert expr.evaluate('=메모="3*4"', 값들) == "TRUE"       # 이것도 맞기는 맞는다
    assert expr.evaluate('=메모="3~*5"', 값들) == "FALSE"


def test_a_plain_comparison_is_untouched():
    """와일드카드가 없으면 예전 그대로 글자를 견준다."""
    assert expr.evaluate('=부서="차세대공정"', 값들) == "TRUE"
    assert expr.evaluate('=부서="차세대"', 값들) == "FALSE"


def test_the_pattern_may_sit_on_the_left():
    assert expr.evaluate('="*합격"=최종상태', 값들) == "TRUE"


def test_it_works_inside_if():
    assert expr.evaluate('=IF(최종상태="*합격","O","")', 값들) == "O"


# --- 집계 문맥 (`formula`) -------------------------------------------------------
ROWS = F.Rows(
    지원자=[
        {"지원자_ID": "A", "한글_이름": "홍길동", "부서": "차세대공정", "최종상태": "서류 합격"},
        {"지원자_ID": "B", "한글_이름": "김철수", "부서": "소재분석", "최종상태": "최종 합격"},
        {"지원자_ID": "C", "한글_이름": "이영희", "부서": "", "최종상태": "불합격"},
    ],
    채용=[],
)


def test_count_with_a_bare_star_counts_everyone():
    assert F.run('=COUNT(지원자, 부서="*")', ROWS)[0] == "3"


def test_count_with_a_prefix_pattern():
    assert F.run('=COUNT(지원자, 부서="차세대*")', ROWS)[0] == "1"
    assert F.run('=COUNT(지원자, 최종상태="*합격")', ROWS)[0] == "3"


def test_question_mark_in_the_aggregate_context():
    assert F.run('=COUNT(지원자, 부서="소재??")', ROWS)[0] == "1"


def test_not_equal_with_a_pattern():
    assert F.run('=COUNT(지원자, 부서!="*")', ROWS)[0] == "0"
    assert F.run('=COUNT(지원자, 부서!="차세대*")', ROWS)[0] == "2"


def test_the_tilde_operator_still_works():
    """쓰던 `열~"패턴*"` 이 깨지면 안 된다."""
    assert F.run('=COUNT(지원자, 최종상태~"*합격")', ROWS)[0] == "3"
    assert F.run('=COUNT(지원자, 최종상태~"*합격", 최종상태!~"*불합격")', ROWS)[0] == "2"


def test_the_tilde_operator_learned_the_question_mark():
    assert F.run('=COUNT(지원자, 부서~"소재??")', ROWS)[0] == "1"


# --- 대시보드에서 실제로 -----------------------------------------------------------
def test_a_list_block_with_a_star_shows_everyone():
    """사람이 실제로 하는 일: 목록 블록 '행 고르기' 에 `=부서="*"` 를 적는다."""
    b = Block(id=1, dashboard_id=1, 순서=0, 종류="목록", 제목="표",
              설정={"목록대상": "지원자", "목록조건": '=부서="*"',
                   "목록열": [["이름", "=한글_이름"]]})
    r = render_list(b, ROWS)
    assert r.오류 == []
    assert [줄[0] for 줄 in r.행] == ["홍길동", "김철수", "이영희"]


def test_a_list_block_can_filter_by_a_pattern():
    b = Block(id=1, dashboard_id=1, 순서=0, 종류="목록", 제목="표",
              설정={"목록대상": "지원자", "목록조건": '=최종상태="*합격"',
                   "목록열": [["이름", "=한글_이름"]]})
    assert [줄[0] for 줄 in render_list(b, ROWS).행] == ["홍길동", "김철수", "이영희"]

    b.설정["목록조건"] = '=최종상태="*합격"'
    b.설정["목록조건"] = '=AND(최종상태="*합격", 최종상태<>"*불합격")'
    assert [줄[0] for 줄 in render_list(b, ROWS).행] == ["홍길동", "김철수"]
