#!/usr/bin/env python3
"""CV 여러 장을 한 번에 돌려 추출 품질을 요약하는 도구.

"웬만한 CV 에 대응되는지"는 한 장으로 알 수 없다. 여러 장을 돌려서
어떤 항목이 자주 비는지, 어떤 파일이 실패하는지를 봐야 한다.

    python3 tools/batch_check.py ~/cv_samples
    python3 tools/batch_check.py ~/cv_samples --csv 결과.csv

결과는 저장되지 않는다(웹 DB 를 건드리지 않음). 순수 점검용이다.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvtool.extract import extract_cv_from_file  # noqa: E402
from cvtool.ingestion.parsers import extract_text  # noqa: E402
from cvtool.schemas import COLUMNS  # noqa: E402
from cvtool.store import SUPPORTED_SUFFIXES  # noqa: E402

# 비어 있으면 곤란한 핵심 항목. 여기가 자주 비면 실사용이 어렵다.
CORE = ["한글_이름", "현재_신분", "박사_학교", "이메일"]


def main() -> int:
    ap = argparse.ArgumentParser(description="CV 여러 장 추출 점검")
    ap.add_argument("folder", help="CV 파일이 든 폴더")
    ap.add_argument("--csv", help="행별 결과를 CSV 로 저장")
    args = ap.parse_args()

    folder = Path(args.folder).expanduser()
    files = sorted(
        f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_SUFFIXES
    )
    if not files:
        print(f"{folder} 에 처리할 파일이 없습니다 ({', '.join(sorted(SUPPORTED_SUFFIXES))})")
        return 1

    print(f"{len(files)}개 파일을 처리합니다. CV 한 장당 LLM 호출 5회라 시간이 걸립니다.\n")
    빈칸_횟수: Counter[str] = Counter()
    rows = []
    실패 = []

    for i, path in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {path.name} ... ", end="", flush=True)
        try:
            글자수 = len(extract_text(path))
        except Exception as exc:  # noqa: BLE001
            print(f"텍스트 추출 실패: {exc}")
            실패.append((path.name, f"텍스트 추출: {exc}"))
            continue
        if 글자수 == 0:
            print("텍스트 0자 (스캔 PDF?)")
            실패.append((path.name, "텍스트 0자"))
            continue

        try:
            rec = extract_cv_from_file(path)
        except Exception as exc:  # noqa: BLE001
            print(f"추출 실패: {type(exc).__name__}: {exc}")
            실패.append((path.name, f"{type(exc).__name__}: {exc}"))
            continue

        row = rec.to_row()
        채움 = sum(1 for c in COLUMNS if row.get(c))
        for c in COLUMNS:
            if not row.get(c):
                빈칸_횟수[c] += 1
        핵심빈칸 = [c for c in CORE if not row.get(c)]
        mark = "⚠" if (rec.검토_필요 == "Y" or 핵심빈칸) else "✓"
        print(f"{mark} {글자수:,}자 -> {채움}/{len(COLUMNS)}항목"
              + (f" | 핵심 누락: {', '.join(핵심빈칸)}" if 핵심빈칸 else ""))
        rows.append((path.name, 글자수, 채움, rec))

    print("\n" + "=" * 70)
    print("[요약]")
    print("=" * 70)
    처리 = len(rows)
    print(f"  처리 성공 : {처리}/{len(files)}")
    if 실패:
        print(f"  실패      : {len(실패)}")
        for name, why in 실패:
            print(f"    - {name}: {why}")
    if not 처리:
        return 1

    평균 = sum(r[2] for r in rows) / 처리
    검토 = sum(1 for r in rows if r[3].검토_필요 == "Y")
    print(f"  평균 채움 : {평균:.1f}/{len(COLUMNS)} 항목")
    print(f"  검토 필요 : {검토}/{처리}")

    print("\n  자주 비는 항목 (많이 빌수록 프롬프트를 손봐야 함):")
    for col, n in 빈칸_횟수.most_common(12):
        bar = "█" * round(n / 처리 * 20)
        print(f"    {col:22s} {n:3d}/{처리}  {bar}")

    print("\n  자주 나온 검토 사유:")
    사유_횟수: Counter[str] = Counter()
    for _, _, _, rec in rows:
        for reason in (rec.검토_사유 or "").split(" / "):
            if reason:
                사유_횟수[reason.split(":")[0][:40]] += 1
    for reason, n in 사유_횟수.most_common(8):
        print(f"    {n:3d}회  {reason}")

    if args.csv:
        import csv

        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["파일", "글자수", "채운항목"] + COLUMNS)
            for name, 글자수, 채움, rec in rows:
                row = rec.to_row()
                w.writerow([name, 글자수, 채움] + [row.get(c, "") for c in COLUMNS])
        print(f"\n  저장됨: {args.csv}")
        print("  ⚠️ 개인정보가 들어 있습니다. 확인 후 삭제하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
