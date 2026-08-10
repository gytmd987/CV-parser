"""2단계 추출 / 길이 방어 / enum 불명 / 하드코딩 제거 검증."""

from __future__ import annotations

import json

import httpx
import pytest

from cvtool.clients.llm import LLMClient
from cvtool.extract import (
    _CAREER_HINT,
    _EDU_HINT,
    _RESEARCH_HINT,
    extract_cv_from_text,
    guard_length,
)
from cvtool.schemas import 현재_신분_ENUM

DIGEST = "정리 노트: 홍길동, 서울대 박사 재학, NeurIPS 1저자 1편."


def _recording_client(overrides: dict | None = None):
    """호출 내역을 기록하는 목 클라이언트. (calls, client) 반환."""
    calls: list[dict] = []
    overrides = overrides or {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        schema = payload.get("guided_json") or (
            payload.get("response_format", {}).get("json_schema", {}).get("schema")
        )
        if schema is None:  # 1단계 자유 서술
            content = DIGEST
        else:
            props = set(schema.get("properties", {}))
            if "현재_신분" in props:
                reply = {
                    "한글_이름": "홍길동",
                    "영문_이름": "Gildong Hong",
                    "한글_이름_출처": "원문",
                    "영문_이름_출처": "원문",
                    "현재_신분": "박사",
                }
            elif "박사_학교" in props:
                reply = {"박사_학교": "서울대학교"}
            elif "1저자_논문" in props:
                reply = {"1저자_논문": [], "연구분야_키워드": []}
            else:
                reply = {"경력": []}
            reply.update(overrides.get(_section_of(props), {}))
            content = json.dumps(reply, ensure_ascii=False)
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        )

    client = LLMClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    return calls, client


def _section_of(props: set) -> str:
    if "현재_신분" in props:
        return "basic"
    if "박사_학교" in props:
        return "education"
    if "1저자_논문" in props:
        return "research"
    return "career"


# --- 하드코딩 제거 -----------------------------------------------------------
def test_prompt_has_no_hardcoded_venue_list():
    """학회 판별을 프롬프트에 박아두지 않는다. 분류는 웹에서 사람이 관리한다."""
    for name in ("KCC", "KSC", "한국정보과학회", "대한전자공학회", "CVPR", "Nature"):
        assert name not in _RESEARCH_HINT, f"프롬프트에 {name} 이 하드코딩돼 있다"


def test_prompts_do_not_assume_a_fixed_layout():
    """CV 는 정해진 양식이 없으므로 위치·순서를 가정하면 안 된다."""
    for hint in (_EDU_HINT, _RESEARCH_HINT, _CAREER_HINT):
        assert "첫 줄" not in hint
        assert "맨 위" not in hint


def test_no_regex_parsing_of_cv_body():
    """본문을 정규식으로 파싱하지 않는다 (LLM 이 판단한다)."""
    import inspect

    from cvtool import extract

    source = inspect.getsource(extract)
    assert "import re" not in source
    assert "re.compile" not in source


# --- enum 에 '불명' ----------------------------------------------------------
def test_status_enum_allows_unknown():
    """'불명' 이 없으면 모델이 판단 못 해도 억지로 하나를 찍는다."""
    assert "불명" in 현재_신분_ENUM


def test_unknown_status_is_flagged_for_review():
    calls, client = _recording_client({"basic": {"현재_신분": "불명"}})
    rec = extract_cv_from_text("이력서", client=client)
    assert rec.현재_신분 == "불명"
    assert rec.검토_필요 == "Y"
    assert "현재_신분" in rec.검토_사유


# --- 2단계 추출 --------------------------------------------------------------
def test_two_stage_runs_free_form_pass_first():
    """1단계는 스키마 없이 호출돼야 한다 (추론 모델이 생각할 자리)."""
    calls, client = _recording_client()
    extract_cv_from_text("이력서 본문", client=client, two_stage=True)
    assert "guided_json" not in calls[0]
    assert "response_format" not in calls[0]
    assert len(calls) == 5  # 읽기 1 + 섹션 4


def test_two_stage_feeds_digest_into_structured_calls():
    calls, client = _recording_client()
    extract_cv_from_text("이력서 본문", client=client, two_stage=True)
    for payload in calls[1:]:
        content = payload["messages"][-1]["content"]
        assert DIGEST in content, "정리 노트가 구조화 단계에 전달되지 않았다"
        assert "이력서 본문" in content, "원문도 함께 줘야 근거를 확인할 수 있다"


def test_single_stage_skips_read_pass():
    calls, client = _recording_client()
    extract_cv_from_text("이력서 본문", client=client, two_stage=False)
    assert len(calls) == 4
    assert all("guided_json" in c or "response_format" in c for c in calls)


def test_candidate_name_is_passed_to_research_prompt():
    """저자 순서 판별에는 지원자 이름이 필요하다 (굵기 표시는 텍스트에서 사라진다)."""
    calls, client = _recording_client()
    extract_cv_from_text("이력서", client=client, two_stage=False)
    research = next(
        c for c in calls if "1저자_논문" in (c.get("guided_json") or {}).get("properties", {})
    )
    content = research["messages"][-1]["content"]
    assert "홍길동" in content
    assert "Gildong Hong" in content


def test_read_pass_failure_still_produces_record():
    """1단계가 실패해도 원문만으로 진행해야 한다."""
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "guided_json" not in payload and "response_format" not in payload:
            return httpx.Response(500, text="read pass down")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": '{"현재_신분": "박사"}'}}
                ]
            },
        )

    client = LLMClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    rec = extract_cv_from_text("이력서", client=client, two_stage=True)
    assert rec.현재_신분 == "박사"
    assert "1단계 읽기 실패" in rec.검토_사유


# --- 길이 방어 ---------------------------------------------------------------
def test_guard_length_passes_short_text_through():
    text, warn = guard_length("짧은 이력서", limit=1000)
    assert text == "짧은 이력서"
    assert warn == ""


def test_guard_length_truncates_and_warns():
    long_text = "가" * 5000
    text, warn = guard_length(long_text, limit=1000)
    assert len(text) == 1000
    assert "잘랐습니다" in warn
    assert "4,000자" in warn


def test_long_cv_is_not_truncated_by_default():
    """기본은 제한 없음. 컨텍스트가 큰 모델이라 자를 이유가 없다."""
    from cvtool.config import settings

    assert settings.max_input_chars == 0, "기본값이 무제한이어야 한다"
    calls, client = _recording_client()
    long_cv = "가" * 100000
    extract_cv_from_text(long_cv, client=client, two_stage=False)
    sent = calls[0]["messages"][-1]["content"]
    assert long_cv in sent, "CV 본문이 잘린 채로 전달됐다"


def test_truncation_still_flags_when_limit_configured():
    """컨텍스트가 작은 모델로 바꿔 제한을 걸면, 잘린 사실이 드러나야 한다."""
    text, warn = guard_length("가" * 5000, limit=1000)
    assert len(text) == 1000
    assert "잘랐습니다" in warn


def test_guard_length_disabled_with_zero():
    text, warn = guard_length("가" * 100, limit=0)
    assert len(text) == 100
    assert warn == ""


# --- 빈 입력 -----------------------------------------------------------------
def test_empty_text_raises():
    with pytest.raises(ValueError):
        extract_cv_from_text("   ")
