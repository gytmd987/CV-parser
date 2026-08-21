"""대시보드 — 수식 언어 · 문장 틀 · 블록 저장.

가장 중요한 성질 둘을 지킨다.
  - **SQL 이 없다.** 할 수 있는 일이 정해진 수식만 돈다.
  - **없는 열은 저장 단계에서 막힌다.** 그대로 저장되면 화면에는 0 이 뜨고
    아무도 틀린 줄 모른다.
"""

from __future__ import annotations

import pytest

from cvtool import formula as F
from cvtool import profile_form as P
from cvtool.dashboards import (
    Block,
    DashboardStore,
    format_cell,
    render_profile,
    render_table,
)


@pytest.fixture
def rows():
    지원자 = [
        {"지원자_ID": "A", "한글_이름": "홍길동", "부서": "차세대공정",
         "최종상태": "최종 합격", "서류 검토": "합격", "저널_수": "3",
         "등록년도": "2026"},
        {"지원자_ID": "B", "한글_이름": "김철수", "부서": "차세대공정",
         "최종상태": "서류 검토 불합격", "서류 검토": "불합격", "저널_수": "1",
         "등록년도": "2026"},
        {"지원자_ID": "C", "한글_이름": "이영희", "부서": "소재분석",
         "최종상태": "미시작", "서류 검토": "", "저널_수": "", "등록년도": "2025"},
    ]
    return F.Rows(지원자=지원자, 채용=지원자[:2])


# --- 수식 -------------------------------------------------------------------
def test_count_and_conditions(rows):
    assert F.run("=COUNT(지원자)", rows)[0] == "3"
    assert F.run('=COUNT(채용, 부서="차세대공정")', rows)[0] == "2"
    assert F.run('=COUNT(지원자, 부서="소재분석")', rows)[0] == "1"


def test_target_picks_the_right_group(rows):
    """지원자는 인재 Pool 전체, 채용은 채용 시작한 사람."""
    assert F.run("=COUNT(지원자)", rows)[0] == "3"
    assert F.run("=COUNT(채용)", rows)[0] == "2"


def test_like_and_not_like(rows):
    assert F.run('=COUNT(지원자, 최종상태~"*합격")', rows)[0] == "2"
    assert F.run('=COUNT(지원자, 최종상태~"*합격", 최종상태!~"*불합격")', rows)[0] == "1"


def test_number_comparison_skips_blanks(rows):
    """숫자가 아닌 칸은 조건에 안 맞는 것으로 본다 (0 으로 치지 않는다)."""
    assert F.run("=COUNT(지원자, 저널_수>=2)", rows)[0] == "1"
    assert F.run("=COUNT(지원자, 저널_수>=0)", rows)[0] == "2"   # 빈칸 하나는 빠진다


def test_pct_is_against_the_same_target(rows):
    assert F.run('=PCT(채용, 부서="차세대공정")', rows)[0] == "100.0%"
    assert F.run('=PCT(지원자, 부서="차세대공정")', rows)[0] == "66.7%"


def test_aggregates(rows):
    assert F.run("=AVG(지원자, 저널_수)", rows)[0] == "2.0"
    assert F.run("=SUM(지원자, 저널_수)", rows)[0] == "4"
    assert F.run("=MAX(지원자, 저널_수)", rows)[0] == "3"
    assert F.run("=MIN(지원자, 저널_수)", rows)[0] == "1"


def test_aggregate_with_no_numbers_is_a_dash(rows):
    빈것 = F.Rows(지원자=[{"저널_수": ""}], 채용=[])
    assert F.run("=AVG(지원자, 저널_수)", 빈것)[0] == "-"


def test_list_names(rows):
    assert F.run('=LIST(채용, 부서="차세대공정")', rows)[0] == "홍길동, 김철수"
    assert F.run("=LIST(지원자, 열=지원자_ID)", rows)[1] == ["A", "B", "C"]


def test_unknown_function_is_refused(rows):
    with pytest.raises(F.FormulaError, match="모르는 함수"):
        F.run("=SELECT(지원자)", rows)


def test_unknown_target_is_refused(rows):
    with pytest.raises(F.FormulaError, match="모르는 대상"):
        F.run("=COUNT(users)", rows)


def test_unknown_column_is_refused_at_save_time(rows):
    """LLM 이 가장 자주 하는 실수. 그대로 저장되면 화면엔 0 만 뜬다."""
    with pytest.raises(F.FormulaError, match="표에 없는 열"):
        F.validate('=COUNT(지원자, 없는열="x")', {"부서", "최종상태"})
    F.validate('=COUNT(지원자, 부서="x")', {"부서", "최종상태"})   # 있는 열은 통과


def test_sql_is_not_a_formula(rows):
    for 글 in ("SELECT * FROM candidates", "=DROP TABLE x", "=COUNT(지원자"):
        with pytest.raises(F.FormulaError):
            F.run(글, rows)


def test_plain_text_is_not_a_formula():
    assert not F.is_formula("합계")
    assert F.is_formula("  =COUNT(지원자)")


# --- 형식 -------------------------------------------------------------------
def test_cell_formats():
    assert format_cell("3", 3, "명") == "3명"
    assert format_cell("1234", 1234, "쉼표") == "1,234"
    assert format_cell("66.66", 66.66, "퍼센트") == "66.7%"
    assert format_cell("3", 3, "그대로") == "3"
    assert format_cell("홍길동, 김철수", ["홍길동"], "정수") == "홍길동, 김철수"


# --- 문장 틀 ----------------------------------------------------------------
def test_period_is_shortened():
    값 = {"박사_학교": "서울대학교", "박사_전공": "기계공학",
         "박사_시작": "202202", "박사_졸업": "202602"}
    틀 = "{박사_학교} {박사_전공}({기간:박사_시작~박사_졸업})"
    assert P.render(틀, 값) == "서울대학교 기계공학('22.2~'26.2)"


def test_ongoing_period_says_now():
    값 = {"경력_회사": "한국대학교", "직책": "박사후연구원",
         "경력_시작": "202605", "경력_종료": "재직중"}
    틀 = "{경력_회사}/{직책}({기간:경력_시작~경력_종료})"
    assert P.render(틀, 값) == "한국대학교/박사후연구원('26.5~현재)"


def test_empty_slot_takes_its_brackets_with_it():
    """`서울대 ('22.2~)` 같은 반쪽은 아무도 안 본다."""
    틀 = "{박사_학교} {박사_전공}({기간:박사_시작~박사_졸업})"
    assert P.render(틀, {"박사_학교": "서울대"}) == "서울대"


def test_empty_line_disappears_entirely():
    """석사를 안 한 사람 프로필에 빈 석사 줄이 남으면 안 된다."""
    틀 = ("{박사_학교} {박사_전공}\n{석사_학교} {석사_전공}")
    assert P.render(틀, {"박사_학교": "서울대", "박사_전공": "기계"}) == "서울대 기계"


def test_separators_next_to_an_empty_slot_go_away():
    assert P.render_line("{a} · {b} · {c}", {"a": "하나", "c": "셋"}) == "하나 · 셋"
    assert P.render_line("{a}, {b}", {"b": "둘"}) == "둘"


def test_separators_that_had_no_empty_slot_stay():
    """멀쩡한 구분자까지 지우면 안 된다."""
    assert P.render_line("논문 {수:a}편 · 학회 {수:b}편",
                         {"a": "3", "b": "5"}) == "논문 3편 · 학회 5편"


def test_count_slot_shows_zero_but_does_not_keep_the_line_alive():
    """`매칭 (0점)` 만 덩그러니 남던 문제.

    `{수:}` 는 값이 비면 0 을 보여주지만, **그 0 이 줄을 살려 두면 안 된다.**
    다른 값이 하나라도 있을 때만 0 이 보인다.
    """
    assert P.render_line("특허 {수:특허}건", {"특허": "0"}) == "특허 0건"
    assert P.render_line("{매칭_과제} ({수:매칭_점수}점)", {}) == ""
    assert P.render_line("저널 {수:a}편 · 학회 {수:b}편",
                         {"a": "3"}) == "저널 3편 · 학회 0편"


def test_template_columns_are_listed_for_checking():
    틀 = "{박사_학교}({기간:박사_시작~박사_졸업}) {수:저널_수}"
    assert set(P.columns(틀)) == {"박사_학교", "박사_시작", "박사_졸업", "저널_수"}


# --- 블록 저장·계산 ---------------------------------------------------------
@pytest.fixture
def store(tmp_path):
    return DashboardStore(tmp_path / "dash.db")


def test_dashboard_and_blocks_round_trip(store):
    did = store.add("현황판", 만든이="admin")
    bid = store.add_block(did, "숫자", 제목="채용 중",
                          설정={"수식": "=COUNT(채용)"})
    b = store.block(bid)
    assert b.수식 == "=COUNT(채용)" and b.제목 == "채용 중"
    assert [x.id for x in store.blocks(did)] == [bid]


def test_duplicate_dashboard_name_is_refused(store):
    store.add("현황판")
    with pytest.raises(ValueError):
        store.add("현황판")


def test_blocks_can_be_reordered(store):
    did = store.add("현황판")
    a = store.add_block(did, "글", 제목="가")
    b = store.add_block(did, "글", 제목="나")
    store.move_block(b, -1)
    assert [x.id for x in store.blocks(did)] == [b, a]
    store.move_block(b, -1)                       # 맨 위에서 더 올려도 안전
    assert [x.id for x in store.blocks(did)] == [b, a]


def test_copy_takes_the_blocks_along(store):
    did = store.add("원본")
    store.add_block(did, "글", 제목="가")
    store.add_block(did, "숫자", 제목="나", 설정={"수식": "=COUNT(지원자)"})
    새 = store.copy(did, "사본")
    assert [x.제목 for x in store.blocks(새)] == ["가", "나"]


def test_unknown_block_kind_is_refused(store):
    did = store.add("현황판")
    with pytest.raises(ValueError):
        store.add_block(did, "그래프")


def test_axis_table_repeats_one_formula(rows):
    b = Block(id=1, dashboard_id=1, 순서=1, 종류="축표", 제목="부서×단계",
              설정={"행축": "부서", "열축": "단계",
                   "칸수식": '=COUNT(채용, 부서="{행}", {열}="합격")'})
    결과 = render_table(b, rows, {"부서": ["차세대공정", "소재분석"],
                                "단계": ["서류 검토"]})
    assert 결과.머리 == ["서류 검토"]
    assert 결과.행 == [("차세대공정", ["1"]), ("소재분석", ["0"])]
    assert not 결과.오류


def test_axis_table_grows_with_the_organisation(rows):
    """부서가 하나 늘면 표가 알아서 한 줄 는다. 손댈 게 없다."""
    b = Block(id=1, dashboard_id=1, 순서=1, 종류="축표", 제목="",
              설정={"행축": "부서", "열축": "직접 입력", "열": ["인원"],
                   "칸수식": '=COUNT(지원자, 부서="{행}")'})
    앞 = render_table(b, rows, {"부서": ["차세대공정"]})
    뒤 = render_table(b, rows, {"부서": ["차세대공정", "소재분석", "신설부서"]})
    assert len(앞.행) == 1 and len(뒤.행) == 3
    assert 뒤.행[-1] == ("신설부서", ["0"])


def test_free_table_uses_per_cell_formulas(rows):
    b = Block(id=1, dashboard_id=1, 순서=1, 종류="표", 제목="",
              설정={"행": ["접수"], "열": ["2026", "2025"],
                   "칸": {"접수\t2026": '=COUNT(지원자, 등록년도="2026")',
                         "접수\t2025": '=COUNT(지원자, 등록년도="2025")'}})
    결과 = render_table(b, rows, {})
    assert 결과.행 == [("접수", ["2", "1"])]


def test_plain_text_cells_pass_through(rows):
    b = Block(id=1, dashboard_id=1, 순서=1, 종류="표", 제목="",
              설정={"행": ["메모"], "열": ["내용"], "칸": {"메모\t내용": "직접 적은 글"}})
    assert render_table(b, rows, {}).행 == [("메모", ["직접 적은 글"])]


def test_a_broken_formula_shows_a_question_mark_not_a_zero(rows):
    """조용히 0 을 띄우면 아무도 틀린 줄 모른다."""
    b = Block(id=1, dashboard_id=1, 순서=1, 종류="표", 제목="",
              설정={"행": ["가"], "열": ["나"], "칸": {"가\t나": "=COUNT(없는대상)"}})
    결과 = render_table(b, rows, {})
    assert 결과.행 == [("가", ["?"])]
    assert 결과.오류 and "모르는 대상" in 결과.오류[0]


def test_profile_block_renders_one_card_per_person(rows):
    값 = {
        "A": {"한글_이름": "홍길동", "박사_학교": "서울대학교", "박사_전공": "기계공학",
              "박사_시작": "202202", "박사_졸업": "202602"},
        "B": {"한글_이름": "김철수", "박사_학교": "한국대학교", "박사_전공": "재료공학"},
    }
    b = Block(id=1, dashboard_id=1, 순서=1, 종류="프로필", 제목="후보",
              설정={"대상": '=LIST(채용)', "머리": "{한글_이름}",
                   "줄": [["학력", "{박사_학교} {박사_전공}"
                                "({기간:박사_시작~박사_졸업})"]]})
    결과 = render_profile(b, rows, lambda cid: 값.get(cid, {}))
    assert [머리 for 머리, _ in 결과.사람] == ["홍길동", "김철수"]
    assert 결과.사람[0][1] == [("학력", "서울대학교 기계공학('22.2~'26.2)")]
    assert 결과.사람[1][1] == [("학력", "한국대학교 재료공학")]   # 기간이 없으면 괄호도 없다
