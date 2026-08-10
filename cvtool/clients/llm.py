"""로컬 vLLM 클라이언트 (OpenAI 호환).

구조화 출력은 두 방식 모두 지원하고, guided_json 실패 시 표준 방식으로 폴백한다.
  1) guided_json     : vLLM 확장. 스키마 강제 (권장)
  2) response_format : OpenAI 표준 json_schema. guided_json 이 안 먹을 때 대체

응답 정제(_clean_content)가 중요하다. finish_reason=stop 인데 json.loads 가 실패하는
경우가 실제로 있었고, 원인 후보가 여럿이라 전부 방어한다.
  - 추론 모델의 <think>...</think> 접두 (모델명 thinkingcap)
  - ```json ... ``` 마크다운 펜스
  - JSON 앞뒤에 붙은 설명 문장
  - reasoning_content 필드로 추론이 분리돼 오는 경우

주의: 여기서 모델을 새로 올리지 않는다. 이미 떠 있는 서비스를 호출만 한다.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..config import settings


class LLMError(RuntimeError):
    """LLM 호출/파싱 실패. 원본 응답을 raw 에 담아 진단할 수 있게 한다."""

    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


class LLMTruncated(LLMError):
    """finish_reason=length. 출력이 토큰 한도에 걸려 잘린 경우."""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def build_json_payload(
    messages: list[dict],
    schema: dict,
    *,
    mode: str = "guided_json",
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    schema_name: str = "extraction",
) -> dict:
    """구조화 출력 요청 payload 를 만든다."""
    payload: dict[str, Any] = {
        "model": model or settings.llm_model,
        "temperature": temperature,
        "messages": messages,
        # 명시하지 않으면 서버 설정에 따라 조용히 잘릴 수 있다.
        "max_tokens": max_tokens or settings.llm_max_tokens,
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


def _strip_reasoning(content: str) -> str:
    """추론 블록을 걷어낸다.

    닫는 태그가 없거나 여는 태그만 오는 경우도 있어서 세 가지를 모두 처리한다.
    """
    text = _THINK_RE.sub("", content)
    # 짝이 안 맞고 </think> 만 남았으면 그 뒤가 답이다
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.strip()


def _json_candidates(text: str) -> list[str]:
    """문자열 안의 최상위 JSON 객체들을 전부 찾아낸다 (중첩은 제외).

    모델이 JSON 앞뒤에 설명을 붙이거나, 추론 중 예시 JSON 을 남기거나,
    같은 객체를 두 번 내보내는 일이 흔하다.
    """
    out: list[str] = []
    depth = 0
    in_str = False
    escape = False
    start = -1
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start : i + 1])
                start = -1
    return out


def _score(candidate: dict, schema: dict | None) -> int:
    """스키마 속성과 얼마나 겹치는지. 예시 JSON 과 진짜 답을 가른다."""
    if not schema:
        return len(candidate)
    props = set((schema or {}).get("properties", {}))
    return sum(1 for k in candidate if k in props)


def extract_json(content: str, schema: dict | None = None) -> dict:
    """모델 응답 문자열에서 JSON 객체를 뽑아낸다.

    JSON 뒤에 "위와 같이 추출했습니다" 한 줄만 붙어도 json.loads 는
    'Extra data' 로 실패한다. 사람 눈에는 정상인 응답이므로 반드시 견뎌야 한다.
    """
    text = _strip_reasoning(content)

    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()

    # 통째로 파싱되면 그대로 (가장 흔한 정상 경로)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # 앞뒤에 뭔가 붙었거나 객체가 여러 개인 경우: 스키마와 가장 잘 맞는 것을 고른다
    best: dict | None = None
    best_score = -1
    for chunk in _json_candidates(text):
        try:
            candidate = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not isinstance(candidate, dict):
            continue
        score = _score(candidate, schema)
        if score >= best_score:  # 동점이면 뒤쪽(최종 답)을 택한다
            best, best_score = candidate, score
    if best is not None:
        return best

    raise LLMError("응답에서 JSON 객체를 찾지 못했습니다.", raw=content)


def parse_response(body: dict, schema: dict | None = None) -> dict:
    """OpenAI 호환 응답 본문에서 JSON 객체를 뽑아낸다.

    실패 시 LLMError.raw 에 **자르지 않은** 원본을 담는다.
    (이전 버전이 200자로 잘라 보여줘서 원인 파악을 방해했다.)
    """
    try:
        choice = body["choices"][0]
        message = choice.get("message", {})
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"예상치 못한 응답 형태: {str(body)[:500]}") from exc

    finish = choice.get("finish_reason")
    content = message.get("content") or ""

    if finish == "length":
        raise LLMTruncated(
            "출력이 토큰 한도에 걸려 잘렸습니다. max_tokens 를 늘리거나 "
            "추출 섹션을 더 잘게 나누세요.",
            raw=content,
        )

    if not content.strip():
        # 추론 모델이 content 를 비우고 reasoning_content 에만 쓰는 경우가 있다
        reasoning = message.get("reasoning_content") or ""
        if reasoning.strip():
            content = reasoning
        else:
            raise LLMError("응답 content 가 비어 있습니다.", raw=str(message)[:1000])

    return extract_json(content, schema)


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
        max_tokens: int | None = None,
        schema_name: str = "extraction",
    ) -> dict:
        """구조화 출력을 받아 dict 로 파싱해 반환."""
        url = f"{self._base}/chat/completions"
        last_err: Exception | None = None

        for mode in ("guided_json", "response_format"):
            payload = build_json_payload(
                messages,
                schema,
                mode=mode,
                temperature=temperature,
                max_tokens=max_tokens,
                schema_name=schema_name,
            )
            try:
                resp = self._client.post(url, json=payload, headers=self._headers)
            except httpx.HTTPError as exc:
                raise LLMError(f"LLM 요청 실패: {exc}") from exc

            if resp.status_code == 400:
                last_err = LLMError(f"{mode} 거부됨(400): {resp.text[:300]}")
                continue
            if resp.status_code >= 400:
                raise LLMError(f"LLM 오류 {resp.status_code}: {resp.text[:300]}")

            try:
                return parse_response(resp.json(), schema)
            except LLMTruncated:
                raise  # 잘림은 폴백해도 같으므로 즉시 보고
            except LLMError as exc:
                # guided_json 이 조용히 무시돼 자유 생성된 경우 -> 표준 방식으로 재시도
                last_err = exc
                continue

        assert last_err is not None
        raise LLMError(
            f"구조화 출력 실패(두 방식 모두): {last_err}",
            raw=getattr(last_err, "raw", None),
        )

    def chat_text(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        """스키마 없이 자유 서술을 받는다 (2단계 추출의 1단계).

        guided_json 은 첫 토큰부터 JSON 문법을 강제해서 추론 모델이 생각할
        자리를 없앤다. 읽고 정리하는 단계에서는 문법을 풀어준다.
        """
        payload = {
            "model": settings.llm_model,
            "temperature": temperature,
            "messages": messages,
            "max_tokens": max_tokens or settings.llm_max_tokens,
        }
        url = f"{self._base}/chat/completions"
        try:
            resp = self._client.post(url, json=payload, headers=self._headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 요청 실패: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"LLM 오류 {resp.status_code}: {resp.text[:300]}")

        body = resp.json()
        try:
            choice = body["choices"][0]
            message = choice.get("message", {})
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"예상치 못한 응답 형태: {str(body)[:500]}") from exc

        content = message.get("content") or ""
        if not content.strip():
            content = message.get("reasoning_content") or ""
        # 추론 블록은 정리 결과가 아니므로 걷어낸다
        return _THINK_RE.sub("", content).strip()

    def close(self) -> None:
        self._client.close()
