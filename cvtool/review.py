"""검토 필요 항목 — 사유 한 줄씩을 **처리할 수 있는 일감**으로 다룬다.

`검토_사유` 는 " / " 로 이어 붙인 한 덩어리 글이었다. 화면에 그대로 뿌리니
읽기도 어렵고, 그중 하나를 확인해도 표시할 데가 없었다. 여기서 하는 일은 둘.

1. 한 덩어리를 **항목별로 쪼갠다.**
2. 각 항목이 **어느 열에 대한 이야기인지** 알아낸다. 그래야 상세 화면에서
   그 줄을 짚어 보여줄 수 있다.

2번은 CV 본문을 파싱하는 게 아니다. **우리가 만든 문장을 우리가 아는 열
이름과 맞춰 보는 것**뿐이다. 양쪽 다 이 코드가 쥐고 있다.
"""

from __future__ import annotations

import re

from .schemas import COLUMNS

#: 사유를 잇는 구분자 (extract 가 이걸로 이어 붙인다)
SEP = " / "

#: 열 이름이 문장에 그대로 안 나오는 사유들. 왼쪽 글이 들어 있으면 오른쪽 열들을
#: 가리키는 것으로 본다. **우리가 만든 문장이라** 목록으로 관리할 수 있다.
HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("연구분야 키워드", ("연구분야_키워드",)),
    ("국내/해외 판별", ("1저자_해외논문_제출처",)),
    ("특허", ("특허_등록_수", "특허_출원_수")),
    ("석박통합", ("박사_석박통합", "석사_학교", "석사_전공")),
    ("석사 학력이 없어", ("박사_석박통합", "석사_학교")),
    ("이름", ("한글_이름", "영문_이름")),
    ("경력 기간", ("경력_시작", "경력_종료", "경력_요약")),
    ("기본정보 추출 실패", ("한글_이름", "현재_신분")),
    ("학력 추출 실패", ("박사_학교", "석사_학교", "학사_학교")),
    ("연구 추출 실패", ("연구분야_키워드", "1저자_해외논문_제출처")),
    ("경력 추출 실패", ("경력_요약", "경력_회사")),
    ("CV 없이 직접 등록", ()),
]

#: 긴 열 이름이 짧은 것 안에 들어 있을 때 (박사_학교 vs 학교) 긴 쪽을 먼저 본다
_열들 = sorted(COLUMNS, key=len, reverse=True)


def split(사유: str) -> list[str]:
    """한 덩어리 사유를 항목별로. 빈 것과 구분자만 남은 것은 버린다."""
    조각 = re.split(r"\s+/\s+|\n", 사유 or "")
    return [x.strip() for x in 조각 if x.strip().strip("/")]


def join(항목들: list[str]) -> str:
    return SEP.join(x for x in 항목들 if x.strip())


def columns_for(항목: str) -> list[str]:
    """이 사유가 가리키는 열 이름들 (없으면 빈 목록)."""
    글 = 항목 or ""
    나온것: list[str] = []
    for c in _열들:
        if c in 글 and c not in 나온것:
            나온것.append(c)
    if 나온것:
        return 나온것
    for 힌트, 열들 in HINTS:
        if 힌트 in 글:
            return [c for c in 열들 if c in COLUMNS]
    return []


def items(사유: str, 끝낸것: set[str] | None = None) -> list[dict]:
    """항목마다 {글, 열, 완료}. 화면이 이걸 그대로 그린다."""
    끝낸것 = 끝낸것 or set()
    return [
        {"글": x, "열": columns_for(x), "완료": x in 끝낸것}
        for x in split(사유)
    ]


def remaining(사유: str, 끝낸것: set[str] | None = None) -> list[str]:
    """아직 안 본 항목들."""
    끝낸것 = 끝낸것 or set()
    return [x for x in split(사유) if x not in 끝낸것]


def flagged(사유: str, 끝낸것: set[str] | None = None) -> str:
    """지금 검토_필요 여야 하나. 'Y' 또는 ''."""
    return "Y" if remaining(사유, 끝낸것) else ""


def columns_needing_review(사유: str, 끝낸것: set[str] | None = None) -> set[str]:
    """아직 안 본 항목들이 가리키는 열 전부. 상세 화면에서 그 줄을 짚는다."""
    쓸것: set[str] = set()
    for x in remaining(사유, 끝낸것):
        쓸것.update(columns_for(x))
    return 쓸것


def short(항목: str, 길이: int = 60) -> str:
    """목록에 줄여 쓸 때. 괄호 안 설명은 떼고 앞부분만."""
    글 = re.sub(r"\s*\([^)]*\)\s*$", "", 항목 or "").strip()
    return 글 if len(글) <= 길이 else 글[:길이] + "…"
