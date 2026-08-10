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
        "1저자_논문": [
            {"제출처": "NeurIPS", "연도": "2024", "유형": "학회", "국내해외": "해외"},
            {"제출처": "한국정보과학회 KCC", "연도": "2023", "유형": "학회", "국내해외": "국내"},
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
    if "1저자_논문" in props:
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
    """추정한 이름은 반드시 표시돼야 한다 (원문과 구분)."""
    rec = extract_cv_from_text("이력서", client=_sectioned_client())
    assert rec.이름_추정여부 == "영문추정"
    assert rec.검토_필요 == "Y"
    assert "추정" in rec.검토_사유


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
