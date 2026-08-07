"""로컬 vLLM 클라이언트 (OpenAI 호환).

구조화 출력은 두 방식 모두 지원하고, guided_json 실패 시 자동으로 표준 방식으로 폴백한다.
  1) guided_json          : vLLM 확장. 스키마를 강제 (권장).
  2) response_format      : OpenAI 표준 json_schema. guided_json 이 안 먹을 때 대체.

참고(수정 금지): 기존 RAG /opt/data-gov/app/clients/llm.py 의 build_json_payload.

주의: 여기서 모델을 새로 올리지 않는다. 이미 떠 있는 서비스를 호출만 한다.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..config import settings


class LLMError(RuntimeError):
    pass


def build_json_payload(
    messages: list[dict],
    schema: dict,
    *,
    mode: str = "guided_json",
    model: str | None = None,
    temperature: float = 0.0,
    schema_name: str = "extraction",
) -> dict:
    """구조화 출력 요청 payload 를 만든다.

    mode="guided_json"     -> vLLM 확장 필드 사용
    mode="response_format" -> OpenAI 표준 json_schema 사용
    """
    payload: dict[str, Any] = {
        "model": model or settings.llm_model,
        "temperature": temperature,
        "messages": messages,
    }
    if mode == "guided_json":
        payload["guided_json"] = schema
    elif mode == "response_format":
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        }
    else:  # pragma: no cover - 방어적
        raise ValueError(f"unknown mode: {mode}")
    return payload


class LLMClient:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._base = settings.llm_base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
        self._client = client or httpx.Client(timeout=settings.llm_timeout)

    def chat_json(
        self,
        messages: list[dict],
        schema: dict,
        *,
        temperature: float = 0.0,
        schema_name: str = "extraction",
    ) -> dict:
        """구조화 출력을 받아 dict 로 파싱해 반환.

        guided_json 을 먼저 시도하고, 400 등으로 실패하면 response_format 으로 폴백한다.
        """
        url = f"{self._base}/chat/completions"
        last_err: Exception | None = None
        for mode in ("guided_json", "response_format"):
            payload = build_json_payload(
                messages, schema, mode=mode, temperature=temperature, schema_name=schema_name
            )
            try:
                resp = self._client.post(url, json=payload, headers=self._headers)
            except httpx.HTTPError as exc:  # 네트워크 오류는 폴백해도 소용 없음
                raise LLMError(f"LLM 요청 실패: {exc}") from exc
            if resp.status_code == 400:
                # 서버가 guided_json 을 모를 때 등 -> 다음 모드로 폴백
                last_err = LLMError(f"{mode} 거부됨(400): {resp.text[:200]}")
                continue
            if resp.status_code >= 400:
                raise LLMError(f"LLM 오류 {resp.status_code}: {resp.text[:200]}")
            return _parse_json_content(resp.json())
        raise LLMError(f"구조화 출력 실패(두 방식 모두): {last_err}")

    def close(self) -> None:
        self._client.close()


def _parse_json_content(body: dict) -> dict:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"예상치 못한 응답 형태: {str(body)[:200]}") from exc
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMError(f"JSON 파싱 실패: {content[:200]}") from exc
