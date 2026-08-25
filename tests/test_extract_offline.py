"""LLM 을 목킹한 오프라인 파이프라인 테스트.

실제 로컬 LLM 없이 추출 파이프라인 로직을 검증한다.
실제 LLM 대상 검증은 서비스가 떠 있는 온프레미스 서버에서 수행해야 한다.
"""

from __future__ import annotations

import json

import httpx
import pytest

from cvtool.clients.llm import LLMClient, LLMError, LLMTruncated, build_json_payload, parse_response
from cvtool.extract import extract_cv_from_text
from cvtool.schemas import COLUMNS, SECTION_BASIC

SECTION_REPLIES = {
    "basic": {
        "한글_이름": "홍길동",
        "영문_이름": "Gildong Hong",
        "한글_이름_출처": "원문",
        "영문_이름_출처": "추정",
        "생년월일": "19920315",
        "전화번호": "010-1234-5678",
        "이메일": "hong@example.com",
        "현재_신분": "포닥",
        "현재_소속": "서울대학교",
        "현재_소속_상세": "전기정보공학부",
        "현재_지도교수": "김철수",
    },
    "education": {
        "박사_학교": "서울대학교",
        "박사_전공": "컴퓨터공학",
        "박사_지도교수": "김철수",
        "박사_시작": "201903",
        "박사_졸업": "202502",
        "박사_학위상태": "졸업",
        "석사_학교": "",
        "석사_지도교수": "",
        "학사_학교": "한국대학교",
        "학사_전공": "컴퓨터공학",
        "학사_시작": "201503",
        "학사_졸업": "201902",
        "석박통합_여부": False,
    },
    "research": {
        "논문": [
            {"제출처": "NeurIPS", "연도": "2024", "유형": "학회", "국내해외": "해외",
             "저자구분": "주저자"},
            {"제출처": "한국정보과학회 KCC", "연도": "2023", "유형": "학회",
             "국내해외": "국내", "저자구분": "주저자"},
            {"제출처": "Nature Machine Intelligence", "연도": "2025", "유형": "저널",
             "국내해외": "해외", "저자구분": "공저자"},
        ],
        "특허": [
            {"제목": "무언가 하는 방법", "상태": "등록", "연도": "2024"},
            {"제목": "다른 방법", "상태": "출원", "연도": "2025"},
            {"제목": "세번째", "상태": "출원", "연도": "2025"},
        ],
        "연구분야_키워드": ["컴퓨터비전", "멀티모달"],
    },
    "career": {"경력": [{"회사": "라마디테크", "직무": "연구원", "시작": "202103", "종료": "202406"}]},
}


def _sectioned_client(fail: set[str] | None = None) -> LLMClient:
    """섹션 이름에 따라 알맞은 응답을 돌려주는 목 클라이언트."""
    fail = fail or set()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        name = (
            payload.get("response_format", {}).get("json_schema", {}).get("name")
            or _guess_section(payload)
        )
        if name in fail:
            return httpx.Response(500, text="boom")
        reply = SECTION_REPLIES.get(name, {})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(reply, ensure_ascii=False)},
                    }
                ]
            },
        )

    return LLMClient(client=httpx.Client(transport=httpx.MockTransport(handler)))


def _guess_section(payload: dict) -> str:
    """guided_json 모드에는 이름이 없으므로 스키마 속성으로 섹션을 구분한다."""
    props = set((payload.get("guided_json") or {}).get("properties", {}))
    if "현재_신분" in props:
        return "basic"
    if "박사_학교" in props:
        return "education"
    if "논문" in props:
        return "research"
    if "경력" in props:
        return "career"
    return ""


# --- payload 구성 -----------------------------------------------------------
def test_payload_includes_max_tokens():
    """max_tokens 미지정으로 조용히 잘리는 것을 막는다."""
    p = build_json_payload([{"role": "user", "content": "x"}], SECTION_BASIC)
    assert p["guided_json"] == SECTION_BASIC
    assert p["max_tokens"] > 0
    assert p["temperature"] == 0.0


def test_payload_response_format_fallback():
    p = build_json_payload([{"role": "user", "content": "x"}], SECTION_BASIC, mode="response_format")
    assert p["response_format"]["json_schema"]["schema"] == SECTION_BASIC


# --- 응답 정제: 실제로 났던 파싱 실패 원인들 --------------------------------
def _body(content: str, finish: str = "stop") -> dict:
    return {"choices": [{"finish_reason": finish, "message": {"content": content}}]}


def test_parse_plain_json():
    assert parse_response(_body('{"이름": "홍길동"}')) == {"이름": "홍길동"}


def test_parse_strips_markdown_fence():
    raw = '```json\n{"이름": "홍길동"}\n```'
    assert parse_response(_body(raw)) == {"이름": "홍길동"}


def test_parse_strips_think_block():
    """모델명이 thinkingcap 이라 추론 접두가 붙을 수 있다."""
    raw = '<think>먼저 이름을 찾자...</think>\n{"이름": "홍길동"}'
    assert parse_response(_body(raw)) == {"이름": "홍길동"}


def test_parse_extracts_json_from_prose():
    raw = '네, 추출 결과입니다:\n{"이름": "홍길동", "메모": "{중괄호} 포함"}\n이상입니다.'
    assert parse_response(_body(raw))["이름"] == "홍길동"


def test_parse_uses_reasoning_content_when_content_empty():
    body = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "", "reasoning_content": '{"이름": "홍길동"}'},
            }
        ]
    }
    assert parse_response(body) == {"이름": "홍길동"}


def test_truncation_raises_clear_error():
    """finish_reason=length 는 'JSON 파싱 실패'가 아니라 잘림이라고 말해야 한다."""
    with pytest.raises(LLMTruncated) as exc:
        parse_response(_body('{"이름": "홍', finish="length"))
    assert "잘렸" in str(exc.value)


def test_parse_error_keeps_full_raw():
    """원인 파악을 위해 원본을 자르지 않고 보존한다."""
    raw = "완전히 JSON 이 아닌 긴 응답 " + "가" * 500
    with pytest.raises(LLMError) as exc:
        parse_response(_body(raw))
    assert exc.value.raw == raw
    assert len(exc.value.raw) > 200


# --- 섹션 분할 추출 ---------------------------------------------------------
def test_sectioned_extraction_builds_record():
    rec = extract_cv_from_text("이력서 본문", client=_sectioned_client())
    assert rec.한글_이름 == "홍길동"
    assert rec.현재_신분 == "포닥"
    assert rec.박사_시작 == "201903"
    assert rec.박사_지도교수 == "김철수"
    assert rec.경력_요약 == "라마디테크/연구원(202103-202406)"
    assert rec.지원자_ID.startswith("CV-")


def test_overseas_papers_only_in_column():
    """해외 학회/저널만 열에 들어가고 국내는 빠진다."""
    rec = extract_cv_from_text("이력서", client=_sectioned_client())
    assert rec.해외논문_제출처() == "NeurIPS 2024"
    assert "KCC" not in rec.해외논문_제출처()


def test_guessed_name_is_flagged():
    """추정한 이름은 반드시 표시돼야 한다 (원문과 구분).

    표시하는 곳은 **검토 사유 하나뿐**이다. 예전에는 `이름_추정여부` 열도 따로
    뒀는데, 같은 말을 두 군데서 하니 '한글추정' 을 확인함 처리해도 열은 그대로
    남아 표에서 자리만 차지했다.
    """
    rec = extract_cv_from_text("이력서", client=_sectioned_client())
    assert rec.검토_필요 == "Y"
    assert "영문추정" in rec.검토_사유
    assert not hasattr(rec, "이름_추정여부")


def test_one_section_failure_keeps_the_rest():
    """한 섹션이 실패해도 나머지는 살아야 한다."""
    rec = extract_cv_from_text("이력서", client=_sectioned_client(fail={"career"}))
    assert rec.한글_이름 == "홍길동"       # 기본정보는 살아있고
    assert rec.경력_요약 == ""             # 경력만 비었으며
    assert rec.검토_필요 == "Y"
    assert "경력 추출 실패" in rec.검토_사유


def test_row_has_every_column():
    rec = extract_cv_from_text("이력서", client=_sectioned_client())
    row = rec.to_row()
    assert list(row.keys()) == COLUMNS


def test_empty_text_raises():
    with pytest.raises(ValueError):
        extract_cv_from_text("   ")


# --- 실제로 겪은 파싱 실패: 모델 응답은 정상인데 파서가 거부한 경우 -----------
SCHEMA_HINT = {"properties": {"한글_이름": {}, "영문_이름": {}, "현재_신분": {}}}


@pytest.mark.parametrize(
    "label,raw",
    [
        ("JSON 뒤에 설명", '{"한글_이름": "홍길동"}\n\n위와 같이 추출했습니다.'),
        ("JSON 앞뒤로 설명", '결과입니다:\n{"한글_이름": "홍길동"}\n이상입니다.'),
        ("펜스 뒤에 설명", '```json\n{"한글_이름": "홍길동"}\n```\n확인 바랍니다.'),
        ("같은 객체 두 번", '{"한글_이름": "홍길동"}\n{"한글_이름": "홍길동"}'),
        ("닫히지 않은 think", '<think>이름을 찾자\n{"한글_이름": "홍길동"}'),
        ("여는 태그 없는 </think>", '이름을 찾았다</think>\n{"한글_이름": "홍길동"}'),
        ("설명에 중괄호", '{"한글_이름": "홍길동"}\n메모: {참고} 있음'),
        ("앞에 공백/개행", '\n\n  {"한글_이름": "홍길동"}  \n'),
    ],
)
def test_parses_responses_that_look_fine_to_a_human(label, raw):
    """사람이 봐서 정상인 응답은 반드시 파싱돼야 한다.

    JSON 뒤에 한 줄만 붙어도 json.loads 는 'Extra data' 로 실패한다.
    실제로 이것 때문에 섹션 추출이 전부 실패했다.
    """
    body = {"choices": [{"finish_reason": "stop", "message": {"content": raw}}]}
    assert parse_response(body, SCHEMA_HINT)["한글_이름"] == "홍길동", label


def test_schema_picks_answer_over_example_json():
    """추론 중 남긴 예시 JSON 이 아니라 스키마에 맞는 답을 골라야 한다."""
    raw = (
        '<think>이런 형식으로 만들자: {"예시": 1, "샘플": 2, "더미": 3, "테스트": 4}\n'
        '이제 답을 쓰자</think>\n{"한글_이름": "홍길동", "현재_신분": "포닥"}'
    )
    body = {"choices": [{"finish_reason": "stop", "message": {"content": raw}}]}
    result = parse_response(body, SCHEMA_HINT)
    assert result["한글_이름"] == "홍길동"
    assert "예시" not in result


def test_no_json_at_all_still_errors():
    """정말 JSON 이 없으면 실패해야 한다 (아무거나 지어내면 안 된다)."""
    body = {"choices": [{"finish_reason": "stop", "message": {"content": "죄송합니다"}}]}
    with pytest.raises(LLMError):
        parse_response(body, SCHEMA_HINT)


# --- 석박통합 · 논문/특허 수 · 대표 경력 ------------------------------------------
def test_integrated_program_becomes_its_own_column():
    """박사 정보 왼쪽에 석박통합 여부가 따로 선다."""
    from cvtool.schemas import COLUMNS

    assert COLUMNS.index("박사_석박통합") == COLUMNS.index("박사_학교") - 1


def test_no_masters_but_a_phd_is_read_as_an_integrated_program():
    """석사 없이 박사만 있으면 통합과정으로 본다 (국내에서는 대개 맞다).

    다만 **확실하지 않다** — 해외 박사는 석사 없이 바로 가기도 하고, 석사를
    이력서에 안 적었을 수도 있다. 그래서 반드시 검토_필요로 표시한다.
    """
    from cvtool.extract import _assemble

    rec = _assemble({"education": {"박사_학교": "서울대학교", "박사_전공": "기계공학"}},
                    [], 지원자_ID="CV-1", 원본_파일명="")
    assert rec.박사_석박통합 == "석박통합"
    assert rec.검토_필요 == "Y"
    assert "석사 학력이 없어" in rec.검토_사유


def test_a_masters_degree_means_it_is_not_inferred():
    from cvtool.extract import _assemble

    rec = _assemble(
        {"education": {"박사_학교": "서울대학교", "석사_학교": "서울대학교"}},
        [], 지원자_ID="CV-1", 원본_파일명="",
    )
    assert rec.박사_석박통합 == ""
    assert "석사 학력이 없어" not in rec.검토_사유


def test_no_phd_means_no_inference():
    from cvtool.extract import _assemble

    rec = _assemble({"education": {"석사_학교": "서울대학교"}}, [],
                    지원자_ID="CV-1", 원본_파일명="")
    assert rec.박사_석박통합 == ""


def test_stated_integrated_program_is_not_flagged_as_a_guess():
    """이력서에 적혀 있으면 유추가 아니다. 검토 사유에 안 남는다."""
    from cvtool.extract import _assemble

    rec = _assemble({"education": {"박사_학교": "서울대", "석박통합_여부": True}},
                    [], 지원자_ID="CV-1", 원본_파일명="")
    assert rec.박사_석박통합 == "석박통합"
    assert "석사 학력이 없어" not in rec.검토_사유


def test_the_prompt_tells_the_model_to_infer_it_too():
    from cvtool.extract import _EDU_HINT

    assert "석사 학력이 없고 박사만 있으면" in _EDU_HINT


def test_integrated_program_is_marked_from_either_signal():
    from cvtool.extract import _assemble

    for edu, basic in (
        ({"석박통합_여부": True}, {}),
        ({}, {"현재_신분": "석박통합"}),
    ):
        rec = _assemble({"education": edu, "basic": basic}, [],
                        지원자_ID="CV-1", 원본_파일명="")
        assert rec.박사_석박통합 == "석박통합"


def test_integrated_program_blank_when_not_applicable():
    from cvtool.extract import _assemble

    rec = _assemble({"education": {"석박통합_여부": False}, "basic": {"현재_신분": "박사"}},
                    [], 지원자_ID="CV-1", 원본_파일명="")
    assert rec.박사_석박통합 == ""


def test_coauthored_papers_are_collected_but_kept_out_of_the_first_author_column():
    rec = extract_cv_from_text("이력서", client=_sectioned_client())
    assert len(rec.논문) == 3                       # 공저자 논문까지 전부
    # 공저자 해외 저널은 1저자 열에 들어가면 안 된다
    assert "Nature Machine Intelligence" not in rec.해외논문_제출처()
    assert rec.해외논문_제출처() == "NeurIPS 2024"


def test_paper_and_patent_counts():
    rec = extract_cv_from_text("이력서", client=_sectioned_client())
    row = rec.to_row()
    assert row["학회_수"] == "2" and row["학회_주저자_수"] == "2"
    assert row["저널_수"] == "1" and row["저널_주저자_수"] == ""      # 공저자뿐
    assert row["특허_등록_수"] == "1" and row["특허_출원_수"] == "2"


def test_patent_with_unknown_status_is_counted_in_neither():
    from cvtool.schemas import CVRecord, Patent

    rec = CVRecord(지원자_ID="X", 특허=[Patent(상태="불명"), Patent(상태="등록")])
    assert rec.특허_수() == {"특허_등록_수": 1, "특허_출원_수": 0}


def test_representative_career_columns_are_filled():
    rec = extract_cv_from_text("이력서", client=_sectioned_client())
    assert rec.경력_회사 == "라마디테크"
    assert rec.직책 == "연구원"
    assert rec.경력_시작 == "202103" and rec.경력_종료 == "202406"


def test_postdoc_shows_up_as_the_current_career():
    """현재 포닥 중이면 그게 대표 경력이어야 한다 — 6개월이 안 됐어도."""
    from cvtool.extract import _assemble

    rec = _assemble(
        {"career": {"경력": [
            {"회사": "라마디테크", "직무": "연구원", "시작": "202103", "종료": "202406"},
            {"회사": "한국대학교", "직무": "박사후연구원", "시작": "202605", "종료": "재직중"},
        ]}},
        [], 지원자_ID="CV-1", 원본_파일명="",
    )
    assert rec.경력_회사 == "한국대학교"
    assert rec.직책 == "박사후연구원"
    assert rec.경력_종료 == "재직중"
    assert "박사후연구원" in rec.경력_요약


def test_short_and_intern_careers_are_left_out():
    from cvtool.extract import _assemble

    rec = _assemble(
        {"career": {"경력": [
            {"회사": "짧은곳", "직무": "연구원", "시작": "202401", "종료": "202403"},
            {"회사": "인턴한곳", "직무": "인턴", "시작": "202301", "종료": "202312"},
            {"회사": "제대로", "직무": "선임연구원", "시작": "202101", "종료": "202312"},
        ]}},
        [], 지원자_ID="CV-1", 원본_파일명="",
    )
    assert rec.경력_회사 == "제대로"
    # 요약에는 그대로 남는다 — 뽑아 쓰는 것과 기록은 다르다
    assert "인턴한곳" in rec.경력_요약


def test_no_eligible_career_leaves_the_columns_blank():
    from cvtool.extract import _assemble

    rec = _assemble(
        {"career": {"경력": [{"회사": "인턴만", "직무": "Intern",
                            "시작": "202301", "종료": "202306"}]}},
        [], 지원자_ID="CV-1", 원본_파일명="",
    )
    assert rec.경력_회사 == "" and rec.직책 == ""


def test_count_columns_cannot_be_edited_by_hand():
    from cvtool.edit import READONLY_FIELDS
    from cvtool.schemas import COUNT_COLUMNS

    for c in COUNT_COLUMNS:
        assert c in READONLY_FIELDS, c


def test_registry_decides_journal_vs_conference():
    """학회/저널 구분은 담당자가 판별한 값이 LLM 추측을 이긴다."""
    from cvtool.schemas import CVRecord, Paper

    class 사전:
        def lookup(self, 종류, 원문):
            class N:
                표시명 = "Nature"
                등급 = "최우수"
                국내해외 = "해외"
                유형 = "저널"
            return N()

        def column_tiers(self):
            return []

    rec = CVRecord(지원자_ID="X",
                   논문=[Paper(제출처="Nature", 유형="학회", 저자구분="주저자")])
    센것 = rec.논문_수(사전())
    assert 센것["저널_수"] == 1 and 센것["학회_수"] == 0


# --- 연구분야 키워드 ---------------------------------------------------------
def test_the_model_is_actually_asked_for_keywords():
    """비어 있던 진짜 이유: **프롬프트에 키워드 얘기가 한 줄도 없었다.**

    스키마에 항목만 있으면 모델은 채울 이유가 없다. 지시가 있어야 채운다.
    """
    from cvtool.extract import _RESEARCH_HINT, _READ_PROMPT

    assert "연구분야 키워드" in _RESEARCH_HINT
    assert "Research Interests" in _RESEARCH_HINT
    assert "논문 제목" in _RESEARCH_HINT      # 없을 때 어디서 뽑을지
    assert "연구 분야" in _READ_PROMPT        # 1단계 정리에서도 챙긴다


def test_keywords_are_required_by_the_schema():
    """guided_json 의 required 에 없으면 모델이 조용히 빼먹는다."""
    from cvtool.schemas import SECTION_RESEARCH

    assert "연구분야_키워드" in SECTION_RESEARCH["required"]


def test_useless_words_are_dropped():
    """'연구' '개발' 같은 낱말은 아무것도 알려주지 않는다."""
    from cvtool.extract import _assemble

    rec = _assemble(
        {"research": {"연구분야_키워드": ["플라즈마 식각", "연구", "개발", "박막 증착"]}},
        [], 지원자_ID="CV-1", 원본_파일명="",
    )
    assert rec.연구분야_키워드 == "플라즈마 식각 | 박막 증착"


def test_duplicate_keywords_are_merged_in_order():
    from cvtool.extract import _assemble

    rec = _assemble(
        {"research": {"연구분야_키워드": ["광학", "박막", "광학", " 박막 "]}},
        [], 지원자_ID="CV-1", 원본_파일명="",
    )
    assert rec.연구분야_키워드 == "광학 | 박막"


def test_empty_keywords_are_flagged_for_review():
    """조용히 빈칸으로 두면 아무도 모른다. 이 값은 매칭·검색에서 제일 많이 쓴다."""
    from cvtool.extract import _assemble

    rec = _assemble({"research": {"연구분야_키워드": []}}, [],
                    지원자_ID="CV-1", 원본_파일명="")
    assert rec.검토_필요 == "Y"
    assert "연구분야 키워드" in rec.검토_사유


def test_keywords_survive_the_normal_path():
    rec = extract_cv_from_text("이력서", client=_sectioned_client())
    assert rec.연구분야_키워드 == "컴퓨터비전 | 멀티모달"
    assert "연구분야 키워드" not in rec.검토_사유


# --- 논문 제목 ---------------------------------------------------------------
def test_paper_titles_are_kept():
    """제목이 그 사람의 분야를 가장 잘 알려준다."""
    from cvtool.extract import _assemble

    rec = _assemble(
        {"research": {"논문": [
            {"제목": "Plasma etching of high aspect ratio patterns",
             "제출처": "JVST", "유형": "저널", "저자구분": "주저자"},
        ], "연구분야_키워드": ["식각"]}},
        [], 지원자_ID="CV-1", 원본_파일명="",
    )
    assert rec.논문[0].제목.startswith("Plasma etching")
    assert rec.papers_view()[0]["제목"] == rec.논문[0].제목


def test_paper_titles_go_into_the_matching_profile():
    from cvtool.matching import candidate_profile
    from cvtool.schemas import CVRecord, Paper

    rec = CVRecord(지원자_ID="X", 논문=[
        Paper(제목="Plasma etching of Si", 제출처="JVST", 저자구분="주저자"),
        Paper(제목="공저자 논문", 제출처="Optics Express", 저자구분="공저자"),
    ])
    프로필 = candidate_profile(rec)
    assert "주저자 논문 제목: Plasma etching of Si" in 프로필
    # 공저자 논문도 넣되 **줄을 나눠** 어느 쪽인지 알려준다
    assert "공저자 논문 제목: 공저자 논문" in 프로필
    assert "Optics Express" not in 프로필      # 제출처는 주저자 것만


def test_old_records_without_titles_still_work():
    """제목 없이 저장된 옛 레코드도 그대로 읽혀야 한다."""
    from cvtool.schemas import CVRecord, Paper

    rec = CVRecord(지원자_ID="X", 논문=[Paper(제출처="NeurIPS", 저자구분="주저자")])
    assert rec.논문[0].제목 == ""
    assert rec.papers_view()[0]["표시명"] == "NeurIPS"


# --- 지도교수 · IF · 구글 스칼라 -----------------------------------------------------
def test_the_supervisor_prompt_asks_for_korean_then_the_original():
    """`Prof. Gil-Ho Lee` 만 적혀 있으면 누구인지 알아보기 어렵다."""
    from cvtool.extract import _BASIC_HINT, _EDU_HINT

    for hint in (_BASIC_HINT, _EDU_HINT):
        assert "한글명(원문명)" in hint
        assert "이길호(Gil-Ho Lee)" in hint
        assert "지어내지 마라" in hint          # 유추 못 하면 원문만


def test_the_scholar_link_uses_the_original_name_and_affiliation():
    """우리가 추정한 한글명으로 찾으면 해외 저널에 실린 이름과 안 맞는다."""
    from cvtool.extract import scholar_url

    assert scholar_url("Gil Dong Hong", "KAIST") == (
        "https://scholar.google.com/scholar?q=Gil+Dong+Hong+KAIST")
    assert scholar_url("홍길동", "") .endswith("q=%ED%99%8D%EA%B8%B8%EB%8F%99")
    assert scholar_url("", "") == ""


def test_the_scholar_link_falls_back_to_the_last_school():
    """현재 소속을 모르면 마지막 학교로 — 그 이름으로 논문을 냈을 것이다."""
    rec = extract_cv_from_text("이력서", client=_sectioned_client())
    assert rec.구글_스칼라_링크.startswith("https://scholar.google.com/scholar?q=")


def test_the_impact_factor_only_counts_first_author_overseas_journals():
    """학회에는 IF 가 없고, 공저자 논문은 그 사람 성과로 보기 어렵다."""
    import os
    import tempfile

    from cvtool.names import NameRegistry
    from cvtool.schemas import CVRecord, Paper

    reg = NameRegistry(os.path.join(tempfile.mkdtemp(), "n.db"))
    for 이름, IF, 유형 in [("Nature", "64.8", "저널"), ("Small", "13.3", "저널"),
                         ("ICML", "99", "학회")]:
        n = reg.observe(유형, 이름, 국내해외="해외", 유형=유형)
        reg.classify(n.id, IF=IF, 국내해외="해외", 유형=유형)

    def 만들기(*논문):
        return CVRecord(지원자_ID="X", 논문=list(논문)).최고_임팩트팩터(reg)

    주Nature = Paper(제목="a", 제출처="Nature", 유형="저널", 저자구분="주저자", 국내해외="해외")
    공Nature = Paper(제목="b", 제출처="Nature", 유형="저널", 저자구분="공저자", 국내해외="해외")
    주Small = Paper(제목="c", 제출처="Small", 유형="저널", 저자구분="주저자", 국내해외="해외")
    주ICML = Paper(제목="d", 제출처="ICML", 유형="학회", 저자구분="주저자", 국내해외="해외")
    사전밖 = Paper(제목="e", 제출처="어디에도 없는 저널", 유형="저널",
                 저자구분="주저자", 국내해외="해외")

    assert 만들기(주Small, 주Nature) == "64.8"      # 가장 높은 것
    assert 만들기(공Nature, 주Small) == "13.3"      # 공저자는 안 센다
    assert 만들기(주ICML) == ""                     # 학회는 안 센다
    assert 만들기(사전밖) == ""                     # IF 를 안 넣은 곳은 셀 수 없다
    assert 만들기() == ""


def test_the_dictionary_decides_domestic_or_overseas_not_the_llm():
    """명칭 관리에서 사람이 정한 값이 LLM 추측을 이긴다 — IF 도 그 판정을 따른다."""
    import os
    import tempfile

    from cvtool.names import NameRegistry
    from cvtool.schemas import CVRecord, Paper

    reg = NameRegistry(os.path.join(tempfile.mkdtemp(), "n.db"))
    n = reg.observe("저널", "Nature", 국내해외="해외", 유형="저널")
    reg.classify(n.id, IF="64.8", 국내해외="해외", 유형="저널")

    # LLM 은 국내라고 했지만 사전에는 해외로 적혀 있다
    rec = CVRecord(지원자_ID="X", 논문=[
        Paper(제목="a", 제출처="Nature", 유형="저널", 저자구분="주저자", 국내해외="국내")])
    assert rec.최고_임팩트팩터(reg) == "64.8"

    reg.classify(n.id, 국내해외="국내")             # 담당자가 국내로 고치면
    assert rec.최고_임팩트팩터(reg) == ""            # IF 도 같이 빠진다
