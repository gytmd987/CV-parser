"""목록 표 — **한 사람이 한 줄, 열은 만드는 사람이 정한다.**

축표(피벗)로는 만들 수 없는 표가 대부분이다. "채용 중인 사람을 줄로 놓고 옆에
이것저것 붙이고 싶다" 가 사람들이 실제로 만들려는 것이고, 그건 집계가 아니라
목록이다.
"""

from __future__ import annotations

import pytest

from cvtool.dashboards import Block, render_list
from cvtool.formula import Rows


def 사람(cid, 이름, 학교="", 전공="", 시작="", 졸업="", 주=0, 상태="", 부서=""):
    return {
        "지원자_ID": cid, "한글_이름": 이름, "박사_학교": 학교, "박사_전공": 전공,
        "박사_시작": 시작, "박사_졸업": 졸업, "저널_주저자_수": str(주),
        "최종상태": 상태, "부서": 부서,
    }


ROWS = Rows(
    지원자=[
        사람("A", "홍길동", "서울대학교", "기계공학", "202202", "202608", 4, "서류 합격", "공정"),
        사람("B", "김철수", "KAIST", "전기공학", "202003", "202402", 7, "최종 합격", "공정"),
        사람("C", "이영희", "포항공대", "화학공학", "202109", "202509", 2, "불합격", "소재"),
    ],
    채용=[
        사람("A", "홍길동", "서울대학교", "기계공학", "202202", "202608", 4, "서류 합격", "공정"),
        사람("B", "김철수", "KAIST", "전기공학", "202003", "202402", 7, "최종 합격", "공정"),
    ],
)


def 블록(**설정):
    return Block(id=1, dashboard_id=1, 순서=0, 종류="목록", 제목="표", 설정=설정)


def test_one_person_per_row_with_my_own_columns():
    b = 블록(목록대상="채용", 목록열=[
        ["이름", "=한글_이름"],
        ["학력", '=박사_학교 & " " & 박사_전공'],
        ["기간", '=TEXT(박사_시작,"\'yy.m") & "~" & TEXT(박사_졸업,"\'yy.m")'],
    ])
    r = render_list(b, ROWS)
    assert r.머리 == ["이름", "학력", "기간"]
    assert r.행 == [
        ["홍길동", "서울대학교 기계공학", "'22.2~'26.8"],
        ["김철수", "KAIST 전기공학", "'20.3~'24.2"],
    ]


def test_the_target_picks_the_pool_or_the_started_ones():
    b = 블록(목록대상="지원자", 목록열=[["이름", "=한글_이름"]])
    assert len(render_list(b, ROWS).행) == 3
    b.설정["목록대상"] = "채용"
    assert len(render_list(b, ROWS).행) == 2


def test_a_row_filter_is_just_a_formula():
    b = 블록(목록대상="지원자", 목록조건='=최종상태="최종 합격"',
            목록열=[["이름", "=한글_이름"]])
    assert render_list(b, ROWS).행 == [["김철수"]]


def test_a_filter_can_use_anything_the_formula_language_has():
    b = 블록(목록대상="지원자",
            목록조건='=AND(저널_주저자_수>=4, 부서="공정")',
            목록열=[["이름", "=한글_이름"]])
    assert [r[0] for r in render_list(b, ROWS).행] == ["홍길동", "김철수"]


def test_sorting_is_numeric_when_the_values_are_numbers():
    """글자로 정렬하면 10 이 9 보다 앞에 온다."""
    b = 블록(목록대상="지원자", 목록정렬="=저널_주저자_수", 목록내림차순=True,
            목록열=[["이름", "=한글_이름"], ["주저자", "=저널_주저자_수"]])
    assert [r[0] for r in render_list(b, ROWS).행] == ["김철수", "홍길동", "이영희"]
    b.설정["목록내림차순"] = False
    assert [r[0] for r in render_list(b, ROWS).행] == ["이영희", "홍길동", "김철수"]


def test_sorting_by_text_works_too():
    """KAIST < 서울대학교 < 포항공대 (영문이 한글보다 앞이다)."""
    b = 블록(목록대상="지원자", 목록정렬="=박사_학교",
            목록열=[["이름", "=한글_이름"], ["학교", "=박사_학교"]])
    assert [r[1] for r in render_list(b, ROWS).행] == ["KAIST", "서울대학교", "포항공대"]


def test_a_row_cap_keeps_the_total_visible():
    b = 블록(목록대상="지원자", 목록최대=2, 목록정렬="=저널_주저자_수",
            목록내림차순=True, 목록열=[["이름", "=한글_이름"]])
    r = render_list(b, ROWS)
    assert len(r.행) == 2 and r.전체 == 3


def test_plain_text_columns_pass_through():
    b = 블록(목록대상="채용", 목록열=[["이름", "=한글_이름"], ["구분", "지원자"]])
    assert render_list(b, ROWS).행[0] == ["홍길동", "지원자"]


def test_an_empty_header_falls_back_to_the_formula():
    b = 블록(목록대상="채용", 목록열=[["", "=한글_이름"]])
    assert render_list(b, ROWS).머리 == ["=한글_이름"]


def test_no_columns_says_so():
    assert "열이 없습니다" in render_list(블록(목록대상="채용"), ROWS).오류[0]


def test_a_broken_column_shows_a_question_mark_not_a_zero():
    """조용히 0 을 띄우면 그게 진짜 0 인지 알 수가 없다."""
    b = 블록(목록대상="채용", 목록열=[["이름", "=한글_이름"], ["엉뚱", "=없는열"]])
    r = render_list(b, ROWS)
    assert r.행[0] == ["홍길동", "?"]
    assert any("없는열" in x for x in r.오류)


def test_a_broken_filter_stops_the_whole_block():
    """한 줄만 틀린 게 아니라 무엇을 보여줄지가 틀린 것이라, 표를 그리면 안 된다."""
    b = 블록(목록대상="채용", 목록조건="=없는열", 목록열=[["이름", "=한글_이름"]])
    r = render_list(b, ROWS)
    assert not r.행 and any("행 고르기" in x for x in r.오류)


def test_unknown_columns_are_reported_against_the_known_list():
    b = 블록(목록대상="채용", 목록열=[["이름", "=한글_이름"], ["x", "=박사_학교"]])
    r = render_list(b, ROWS, 아는열={"한글_이름"})
    assert any("박사_학교" in x for x in r.오류)


# --- 말로 만드는 초안 -------------------------------------------------------------
def test_the_draft_throws_away_columns_that_use_unknown_names():
    """LLM 이 지어낸 열 이름을 그대로 실으면 화면에 ? 만 뜬다. 없느니만 못하다."""
    import json

    import httpx

    from cvtool import dash_draft
    from cvtool.clients.llm import LLMClient

    답 = {
        "제목": "채용 중", "대상": "채용", "조건": "", "정렬": "=저널_주저자_수",
        "내림차순": True,
        "열": [
            {"머리글": "이름", "수식": "=한글_이름"},
            {"머리글": "출신", "수식": "=출신학교"},          # 없는 열
            {"머리글": "학력", "수식": '=박사_학교 & " " & 박사_전공'},
        ],
    }

    def handler(request):
        return httpx.Response(200, json={"choices": [
            {"finish_reason": "stop",
             "message": {"content": json.dumps(답, ensure_ascii=False)}}]})

    llm = LLMClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    아는열 = {"한글_이름", "박사_학교", "박사_전공", "저널_주저자_수"}
    설정, 메모 = dash_draft.draft("채용 중인 사람", 아는열, llm=llm)

    assert 설정["목록대상"] == "채용"
    assert 설정["목록정렬"] == "=저널_주저자_수" and 설정["목록내림차순"] is True
    assert [c[0] for c in 설정["목록열"]] == ["이름", "학력"]   # 출신은 버렸다
    assert any("출신" in x and "출신학교" in x for x in 메모)


def test_the_draft_result_actually_renders():
    """초안이 그대로 표가 돼야 한다 — 만들어 놓고 손봐야 돌아가면 소용없다."""
    import json

    import httpx

    from cvtool import dash_draft
    from cvtool.clients.llm import LLMClient

    답 = {"대상": "채용", "열": [{"머리글": "이름", "수식": "=한글_이름"}]}

    def handler(request):
        return httpx.Response(200, json={"choices": [
            {"finish_reason": "stop",
             "message": {"content": json.dumps(답, ensure_ascii=False)}}]})

    llm = LLMClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    설정, _메모 = dash_draft.draft("채용 중인 사람 이름", {"한글_이름"}, llm=llm)
    r = render_list(블록(**설정), ROWS)
    assert r.머리 == ["이름"] and [x[0] for x in r.행] == ["홍길동", "김철수"]


def test_a_dead_llm_does_not_stop_you_from_building_by_hand():
    import httpx

    from cvtool import dash_draft
    from cvtool.clients.llm import LLMClient

    def handler(request):
        return httpx.Response(500, text="죽었다")

    llm = LLMClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    설정, 메모 = dash_draft.draft("아무거나", {"한글_이름"}, llm=llm)
    assert 설정 == {} and 메모 and "초안" in 메모[0]


def test_a_cell_with_a_line_break_is_marked_so_it_can_wrap():
    """줄바꿈을 일부러 넣은 칸만 줄을 바꾼다 — 나머지는 한 줄로 잘린 채 둔다."""
    b = 블록(목록대상="채용", 목록열=[
        ["이름", "=한글_이름"],
        ["학력", "=박사_학교 & CHAR(10) & 박사_전공"],
    ])
    r = render_list(b, ROWS)
    assert r.행[0] == ["홍길동", "서울대학교\n기계공학"]


# --- 보이는 모양 ------------------------------------------------------------------
def test_a_column_can_be_given_a_width():
    """열 너비를 못 정하면 표가 화면 폭을 나눠 갖느라 쓸데없이 넓어진다."""
    b = 블록(목록대상="채용", 목록열=[
        ["이름", "=한글_이름", "90"],
        ["학력", "=박사_학교", ""],
    ])
    r = render_list(b, ROWS)
    assert r.폭 == ["90", ""]
    assert r.머리 == ["이름", "학력"]


def test_old_two_column_rows_still_load():
    """쓰던 대시보드가 깨지면 안 된다 — 너비는 나중에 생긴 칸이다."""
    b = 블록(목록대상="채용", 목록열=[["이름", "=한글_이름"]])
    r = render_list(b, ROWS)
    assert r.폭 == [""] and r.행[0] == ["홍길동"]


def test_the_table_shape_has_sensible_defaults():
    b = 블록(목록대상="채용", 목록열=[["이름", "=한글_이름"]])
    assert b.테두리 == "가로줄"
    assert not b.줄무늬 and not b.촘촘히 and not b.머리배경


def test_a_list_defaults_to_its_natural_width_so_it_can_scroll():
    """열을 만드는 사람이 정하니 열두 개도 된다. 화면 폭을 나눠 가지면 전부 잘린다."""
    목록 = 블록(목록대상="채용", 목록열=[["이름", "=한글_이름"]])
    assert 목록.표너비 == "내용에 맞춤"
    목록.설정["표너비"] = "창에 맞춤"          # 고르면 그게 이긴다
    assert 목록.표너비 == "창에 맞춤"

    from cvtool.dashboards import Block
    축표 = Block(id=1, dashboard_id=1, 순서=0, 종류="축표", 제목="x", 설정={})
    assert 축표.표너비 == "창에 맞춤"          # 열이 몇 개 안 된다


def test_the_table_shape_is_remembered():
    b = 블록(목록대상="채용", 목록열=[["이름", "=한글_이름"]],
            테두리="격자", 줄무늬=True, 촘촘히=True,
            표너비="내용에 맞춤", 머리배경="#eef2f7")
    assert (b.테두리, b.표너비) == ("격자", "내용에 맞춤")
    assert b.줄무늬 and b.촘촘히 and b.머리배경 == "#eef2f7"


# --- 값에 따라 칠하기 -------------------------------------------------------------
def 색칠(*규칙, **나머지):
    return 블록(목록대상="지원자",
              목록열=[["이름", "=한글_이름", ""], ["주저자", "=저널_주저자_수", ""]],
              조건서식=list(규칙), **나머지)


def test_a_rule_paints_the_whole_row():
    r = render_list(색칠({"조건": "=저널_주저자_수>=5", "대상": "줄 전체",
                        "배경": "#dcfce7", "글자": ""}), ROWS)
    assert r.행색 == ["", "background:#dcfce7", ""]      # 김철수(7)만
    assert r.칸색 == [["", ""], ["", ""], ["", ""]]


def test_a_rule_can_paint_just_one_cell():
    r = render_list(색칠({"조건": "=저널_주저자_수<=2", "대상": "주저자",
                        "배경": "#fee2e2", "글자": "#b91c1c"}), ROWS)
    assert r.행색 == ["", "", ""]
    assert r.칸색[2] == ["", "background:#fee2e2;color:#b91c1c"]   # 이영희(2)
    assert r.칸색[0] == ["", ""]


def test_many_rules_and_the_first_match_wins():
    """엑셀도 규칙에 순서가 있다. 위에서부터 보다가 처음 맞는 것."""
    r = render_list(색칠(
        {"조건": "=저널_주저자_수>=4", "대상": "줄 전체", "배경": "#dcfce7"},
        {"조건": "=저널_주저자_수>=1", "대상": "줄 전체", "배경": "#fef3c7"},
    ), ROWS)
    # 홍길동 4, 김철수 7 → 첫 규칙 / 이영희 2 → 둘째 규칙
    assert r.행색 == ["background:#dcfce7", "background:#dcfce7",
                    "background:#fef3c7"]


def test_a_cell_rule_beats_a_row_rule():
    """더 좁게 가리킨 쪽이 이긴다."""
    r = render_list(색칠(
        {"조건": "=저널_주저자_수>=1", "대상": "줄 전체", "배경": "#dcfce7"},
        {"조건": "=저널_주저자_수<=2", "대상": "주저자", "배경": "#fee2e2"},
    ), ROWS)
    assert r.행색[2] == "background:#dcfce7"
    assert r.칸색[2][1] == "background:#fee2e2"


def test_only_real_colours_get_through():
    """고른 값이 그대로 style 에 들어간다. 모양을 확인한 것만 내보낸다."""
    r = render_list(색칠({"조건": "=저널_주저자_수>=1", "대상": "줄 전체",
                        "배경": "red; background:url(x)"}), ROWS)
    assert r.행색 == ["", "", ""]                       # 색이 아니면 안 칠한다


def test_a_rule_with_no_colour_does_nothing():
    r = render_list(색칠({"조건": "=저널_주저자_수>=1", "대상": "줄 전체"}), ROWS)
    assert r.행색 == ["", "", ""]


def test_a_broken_rule_says_so_and_paints_nothing():
    r = render_list(색칠({"조건": "=없는열", "대상": "줄 전체",
                        "배경": "#dcfce7"}), ROWS)
    assert r.행색 == ["", "", ""]
    assert any("색칠 조건" in x for x in r.오류)


def test_a_rule_pointing_at_a_missing_column_is_reported():
    """조용히 아무 일도 안 하면 왜 안 칠해지는지 알 수가 없다."""
    r = render_list(색칠({"조건": "=저널_주저자_수>=1", "대상": "없는칸",
                        "배경": "#dcfce7"}), ROWS)
    assert any("색칠 규칙이 가리키는 열이 없습니다" in x for x in r.오류)
