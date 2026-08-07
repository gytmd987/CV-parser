"""LLM 을 목킹한 오프라인 파이프라인 테스트.

실제 로컬 LLM 없이 (즉 이 클라우드/CI 환경에서) 추출 파이프라인 로직을 검증한다.
실제 LLM 대상 검증은 서비스가 떠 있는 온프레미스 서버에서 `cvtool health` / `cvtool extract` 로.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cvtool.clients.llm import LLMClient, build_json_payload
from cvtool.extract import extract_cv_from_file, extract_cv_from_text
from cvtool.ingestion.parsers import extract_text
from cvtool.schemas import CV_JSON_SCHEMA, CVRecord

SAMPLE = Path(__file__).parent / "sample_cv.txt"

FAKE_OUTPUT = {
    "이름": "홍길동",
    "이메일": "hong@example.com",
    "연락처": "010-1234-5678",
    "총_경력_개월": 75,
    "최종학력": "석사",
    "보유_스킬": ["Python", "FastAPI", "PostgreSQL", "Kafka", "Kubernetes", "Docker"],
    "경력": [
        {"회사": "가나다소프트", "직무": "백엔드 개발자", "시작": "2018.03", "종료": "2021.02"},
        {"회사": "라마디테크", "직무": "시니어 백엔드 엔지니어", "시작": "2021.03", "종료": "2024.06"},
    ],
}


def _mock_transport(captured: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        body = {"choices": [{"message": {"content": json.dumps(FAKE_OUTPUT, ensure_ascii=False)}}]}
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def _mock_client(captured: dict) -> LLMClient:
    http = httpx.Client(transport=_mock_transport(captured))
    return LLMClient(client=http)


def test_build_payload_guided_json():
    payload = build_json_payload([{"role": "user", "content": "x"}], CV_JSON_SCHEMA)
    assert payload["guided_json"] == CV_JSON_SCHEMA
    assert payload["temperature"] == 0.0


def test_build_payload_response_format_fallback():
    payload = build_json_payload(
        [{"role": "user", "content": "x"}], CV_JSON_SCHEMA, mode="response_format"
    )
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["schema"] == CV_JSON_SCHEMA


def test_extract_from_text_uses_guided_json_and_validates():
    captured: dict = {}
    record = extract_cv_from_text(SAMPLE.read_text(encoding="utf-8"), client=_mock_client(captured))
    # guided_json 이 실제로 요청에 실렸는지 확인
    assert "guided_json" in captured["payload"]
    assert captured["payload"]["temperature"] == 0.0
    # 출력이 CVRecord 로 검증됐는지
    assert isinstance(record, CVRecord)
    assert record.이름 == "홍길동"
    assert record.총_경력_개월 == 75
    assert "Python" in record.보유_스킬
    assert len(record.경력) == 2


def test_extract_from_file_txt():
    captured: dict = {}
    record = extract_cv_from_file(SAMPLE, client=_mock_client(captured))
    assert record.이름 == "홍길동"


def test_guided_json_400_falls_back_to_response_format():
    """서버가 guided_json 을 거부(400)하면 response_format 으로 폴백해야 한다."""
    seen_modes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "guided_json" in payload:
            seen_modes.append("guided_json")
            return httpx.Response(400, text="unknown field guided_json")
        seen_modes.append("response_format")
        body = {"choices": [{"message": {"content": json.dumps(FAKE_OUTPUT, ensure_ascii=False)}}]}
        return httpx.Response(200, json=body)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    record = extract_cv_from_text("홍길동 이력서", client=LLMClient(client=http))
    assert seen_modes == ["guided_json", "response_format"]
    assert record.이름 == "홍길동"


def test_empty_text_raises():
    with pytest.raises(ValueError):
        extract_cv_from_text("   ")


def test_parser_txt_reads_content():
    text = extract_text(SAMPLE)
    assert "홍길동" in text
