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
            elif "논문" in props:
                reply = {"논문": [], "특허": [], "연구분야_키워드": []}
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
    if "논문" in props:
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
        c for c in calls if "논문" in (c.get("guided_json") or {}).get("properties", {})
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


# --- 서버가 구조화 필드를 무시하는 경우 --------------------------------------
def test_prompt_itself_demands_json():
    """guided_json 에만 의존하면 안 된다.

    서버가 그 필드를 400 없이 조용히 무시하면 모델은 JSON 을 만들 이유가 없어
    산문으로 답한다. 실제로 모든 섹션이 이것 때문에 실패했다.
    """
    from cvtool.extract import json_directive
    from cvtool.schemas import SECTION_BASIC, SECTION_CAREER, SECTION_EDUCATION, SECTION_RESEARCH

    for schema in (SECTION_BASIC, SECTION_EDUCATION, SECTION_RESEARCH, SECTION_CAREER):
        d = json_directive(schema)
        assert "JSON 객체 하나만" in d
        assert "코드펜스" in d
        # 스키마 자체가 프롬프트에 들어가야 모델이 항목을 안다
        for key in schema.get("properties", {}):
            assert key in d


def test_every_section_call_carries_the_directive():
    calls, client = _recording_client()
    extract_cv_from_text("이력서", client=client, two_stage=False)
    for payload in calls:
        assert "JSON 객체 하나만" in payload["messages"][-1]["content"]


def test_extraction_works_when_server_ignores_structured_fields():
    """구조화 필드를 무시하는 서버에서도 프롬프트 지시만으로 뽑혀야 한다."""
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        user = payload["messages"][-1]["content"]
        if "JSON 객체 하나만" not in user:
            content = "\n네, 지원자의 인적사항은 다음과 같습니다. 이름은 홍길동이며..."
        else:
            content = (
                '\n{"한글_이름": "홍길동", "현재_신분": "포닥"}\n\n위와 같이 정리했습니다.'
            )
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        )

    client = LLMClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    rec = extract_cv_from_text("이력서", client=client, two_stage=False)
    assert rec.한글_이름 == "홍길동"
    assert rec.현재_신분 == "포닥"


def test_plain_mode_sends_no_structured_field():
    """마지막 시도는 구조화 필드 없이 프롬프트만으로 요청한다."""
    from cvtool.clients.llm import build_json_payload

    p = build_json_payload([{"role": "user", "content": "x"}], {"properties": {}}, mode="plain")
    assert "guided_json" not in p
    assert "response_format" not in p


def test_falls_through_to_plain_when_both_structured_modes_fail():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "guided_json" in payload:
            seen.append("guided_json")
            return httpx.Response(400, text="unknown field")
        if "response_format" in payload:
            seen.append("response_format")
            return httpx.Response(400, text="not supported")
        seen.append("plain")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": '{"현재_신분": "박사"}'}}
                ]
            },
        )

    client = LLMClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    rec = extract_cv_from_text("이력서", client=client, two_stage=False)
    assert seen[:3] == ["guided_json", "response_format", "plain"]
    assert rec.현재_신분 == "박사"


# --- 한 번에 추출 ------------------------------------------------------------
def _oneshot_client(reply=None, finish="stop"):
    """네 부분을 한 덩어리로 돌려주는 목 클라이언트."""
    calls: list[dict] = []
    통짜 = reply if reply is not None else {
        "basic": {"한글_이름": "홍길동", "영문_이름": "Gildong Hong",
                  "한글_이름_출처": "원문", "영문_이름_출처": "원문",
                  "현재_신분": "박사"},
        "education": {"박사_학교": "서울대학교"},
        "research": {"논문": [], "특허": [], "연구분야_키워드": ["그래프 신경망"]},
        "career": {"경력": []},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        schema = payload.get("guided_json") or (
            payload.get("response_format", {}).get("json_schema", {}).get("schema")
        )
        if schema is None:
            return httpx.Response(200, json={"choices": [
                {"finish_reason": "stop", "message": {"content": DIGEST}}]})
        props = set(schema.get("properties", {}))
        if "basic" in props:                      # 한 덩어리 요청
            return httpx.Response(200, json={"choices": [{
                "finish_reason": finish,
                "message": {"content": json.dumps(통짜, ensure_ascii=False)}}]})
        # 부분별 요청 (되돌아왔을 때)
        조각 = {"basic": {"한글_이름": "홍길동", "영문_이름": "Gildong Hong",
                        "한글_이름_출처": "원문", "영문_이름_출처": "원문",
                        "현재_신분": "박사"},
              "education": {"박사_학교": "서울대학교"},
              "research": {"논문": [], "특허": [], "연구분야_키워드": ["복구됨"]},
              "career": {"경력": []}}[_section_of(props)]
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop",
            "message": {"content": json.dumps(조각, ensure_ascii=False)}}]})

    return calls, LLMClient(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_oneshot_asks_only_twice():
    """CV 전문을 네 번 다시 보내는 게 느린 원인이었다. 한 번만 보낸다."""
    calls, client = _oneshot_client()
    rec = extract_cv_from_text("이력서 본문", client=client, oneshot=True)
    assert len(calls) == 2                       # 통독 1 + 구조화 1
    assert rec.한글_이름 == "홍길동"
    assert rec.박사_학교 == "서울대학교"
    assert "그래프 신경망" in rec.연구분야_키워드


def test_split_mode_still_asks_five_times():
    """기본은 그대로다. 켜야만 바뀐다."""
    calls, client = _recording_client()
    extract_cv_from_text("이력서 본문", client=client, oneshot=False)
    assert len(calls) == 5


def test_oneshot_falls_back_when_the_answer_is_cut(monkeypatch):
    """답이 잘리면 부분별로 다시 받는다 — 느려질 뿐 틀리지 않는다."""
    calls, client = _oneshot_client(finish="length")
    rec = extract_cv_from_text("이력서 본문", client=client, oneshot=True)
    assert rec.한글_이름 == "홍길동"              # 통째로 날아가지 않는다
    assert "복구됨" in rec.연구분야_키워드         # 부분별 답으로 채워졌다
    assert "잘려" in rec.검토_사유                # 무슨 일이 있었는지 남는다


def test_oneshot_keeps_what_came_and_refetches_what_did_not():
    """절반만 온 답도 버리지 않는다. 온 것은 쓰고 빠진 것만 다시 받는다."""
    calls, client = _oneshot_client(reply={
        "basic": {"한글_이름": "김철수", "영문_이름": "Chulsoo Kim",
                  "한글_이름_출처": "원문", "영문_이름_출처": "원문",
                  "현재_신분": "박사"},
        "education": {"박사_학교": "카이스트"},
        # research · career 가 통째로 빠졌다
    })
    rec = extract_cv_from_text("이력서 본문", client=client, oneshot=True)
    assert rec.한글_이름 == "김철수"              # 온 것은 그대로 쓰고
    assert rec.박사_학교 == "카이스트"
    assert "복구됨" in rec.연구분야_키워드         # 빠진 것만 따로 받았다
    assert "빠져" in rec.검토_사유


def test_oneshot_and_split_use_the_same_rules():
    """안내문을 새로 쓰면 두 길이 다른 규칙으로 답한다. 그대로 물려받는다."""
    from cvtool.extract import _ALL_HINT, _BASIC_HINT, _CAREER_HINT, _EDU_HINT

    합친것 = _ALL_HINT.format(basic=_BASIC_HINT, edu=_EDU_HINT,
                            research=_RESEARCH_HINT.format(이름="아무개"),
                            career=_CAREER_HINT)
    for 조각 in (_BASIC_HINT, _EDU_HINT, _CAREER_HINT):
        assert 조각 in 합친것


def test_oneshot_schema_reuses_the_section_schemas():
    """스키마도 마찬가지다. 한쪽만 고치면 두 길이 조용히 어긋난다."""
    from cvtool.schemas import (SECTION_ALL, SECTION_BASIC, SECTION_CAREER,
                                SECTION_EDUCATION, SECTION_RESEARCH)

    assert SECTION_ALL["properties"] == {
        "basic": SECTION_BASIC, "education": SECTION_EDUCATION,
        "research": SECTION_RESEARCH, "career": SECTION_CAREER,
    }
    assert set(SECTION_ALL["required"]) == {"basic", "education", "research", "career"}
