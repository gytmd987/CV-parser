"""명령줄 도구.

    cvtool extract <파일>          # CV 파일 -> 구조화 JSON
    cvtool extract --text "..."    # 텍스트 직접 입력
    cvtool health                  # 로컬 서비스(vLLM/TEI) 연결 확인

실제 로컬 LLM 이 떠 있는 서버에서 실행하세요.
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import settings


def _cmd_extract(args: argparse.Namespace) -> int:
    from .extract import extract_cv_from_file, extract_cv_from_text

    try:
        if args.text is not None:
            record = extract_cv_from_text(args.text)
        else:
            record = extract_cv_from_file(args.path)
    except Exception as exc:  # noqa: BLE001 - CLI 경계에서 사용자에게 그대로 보고
        print(f"[오류] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(record.model_dump(), ensure_ascii=False, indent=2))
    return 0


def _cmd_health(_args: argparse.Namespace) -> int:
    import httpx

    checks = [
        ("vLLM", f"{settings.llm_base_url.rstrip('/')}/models", "GET", None),
        ("TEI embed", f"{settings.embed_url.rstrip('/')}/embed", "POST", {"inputs": ["테스트"]}),
    ]
    ok = True
    for name, url, method, body in checks:
        try:
            with httpx.Client(timeout=5) as c:
                r = c.get(url) if method == "GET" else c.post(url, json=body)
            status = "OK" if r.status_code < 400 else f"HTTP {r.status_code}"
            ok = ok and r.status_code < 400
        except httpx.HTTPError as exc:
            status = f"연결 실패 ({exc})"
            ok = False
        print(f"  {name:12s} {url}  -> {status}")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cvtool", description="지원자 CV 분석 툴")
    sub = p.add_subparsers(dest="command", required=True)

    ext = sub.add_parser("extract", help="CV 파일/텍스트를 구조화 추출")
    ext.add_argument("path", nargs="?", help="CV 파일 경로 (.pdf/.docx/.txt)")
    ext.add_argument("--text", help="파일 대신 이력서 텍스트를 직접 전달")
    ext.set_defaults(func=_cmd_extract)

    h = sub.add_parser("health", help="로컬 서비스 연결 확인")
    h.set_defaults(func=_cmd_health)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "extract" and args.path is None and args.text is None:
        parser.error("파일 경로 또는 --text 중 하나가 필요합니다.")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
