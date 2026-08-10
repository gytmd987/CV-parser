#!/usr/bin/env python3
"""추출 전 과정을 단계별로 보여주는 도구.

"답변이 구리다"의 원인이 어느 단계인지 눈으로 확인하기 위한 것.
아무것도 자르지 않고 각 단계의 입력과 출력을 그대로 찍는다.

    python3 tools/trace_extract.py 이력서.pdf
    python3 tools/trace_extract.py 이력서.pdf --no-two-stage   # 1단계 끄고 비교
    python3 tools/trace_extract.py 이력서.pdf --save out.txt

보는 법
  [0] 추출 텍스트   : 사람이 읽어서 말이 되나? 안 되면 파서 문제다.
  [1] 읽기 단계     : 모델이 CV 를 제대로 이해했나? 여기가 틀리면 뒤도 다 틀린다.
  [2] 섹션별 JSON   : 이해는 했는데 항목에 잘못 넣나? 프롬프트 문제다.
  [3] 정규화 후     : 형식 보정이 값을 망가뜨리나?
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvtool import extract as E  # noqa: E402
from cvtool.clients.llm import LLMClient  # noqa: E402
from cvtool.config import settings  # noqa: E402
from cvtool.ingestion.parsers import extract_text  # noqa: E402


def banner(title: str) -> str:
    return f"\n{'=' * 78}\n{title}\n{'=' * 78}"


def main() -> int:
    ap = argparse.ArgumentParser(description="CV 추출 단계별 추적")
    ap.add_argument("path", help="CV 파일 (.pdf/.docx/.txt)")
    ap.add_argument("--no-two-stage", action="store_true", help="1단계 읽기를 건너뛴다")
    ap.add_argument("--save", help="출력을 파일로도 저장")
    args = ap.parse_args()

    out: list[str] = []

    def emit(line: str = "") -> None:
        print(line)
        out.append(line)

    emit(banner("[설정]"))
    emit(f"  모델            : {settings.llm_model}")
    emit(f"  2단계 추출      : {'켜짐' if not args.no_two_stage and settings.two_stage else '꺼짐'}")
    emit(f"  max_tokens      : {settings.llm_max_tokens}")
    emit(f"  temperature     : {settings.llm_temperature}")
    emit(f"  입력 길이 제한  : {settings.max_input_chars or '없음'}")

    emit(banner("[0] 파일에서 뽑은 텍스트 — 사람이 읽어서 말이 되는지 보세요"))
    text = extract_text(args.path)
    emit(f"  (총 {len(text):,}자)")
    emit(text)

    llm = LLMClient()
    two_stage = settings.two_stage and not args.no_two_stage

    digest = ""
    if two_stage:
        emit(banner("[1] 읽기 단계 — 모델이 CV 를 이해했는지 보세요"))
        try:
            digest = E._read_pass(llm, text)
            emit(digest)
        except Exception as exc:  # noqa: BLE001
            emit(f"  실패: {type(exc).__name__}: {exc}")

    data: dict = {}
    sections = [
        ("기본정보", E._BASIC_HINT, "basic"),
        ("학력", E._EDU_HINT, "education"),
        ("연구", E._RESEARCH_HINT, "research"),
        ("경력", E._CAREER_HINT, "career"),
    ]
    schemas = {
        "basic": E.SECTION_BASIC,
        "education": E.SECTION_EDUCATION,
        "research": E.SECTION_RESEARCH,
        "career": E.SECTION_CAREER,
    }

    for label, hint, name in sections:
        emit(banner(f"[2] 섹션: {label}"))
        if name == "research":
            b = data.get("basic", {})
            이름 = " / ".join(v for v in (b.get("한글_이름"), b.get("영문_이름")) if v) or "(파악 실패)"
            hint = hint.format(이름=이름)
            emit(f"  (논문 단계에 넘긴 지원자 이름: {이름})")
        try:
            data[name] = E._ask(llm, hint, schemas[name], text, digest, name)
            emit(json.dumps(data[name], ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001
            data[name] = {}
            emit(f"  실패: {type(exc).__name__}: {exc}")
            raw = getattr(exc, "raw", None)
            if raw:
                emit("  --- 모델 원본 응답 ---")
                emit(raw)

    llm.close()

    emit(banner("[3] 정규화·병합 후 최종 결과 (엑셀에 들어갈 값)"))
    rec = E._assemble(data, [], 지원자_ID="TRACE", 원본_파일명=Path(args.path).name)
    row = rec.to_row()
    width = max(len(k) for k in row)
    for k, v in row.items():
        mark = "  " if v else "· "
        emit(f"  {mark}{k.ljust(width)} : {v}")

    emit(banner("[요약]"))
    빈칸 = [k for k, v in row.items() if not v]
    emit(f"  채워진 항목 : {len(row) - len(빈칸)}/{len(row)}")
    if 빈칸:
        emit(f"  빈 항목     : {', '.join(빈칸)}")
    if rec.검토_사유:
        emit(f"  검토 사유   : {rec.검토_사유}")

    if args.save:
        Path(args.save).write_text("\n".join(out), encoding="utf-8")
        print(f"\n저장됨: {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
