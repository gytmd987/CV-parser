#!/usr/bin/env python3
"""LLM 응답 원본을 그대로 보여주는 진단 스크립트.

`json.loads` 가 왜 실패하는지 눈으로 확인하기 위한 것. 아무것도 자르지 않는다.

    python3 tools/diagnose_llm.py tests/sample_cv.txt

출력 중 아래를 확인하세요.
  - finish_reason         : length 면 잘림, stop 이면 정상 종료
  - reasoning_content     : 값이 있으면 추론 모델 -> content 와 분리돼 있음
  - content 원본          : ```json 펜스나 <think> 태그가 붙어 있는지
  - guided_json 반영 여부 : 두 방식 각각 시도해 비교
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from cvtool.config import settings  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {
        "이름": {"type": "string"},
        "총_경력_개월": {"type": "integer"},
        "보유_스킬": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["이름", "총_경력_개월", "보유_스킬"],
}


def probe(label: str, payload: dict) -> None:
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    print("=" * 70)
    print(f"[{label}]")
    print("=" * 70)
    try:
        r = httpx.post(url, json=payload, timeout=settings.llm_timeout)
    except httpx.HTTPError as exc:
        print(f"  요청 실패: {exc}\n")
        return

    print(f"  HTTP {r.status_code}")
    if r.status_code >= 400:
        print(f"  본문: {r.text[:1000]}\n")
        return

    body = r.json()
    choice = body["choices"][0]
    msg = choice.get("message", {})

    print(f"  finish_reason   : {choice.get('finish_reason')!r}")
    print(f"  usage           : {body.get('usage')}")
    print(f"  message 키 목록 : {list(msg.keys())}")

    reasoning = msg.get("reasoning_content")
    if reasoning:
        print(f"  reasoning_content 존재! (길이 {len(reasoning)}) -> 추론 모델입니다")
        print(f"    앞 200자: {reasoning[:200]}")

    content = msg.get("content")
    print(f"  content 길이    : {len(content) if content else 0}")
    print("  --- content 원본 (자르지 않음) ---")
    print(content)
    print("  --- 원본 끝 ---")

    if content:
        try:
            json.loads(content)
            print("  ✅ json.loads 성공")
        except json.JSONDecodeError as exc:
            print(f"  ❌ json.loads 실패: {exc}")
            print("     -> 위 content 를 보고 무엇이 덧붙었는지 확인하세요")
    print()


def main() -> int:
    cv_path = sys.argv[1] if len(sys.argv) > 1 else "tests/sample_cv.txt"
    cv_text = Path(cv_path).read_text(encoding="utf-8")
    messages = [{"role": "user", "content": f"다음 이력서에서 정보를 추출해라.\n\n{cv_text}"}]
    base = {"model": settings.llm_model, "temperature": 0.0, "messages": messages}

    probe("1. guided_json (vLLM 확장)", {**base, "guided_json": SCHEMA, "max_tokens": 2048})
    probe(
        "2. response_format (OpenAI 표준)",
        {
            **base,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "cv", "schema": SCHEMA, "strict": True},
            },
            "max_tokens": 2048,
        },
    )
    probe("3. 스키마 없이 (모델 기본 습성 확인)", {**base, "max_tokens": 2048})

    print("=" * 70)
    print("이 출력 전체를 그대로 복사해서 알려주시면 원인을 특정하겠습니다.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
