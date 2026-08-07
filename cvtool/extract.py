"""CV 구조화 추출 (첫 슬라이스의 핵심).

비정형 이력서 텍스트 -> guided_json 강제 -> 검증된 CVRecord.
"""

from __future__ import annotations

from pathlib import Path

from .clients.llm import LLMClient
from .ingestion.parsers import extract_text
from .schemas import CV_JSON_SCHEMA, CVRecord

_SYSTEM = (
    "너는 채용 담당자를 돕는 이력서 정보 추출기다. "
    "주어진 이력서에서 사실만 추출하고, 없는 정보는 지어내지 마라. "
    "총_경력_개월은 재직 기간을 합산해 정수 개월로 계산한다."
)


def build_messages(cv_text: str) -> list[dict]:
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"다음 이력서에서 정보를 추출해라.\n\n{cv_text}"},
    ]


def extract_cv_from_text(cv_text: str, *, client: LLMClient | None = None) -> CVRecord:
    """이력서 텍스트를 구조화 CVRecord 로 변환."""
    if not cv_text or not cv_text.strip():
        raise ValueError("빈 이력서 텍스트입니다.")
    llm = client or LLMClient()
    owns_client = client is None
    try:
        raw = llm.chat_json(build_messages(cv_text), CV_JSON_SCHEMA, temperature=0.0)
    finally:
        if owns_client:
            llm.close()
    return CVRecord.model_validate(raw)


def extract_cv_from_file(path: str | Path, *, client: LLMClient | None = None) -> CVRecord:
    """CV 파일(PDF/docx/txt)을 구조화 CVRecord 로 변환."""
    text = extract_text(path)
    return extract_cv_from_text(text, client=client)
