"""말로 적어서 만드는 블록 초안 — **모든 종류**에 붙는다.

빈 화면에서 시작하는 건 어느 블록이든 부담스럽다. 초안이 있으면 고치는 일이
되고, 고치는 건 훨씬 쉽다.

지키는 것 두 가지:
  1. LLM 은 **값이 아니라 정의**만 만든다. 표는 언제나 우리 계산기가 그린다.
  2. 나온 걸 그대로 쓰지 않는다 — 검사에서 떨어진 것은 **버리고 알린다.**
"""

from __future__ import annotations

import json

import httpx

from cvtool import dash_draft
from cvtool.clients.llm import LLMClient

아는열 = {"한글_이름", "영문_이름", "현재_신분", "박사_학교", "박사_전공",
        "박사_시작", "박사_졸업", "저널_수", "저널_주저자_수", "부서", "과제",
        "최종상태", "서류검토", "검토_필요", "지원자_ID"}
축들 = ["부서", "과제", "단계", "최종상태", "등록년도", "현재_신분"]


def _llm(답: dict):
    def handler(request):
        return httpx.Response(200, json={"choices": [
            {"finish_reason": "stop",
             "message": {"content": json.dumps(답, ensure_ascii=False)}}]})

    return LLMClient(client=httpx.Client(transport=httpx.MockTransport(handler)))


def 만들기(종류: str, 답: dict):
    return dash_draft.draft("아무 설명", 아는열, llm=_llm(답),
                            종류=종류, 축목록=축들)


# --- 축표 ----------------------------------------------------------------------
def test_a_pivot_draft():
    설정, 메모 = 만들기("축표", {
        "제목": "부서×단계", "행축": "부서", "열축": "단계",
        "칸수식": '=COUNT(채용, 부서="{행}", 최종상태="{열}")', "형식": "명",
    })
    assert 설정["행축"] == "부서" and 설정["열축"] == "단계"
    assert "{행}" in 설정["칸수식"] and 설정["형식"] == "명"
    assert 설정["_제목"] == "부서×단계"
    assert any("보고 고친 뒤" in x for x in 메모)


def test_a_pivot_axis_outside_the_list_falls_back_to_manual():
    """모르는 축을 그대로 저장하면 화면이 빈 표를 그린다."""
    설정, _메모 = 만들기("축표", {
        "행축": "별자리", "열축": "부서", "칸수식": "=COUNT(채용)"})
    assert 설정["행축"] == "직접 입력" and 설정["열축"] == "부서"


def test_a_pivot_with_a_broken_cell_formula_says_so():
    설정, 메모 = 만들기("축표", {
        "행축": "부서", "열축": "단계", "칸수식": "=COUNT(없는대상)"})
    assert 설정["칸수식"] == ""
    assert any("손으로 적어" in x for x in 메모)


# --- 숫자 ----------------------------------------------------------------------
def test_a_number_draft():
    설정, 메모 = 만들기("숫자", {
        "제목": "최종 합격", "수식": '=COUNT(채용, 최종상태="최종 합격")',
        "형식": "명"})
    assert 설정["수식"].startswith("=COUNT(채용") and 설정["형식"] == "명"
    assert 설정["_제목"] == "최종 합격"


def test_an_unknown_format_falls_back():
    설정, _메모 = 만들기("숫자", {"수식": "=COUNT(지원자)", "형식": "무지개"})
    assert 설정["형식"] == "그대로"


def test_a_number_needs_an_aggregate_not_a_row_formula():
    """행 문맥 수식(=한글_이름)은 숫자 블록에서 계산되지 않는다."""
    설정, 메모 = 만들기("숫자", {"수식": "=한글_이름"})
    assert 설정["수식"] == "" and any("손으로" in x for x in 메모)


# --- 프로필 ---------------------------------------------------------------------
def test_a_profile_draft():
    설정, 메모 = 만들기("프로필", {
        "제목": "면접 자료", "대상": '=LIST(채용, 부서="공정")',
        "머리": '=한글_이름 & " (" & 현재_신분 & ")"',
        "줄": [{"라벨": "학력", "문장": '=박사_학교 & " " & 박사_전공'},
              {"라벨": "실적", "문장": '=저널_주저자_수 & "/" & 저널_수'},
              {"라벨": "엉뚱", "문장": "=없는열"}],
    })
    assert 설정["대상"].startswith("=LIST(채용")
    assert [r[0] for r in 설정["줄"]] == ["학력", "실적"]      # 엉뚱은 버렸다
    assert any("엉뚱" in x for x in 메모)


def test_a_profile_without_a_target_shows_everyone():
    설정, _메모 = 만들기("프로필", {"대상": "", "줄": [
        {"라벨": "이름", "문장": "=한글_이름"}]})
    assert 설정["대상"] == "=LIST(지원자, 열=지원자_ID)"


# --- 자유 표 ---------------------------------------------------------------------
def test_a_free_table_draft():
    설정, 메모 = 만들기("표", {
        "제목": "요약", "행": ["전체", "합격"], "열": ["공정", "소재"],
        "칸": [
            {"행": "전체", "열": "공정", "값": '=COUNT(채용, 부서="공정")'},
            {"행": "합격", "열": "공정", "값": '=COUNT(채용, 부서="공정", 최종상태="최종 합격")'},
            {"행": "없는줄", "열": "공정", "값": "=COUNT(채용)"},
        ],
    })
    assert 설정["행"] == ["전체", "합격"] and 설정["열"] == ["공정", "소재"]
    assert "전체\t공정" in 설정["칸"] and "없는줄\t공정" not in 설정["칸"]


def test_a_free_table_keeps_plain_text_cells():
    설정, _메모 = 만들기("표", {
        "행": ["메모"], "열": ["내용"],
        "칸": [{"행": "메모", "열": "내용", "값": "직접 적은 글"}]})
    assert 설정["칸"]["메모\t내용"] == "직접 적은 글"


# --- 글 -----------------------------------------------------------------------
def test_a_text_draft():
    설정, _메모 = 만들기("글", {"제목": "안내", "글": "매주 월요일 회의에 씁니다."})
    assert 설정["글"] == "매주 월요일 회의에 씁니다."
    assert 설정["_제목"] == "안내"


# --- 공통 ----------------------------------------------------------------------
def test_an_empty_description_asks_for_one():
    설정, 메모 = dash_draft.draft("", 아는열, llm=_llm({}), 종류="숫자")
    assert 설정 == {} and "적어 주세요" in 메모[0]


def test_an_unknown_block_kind_says_so():
    설정, 메모 = dash_draft.draft("뭐든", 아는열, llm=_llm({}), 종류="없는종류")
    assert 설정 == {} and "아직 말로 못" in 메모[0]


def test_a_dead_llm_never_blocks_building_by_hand():
    def handler(request):
        return httpx.Response(500, text="죽었다")

    llm = LLMClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    for 종류 in ("목록", "축표", "숫자", "프로필", "표", "글"):
        설정, 메모 = dash_draft.draft("뭐든", 아는열, llm=llm, 종류=종류)
        assert 설정 == {} and "초안" in 메모[0], 종류


def test_every_block_kind_has_a_prompt():
    from cvtool.dashboards import BLOCK_KINDS

    for 종류 in BLOCK_KINDS:
        assert 종류 in dash_draft._모양, 종류
        안내 = dash_draft._안내(종류)
        assert len(안내) > 100, 종류
        # 열 목록 자체는 안내문에 안 박고 부를 때 붙인다 (한 군데서 관리)
        assert "{열목록}" not in 안내


# --- 예시 표를 보고 만들기 --------------------------------------------------------
#
# 말로만 설명하면 "이런 모양" 이 전달되지 않는다. 엑셀에 이미 만들어 둔 표를
# 그대로 붙여넣으면, 그 모양이 나오도록 수식을 만들게 한다.
def test_parse_table_reads_what_excel_pastes():
    """엑셀에서 복사하면 탭으로 구분된 글자가 온다."""
    assert dash_draft.parse_table("지원자\t이력\n홍길동\t서울대") == [
        ["지원자", "이력"], ["홍길동", "서울대"]]


def test_parse_table_reads_pipes_and_commas():
    assert dash_draft.parse_table("지원자 | 이력\n홍길동(27세) | 경)서울대") == [
        ["지원자", "이력"], ["홍길동(27세)", "경)서울대"]]
    assert dash_draft.parse_table("가,나\n1,2") == [["가", "나"], ["1", "2"]]
    assert dash_draft.parse_table("| 가 | 나 |\n| 1 | 2 |") == [["가", "나"], ["1", "2"]]


def test_parse_table_skips_blank_lines_and_caps_size():
    assert dash_draft.parse_table("가|나\n\n\n1|2") == [["가", "나"], ["1", "2"]]
    assert dash_draft.parse_table("") == []
    긴것 = "\n".join("a|b" for _ in range(100))
    assert len(dash_draft.parse_table(긴것)) == dash_draft.MAX_예시줄
    assert len(dash_draft.parse_table("|".join("c" * 40 for _ in range(40)))[0]) \
        == dash_draft.MAX_예시칸
    assert len(dash_draft.parse_table("x" * 500)[0][0]) == dash_draft.MAX_칸글자


def _프롬프트잡기(답: dict):
    """LLM 에 실제로 보낸 글을 잡아 둔다."""
    보낸것: list[str] = []

    def handler(request):
        보낸것.append(request.content.decode("utf-8"))
        return httpx.Response(200, json={"choices": [
            {"finish_reason": "stop",
             "message": {"content": json.dumps(답, ensure_ascii=False)}}]})

    return LLMClient(client=httpx.Client(transport=httpx.MockTransport(handler))), 보낸것


def test_the_example_table_reaches_the_prompt():
    llm, 보낸것 = _프롬프트잡기({"제목": "지원자", "대상": "지원자",
                            "열": [{"머리글": "이름", "수식": "=한글_이름"}]})
    설정, 메모 = dash_draft.draft(
        "예시처럼 만들어줘", 아는열, llm=llm, 종류="목록",
        예시표="지원자 | 이력\n홍길동(27세)/Neurips 1저자 | 경)서울대 포닥")

    글 = 보낸것[0]
    assert "예시 표" in 글
    assert "홍길동(27세)/Neurips 1저자" in 글          # 표가 그대로 실려 나간다
    assert "그대로 쓰지 마라" in 글                    # 값이 아니라 모양이라고 일러둔다
    assert any("예시 표 2줄" in m for m in 메모)       # 사람에게도 알린다


def test_no_example_table_works_exactly_as_before():
    llm, 보낸것 = _프롬프트잡기({"제목": "지원자", "대상": "지원자",
                            "열": [{"머리글": "이름", "수식": "=한글_이름"}]})
    설정, 메모 = dash_draft.draft("이름만", 아는열, llm=llm, 종류="목록")
    assert "예시 표" not in 보낸것[0]
    assert not any("예시 표" in m for m in 메모)
    assert 설정["목록열"] == [["이름", "=한글_이름", ""]]


def test_an_example_table_alone_is_enough():
    """설명을 안 적고 표만 붙여넣어도 만들어 준다."""
    llm, 보낸것 = _프롬프트잡기({"제목": "표", "대상": "지원자",
                            "열": [{"머리글": "이름", "수식": "=한글_이름"}]})
    설정, 메모 = dash_draft.draft("", 아는열, llm=llm, 종류="목록",
                               예시표="이름\n홍길동")
    assert 설정["목록열"] == [["이름", "=한글_이름", ""]]

    _, 메모2 = dash_draft.draft("", 아는열, llm=_llm({}), 종류="목록")
    assert 메모2 and "적어 주세요" in 메모2[0]          # 둘 다 없으면 안내
