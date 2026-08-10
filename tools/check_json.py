#!/usr/bin/env python3
"""모델 응답을 파서에 넣어보는 도구.

"모델 응답은 정상인데 JSON 파싱 실패가 뜬다"를 확인할 때 쓴다.
응답을 파일에 붙여넣고 돌리면, 파서가 무엇을 어떻게 처리하는지 보여준다.

    python3 tools/check_json.py response.txt
    echo '{"a":1} 끝' | python3 tools/check_json.py -
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvtool.clients.llm import (  # noqa: E402
    LLMError,
    _json_candidates,
    _strip_reasoning,
    extract_json,
)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    raw = sys.stdin.read() if sys.argv[1] == "-" else Path(sys.argv[1]).read_text(encoding="utf-8")

    print("=" * 70)
    print(f"[원본] {len(raw):,}자")
    print("=" * 70)
    print(raw)

    print("\n" + "=" * 70)
    print("[1] 그대로 json.loads 하면")
    print("=" * 70)
    try:
        json.loads(raw)
        print("  ✅ 성공 — 원래도 문제없는 응답입니다")
    except json.JSONDecodeError as exc:
        print(f"  ❌ 실패: {exc}")
        if "Extra data" in str(exc):
            print("     -> JSON 뒤에 설명이 붙어 있습니다. 사람 눈엔 정상이지만")
            print("        json.loads 는 이걸 못 넘깁니다. 파서가 걷어내야 합니다.")

    print("\n" + "=" * 70)
    print("[2] 추론 블록 제거 후")
    print("=" * 70)
    stripped = _strip_reasoning(raw)
    print(f"  {len(raw):,}자 -> {len(stripped):,}자")
    if stripped != raw.strip():
        print(f"  {stripped[:400]}")

    print("\n" + "=" * 70)
    print("[3] 찾아낸 JSON 객체 후보")
    print("=" * 70)
    candidates = _json_candidates(stripped)
    if not candidates:
        print("  없음 — 응답에 완결된 { } 객체가 없습니다")
    for i, chunk in enumerate(candidates, 1):
        try:
            parsed = json.loads(chunk)
            keys = list(parsed) if isinstance(parsed, dict) else "(객체 아님)"
            print(f"  {i}. 파싱 OK, 키 {len(parsed) if isinstance(parsed, dict) else 0}개: {keys}")
        except json.JSONDecodeError as exc:
            print(f"  {i}. 파싱 실패: {exc}  |  {chunk[:80]}")

    print("\n" + "=" * 70)
    print("[4] 최종 결과 (실제 파이프라인이 쓰는 값)")
    print("=" * 70)
    try:
        result = extract_json(raw)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("\n  ✅ 파서가 처리합니다.")
    except LLMError as exc:
        print(f"  ❌ {exc}")
        print("\n  이 출력을 그대로 알려주시면 파서를 고치겠습니다.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
