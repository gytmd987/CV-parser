"""행 문맥 수식 (`cvtool/expr.py`) — 엑셀 문법으로 한 사람의 값을 글로."""

from __future__ import annotations

import pytest

from cvtool import expr
from cvtool.expr import ExprError


값들 = {
    "한글_이름": "홍길동",
    "영문_이름": "Gildong Hong",
    "생년월일": "19980312",
    "박사_학교": "서울대학교",
    "박사_전공": "기계공학",
    "박사_시작": "202202",
    "박사_졸업": "202608",
    "석사_학교": "",
    "석사_전공": "",
    "학사_학교": "포항공과대학교",
    "박사_석박통합": "석박통합",
    "저널_수": "12",
    "저널_주저자_수": "4",
    "경력_종료": "재직중",
    "연구분야_키워드": "플라즈마 식각, 박막 증착",
}


def 계산(수식: str) -> str:
    return expr.evaluate(수식, 값들)


# --- 잇기 --------------------------------------------------------------------
def test_ampersand_joins_like_excel():
    assert 계산('=박사_학교 & " " & 박사_전공') == "서울대학교 기계공학"


def test_numbers_join_without_a_decimal_point():
    """12.0 을 '12' 로. 엑셀도 정수에 소수점을 안 붙인다."""
    assert 계산('=한글_이름 & "(" & 저널_주저자_수 & "편)"') == "홍길동(4편)"
    assert 계산("=저널_수 + 1") == "13"


# --- TEXT — 지난번 질문 두 개가 여기서 끝난다 -------------------------------------
def test_m_is_one_digit_and_mm_is_two():
    """'08 을 8 로 보이게 하는 법' 이 바로 이것이다. 엑셀 규칙 그대로."""
    assert 계산('=TEXT(박사_졸업,"\'yy.m")') == "'26.8"
    assert 계산('=TEXT(박사_졸업,"\'yy.mm")') == "'26.08"


def test_the_year_can_be_two_or_four_digits():
    assert 계산('=TEXT(박사_졸업,"yyyy.mm")') == "2026.08"
    assert 계산('=TEXT(박사_졸업,"yy.mm")') == "26.08"


def test_letters_outside_the_format_codes_pass_through():
    assert 계산('=TEXT(박사_졸업,"yyyy년 m월")') == "2026년 8월"


def test_a_period_is_two_texts_joined():
    assert (계산('=TEXT(박사_시작,"\'yy.m") & "~" & TEXT(박사_졸업,"\'yy.m")')
            == "'22.2~'26.8")


def test_still_working_stays_as_written():
    """'재직중' 을 날짜로 읽으려 들면 안 된다."""
    assert 계산('=TEXT(경력_종료,"\'yy.m")') == "재직중"


def test_an_unreadable_value_comes_back_untouched():
    """지어내지 않는다."""
    assert expr.evaluate('=TEXT(어디,"yyyy")', {"어디": "미상"}) == "미상"


def test_a_year_only_value_has_no_month():
    assert expr.evaluate('=TEXT(그때,"yyyy.mm")', {"그때": "2026"}) == "2026."
    assert expr.evaluate('=TEXT(그때,"yyyy")', {"그때": "2026"}) == "2026"


def test_month_zero_means_unknown_not_zero():
    """YYYY00 은 '월을 모른다' 는 뜻이라 0 월로 쓰면 안 된다."""
    assert expr.evaluate('=TEXT(그때,"yyyy.m")', {"그때": "202600"}) == "2026."


# --- 빈 값 --------------------------------------------------------------------
def test_textjoin_skips_the_blanks():
    """빈 조각이 사라지는 규칙은 엑셀에 이미 있다 — 두 번째 인자."""
    assert (계산('=TEXTJOIN(" / ", TRUE, 박사_학교, 석사_학교, 학사_학교)')
            == "서울대학교 / 포항공과대학교")


def test_textjoin_can_keep_the_blanks_too():
    assert (계산('=TEXTJOIN("/", FALSE, 박사_학교, 석사_학교)')
            == "서울대학교/")


def test_if_splits_on_an_empty_value():
    assert 계산('=IF(석사_학교="","",석사_학교 & " 석사")') == ""
    assert 계산('=IF(박사_학교="","",박사_학교 & " 박사")') == "서울대학교 박사"


def test_a_value_decides_the_prefix():
    """'석박통합이면 석/박), 따로면 박)' — 지난번에 말한 그것."""
    assert 계산('=IF(박사_석박통합="석박통합","석/박)","박)")') == "석/박)"
    assert expr.evaluate('=IF(박사_석박통합="석박통합","석/박)","박)")',
                         {"박사_석박통합": ""}) == "박)"


# --- 나이 ---------------------------------------------------------------------
def test_age_from_the_birth_date():
    올해 = int(expr.evaluate("=YEAR(TODAY())", {}))
    assert 계산("=YEAR(TODAY())-VALUE(LEFT(생년월일,4))") == str(올해 - 1998)


def test_a_whole_line_the_way_it_was_asked_for():
    """지난번에 보여주신 예시가 그대로 나와야 한다."""
    올해 = int(expr.evaluate("=YEAR(TODAY())", {}))
    수식 = ('=한글_이름 & "(" & (YEAR(TODAY())-VALUE(LEFT(생년월일,4))) & "세)"'
          ' & "/" & 저널_주저자_수 & "저자"')
    assert 계산(수식) == f"홍길동({올해 - 1998}세)/4저자"


def test_the_education_line_from_the_example():
    수식 = ('=박사_학교 & " " & 박사_전공 & " (" & TEXT(박사_시작,"\'yy.m")'
          ' & "~" & TEXT(박사_졸업,"\'yy.m") & ")"')
    assert 계산(수식) == "서울대학교 기계공학 ('22.2~'26.8)"


# --- 글자 다루기 ----------------------------------------------------------------
def test_the_text_helpers():
    assert 계산("=LEFT(한글_이름,1)") == "홍"
    assert 계산("=RIGHT(영문_이름,4)") == "Hong"
    assert 계산("=MID(생년월일,5,2)") == "03"
    assert 계산("=LEN(한글_이름)") == "3"
    assert 계산("=UPPER(영문_이름)") == "GILDONG HONG"
    assert 계산('=SUBSTITUTE(연구분야_키워드,", "," · ")') == "플라즈마 식각 · 박막 증착"


def test_mid_counts_from_one_like_excel():
    with pytest.raises(ExprError, match="1부터"):
        expr.evaluate("=MID(한글_이름,0,2)", 값들)


# --- 셈 ----------------------------------------------------------------------
def test_arithmetic_and_precedence():
    assert expr.evaluate("=2+3*4", {}) == "14"
    assert expr.evaluate("=(2+3)*4", {}) == "20"
    assert expr.evaluate("=-3+10", {}) == "7"
    assert expr.evaluate("=ROUND(10/3,1)", {}) == "3.3"


def test_comparison_gives_true_or_false():
    assert 계산("=저널_수 > 10") == "TRUE"
    assert 계산('=박사_학교 = "서울대학교"') == "TRUE"
    assert 계산('=박사_학교 <> "서울대학교"') == "FALSE"


def test_and_or_not():
    assert 계산('=IF(AND(저널_수>10, 박사_학교<>""),"많음","")') == "많음"
    assert 계산("=NOT(저널_수>100)") == "TRUE"


# --- 틀렸을 때 ------------------------------------------------------------------
def test_an_unknown_column_is_an_error_not_a_blank():
    """조용히 빈칸으로 두면 값이 없는 건지 이름을 틀린 건지 알 수 없다."""
    with pytest.raises(ExprError, match="모르는 열"):
        expr.evaluate("=박사_학굔", 값들)


def test_an_unknown_function_says_what_can_be_used():
    with pytest.raises(ExprError) as e:
        expr.evaluate("=VLOOKUP(1)", 값들)
    assert "모르는 함수" in str(e.value) and "TEXTJOIN" in str(e.value)


def test_unbalanced_quotes_and_parens_are_caught():
    with pytest.raises(ExprError, match="따옴표"):
        expr.evaluate('=TEXT(박사_졸업,"yyyy)', 값들)
    with pytest.raises(ExprError):
        expr.evaluate("=TEXT(박사_졸업", 값들)


def test_dividing_by_zero_says_so():
    with pytest.raises(ExprError, match="0 으로"):
        expr.evaluate("=1/0", {})


def test_iferror_catches_it():
    assert expr.evaluate('=IFERROR(1/0,"-")', {}) == "-"
    assert expr.evaluate('=IFERROR(모르는열,"-")', {}) == "-"


def test_render_never_blows_up_the_page():
    글, 오류 = expr.render("=박사_학굔", 값들)
    assert 글 == "" and "모르는 열" in 오류


# --- 검사 ---------------------------------------------------------------------
def test_columns_lists_what_the_formula_reads():
    쓴것 = expr.columns('=박사_학교 & TEXT(박사_졸업,"yyyy") & "글자"')
    assert 쓴것 == ["박사_학교", "박사_졸업"]


def test_true_and_false_are_not_columns():
    assert expr.columns('=TEXTJOIN("/", TRUE, 박사_학교)') == ["박사_학교"]


def test_validate_blocks_a_typo_before_it_is_saved():
    with pytest.raises(ExprError, match="모르는 열"):
        expr.validate("=박사_학굔", set(값들))
    assert expr.validate("=박사_학교", set(값들)) == ["박사_학교"]


def test_a_column_name_with_spaces_uses_brackets():
    assert expr.evaluate("=[이름 두 개]", {"이름 두 개": "값"}) == "값"


def test_is_formula():
    assert expr.is_formula("=박사_학교")
    assert expr.is_formula("  =박사_학교")
    assert not expr.is_formula("{박사_학교}")
    assert not expr.is_formula("")


# --- 프로필 양식에 붙었는가 -------------------------------------------------------
def test_profile_lines_accept_formulas():
    from cvtool import profile_form as P

    수식 = ('=박사_학교 & " " & 박사_전공 & " (" & TEXT(박사_시작,"\'yy.m")'
          ' & "~" & TEXT(박사_졸업,"\'yy.m") & ")"')
    assert P.render_line(수식, 값들) == "서울대학교 기계공학 ('22.2~'26.8)"


def test_the_old_slot_templates_still_work():
    """쓰던 양식이 깨지면 안 된다. 둘을 같이 두는 이유가 그것뿐이다."""
    from cvtool import profile_form as P

    옛것 = "{박사_학교} {박사_전공}({기간:박사_시작~박사_졸업})"
    assert P.render_line(옛것, 값들) == "서울대학교 기계공학('22.2~'26.8)"


def test_a_broken_formula_shows_up_instead_of_going_blank():
    from cvtool import profile_form as P

    나온것 = P.render_line("=박사_학굔", 값들)
    assert "수식 오류" in 나온것 and "모르는 열" in 나온것


def test_profile_columns_reads_formulas_too():
    from cvtool import profile_form as P

    assert P.columns('=박사_학교 & 박사_전공') == ["박사_학교", "박사_전공"]
    assert P.columns("{박사_학교}") == ["박사_학교"]


def test_an_empty_formula_line_disappears():
    """석사를 안 한 사람 프로필에 빈 석사 줄이 남으면 안 된다."""
    from cvtool import profile_form as P

    줄틀 = [("박사", '=박사_학교'), ("석사", '=IF(석사_학교="","",석사_학교)')]
    assert P.render_rows(줄틀, 값들) == [("박사", "서울대학교")]


# --- 줄바꿈 ---------------------------------------------------------------------
def test_a_line_break_is_char_ten_like_excel():
    """수식 입력칸이 한 줄짜리라 엔터를 칠 수 없다. 엑셀도 이 방법을 쓴다."""
    assert 계산("=박사_학교 & CHAR(10) & 학사_학교") == "서울대학교\n포항공과대학교"


def test_textjoin_stacks_lines_and_skips_the_blanks():
    assert (계산("=TEXTJOIN(CHAR(10), TRUE, 박사_학교, 석사_학교, 학사_학교)")
            == "서울대학교\n포항공과대학교")


def test_char_and_code_are_a_pair():
    assert expr.evaluate('=CODE("A")', {}) == "65"
    assert expr.evaluate("=CHAR(65)", {}) == "A"
