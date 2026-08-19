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
    models_body = None
    for name, url, method, body in checks:
        try:
            with httpx.Client(timeout=5) as c:
                r = c.get(url) if method == "GET" else c.post(url, json=body)
            status = "OK" if r.status_code < 400 else f"HTTP {r.status_code}"
            ok = ok and r.status_code < 400
            if name == "vLLM" and r.status_code < 400:
                models_body = r.json()
        except httpx.HTTPError as exc:
            status = f"연결 실패 ({exc})"
            ok = False
        print(f"  {name:12s} {url}  -> {status}")

    # 컨텍스트 한도를 알려줘야 CVTOOL_MAX_INPUT_CHARS 를 감으로 정하지 않는다.
    if models_body:
        for m in models_body.get("data", []) or []:
            limit = m.get("max_model_len")
            if limit:
                print(f"\n  모델 {m.get('id')} 의 max_model_len = {limit:,} 토큰")
                print(f"  현재 CVTOOL_MAX_INPUT_CHARS = {settings.max_input_chars:,} 자")
                print("  (한글은 대략 1자≈1토큰. 출력 몫도 남겨야 하니 여유 있게 잡으세요)")
    print(f"\n  2단계 추출: {'켜짐' if settings.two_stage else '꺼짐'}"
          f" (CVTOOL_TWO_STAGE)")
    return 0 if ok else 1


def _cmd_deps(_args: argparse.Namespace) -> int:
    """어떤 패키지가 있고 없는지, 없으면 무엇이 안 되는지 보여준다.

    "없음"과 "깔려는 있는데 망가짐"을 구분한다. 후자는 의존성이 깨진 경우로
    (예: pypdf 는 있는데 cryptography 가 망가져 import 가 죽는다),
    ImportError 가 아닌 예외가 날아와 확인 명령 자체를 죽일 수 있다.
    """
    import importlib

    checks = [
        ("httpx", "필수", "LLM/TEI 호출 — 없으면 추출 자체가 불가능"),
        ("pydantic", "필수", "추출 결과 검증 — 없으면 추출 자체가 불가능"),
        ("pypdf", "선택", ".pdf 읽기 (4.0+ 이면 2단 편집도 살림)"),
        ("docx", "선택", ".docx 읽기 (표 내용 포함)"),
        ("pytest", "개발", "테스트 실행"),
    ]
    dist_name = {"docx": "python-docx"}
    없음: list[str] = []
    깨짐: list[tuple[str, str]] = []
    print()

    for module, level, why in checks:
        try:
            mod = importlib.import_module(module)
        except ImportError:
            print(f"  ❌ {module:12s} {'없음':12s} [{level}] {why}")
            if level == "필수":
                없음.append(dist_name.get(module, module))
            continue
        except BaseException as exc:  # noqa: BLE001 - 깨진 설치는 어떤 예외든 날 수 있다
            print(f"  ⚠️  {module:12s} {'설치 깨짐':12s} [{level}] {type(exc).__name__}: {exc}")
            깨짐.append((dist_name.get(module, module), f"{type(exc).__name__}: {exc}"))
            continue

        version = getattr(mod, "__version__", "")
        if not version:
            try:
                import importlib.metadata as md

                version = md.version(dist_name.get(module, module))
            except Exception:  # noqa: BLE001
                version = "?"
        print(f"  ✅ {module:12s} {str(version):12s} [{level}] {why}")

    print()
    if 깨짐:
        print("  설치는 됐는데 import 가 실패합니다 (의존성이 깨진 상태):")
        for name, why in 깨짐:
            print(f"    - {name}: {why}")
        print(f"    pip install --force-reinstall {' '.join(n for n, _ in 깨짐)}")
    if 없음:
        print(f"  필수 패키지가 없습니다: {', '.join(없음)}")
        print("    pip install -r requirements.txt")
    if not 없음 and not 깨짐:
        print("  전부 정상입니다.")
    return 1 if 없음 else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cvtool", description="지원자 관리 툴 (CV 구조화 추출)")
    sub = p.add_subparsers(dest="command", required=True)

    ext = sub.add_parser("extract", help="CV 파일/텍스트를 구조화 추출")
    ext.add_argument("path", nargs="?", help="CV 파일 경로 (.pdf/.docx/.txt)")
    ext.add_argument("--text", help="파일 대신 이력서 텍스트를 직접 전달")
    ext.set_defaults(func=_cmd_extract)

    h = sub.add_parser("health", help="로컬 서비스 연결 확인")
    h.set_defaults(func=_cmd_health)

    d = sub.add_parser("deps", help="필요한 패키지 설치 여부 확인")
    d.set_defaults(func=_cmd_deps)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "extract" and args.path is None and args.text is None:
        parser.error("파일 경로 또는 --text 중 하나가 필요합니다.")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
