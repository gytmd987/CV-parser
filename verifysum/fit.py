"""글이 칸에서 몇 줄이 되는지 미리 센다.

워드를 띄우지 않고 계산한다. 바탕체 같은 한글 글꼴은 **한글 한 자 = 글자 크기
한 칸(1em), 영문·숫자·기호·띄어쓰기 = 반 칸(0.5em)** 이라 꽤 정확하게 맞는다.

줄바꿈은 **띄어쓰기에서만** 일어난다고 보고 센다. 워드는 설정에 따라 한글 낱말
중간에서도 줄을 바꾸는데, 그쪽이 더 많이 들어가므로 여기 계산이 언제나 같거나
더 많은 줄을 낸다 — "두 줄을 절대 넘지 않게" 에는 이 방향이 안전하다.
"""

from __future__ import annotations

import re
import unicodedata

#: 너비 계산에 남겨 두는 여유 (칸 안쪽 여백·글자 사이 오차)
여유 = 0.97


def 글자폭(ch: str) -> float:
    """em 단위. 한글·한자·전각 = 1, 나머지 = 0.5.

    가운뎃점(·) 같은 '애매한 폭' 글자는 한글 글꼴에서 전각으로 그려지므로 1 로 센다.
    """
    if unicodedata.east_asian_width(ch) in ("W", "F", "A"):
        return 1.0
    return 0.5


def 폭(글: str) -> float:
    return sum(글자폭(c) for c in 글)


def 줄수(글: str, 한줄_em: float) -> int:
    """`한줄_em` 너비 칸에 넣으면 몇 줄인가."""
    한줄 = 한줄_em * 여유
    전체 = 0
    for 문단 in (글 or "").split("\n"):
        줄, 지금 = 1, 0.0
        for 토막 in re.findall(r"\S+|\s+", 문단):
            w = 폭(토막)
            if 토막.isspace():
                if 지금:
                    지금 += w
                continue
            if 지금 + w <= 한줄:
                지금 += w
            elif w <= 한줄:
                줄, 지금 = 줄 + 1, w
            else:                         # 칸보다 긴 낱말은 글자 단위로 끊긴다
                for c in 토막:
                    cw = 글자폭(c)
                    if 지금 + cw > 한줄:
                        줄, 지금 = 줄 + 1, 0.0
                    지금 += cw
        전체 += 줄
    return 전체


def 줄에_맞는_글자수(한줄_em: float, 줄: int = 2) -> int:
    """한글로만 썼을 때 대략 몇 자까지 (프롬프트에 알려 줄 숫자 — 넉넉히 낮춘다)."""
    return max(8, int(한줄_em * 여유 * 줄 * 0.88))


def 표높이(항목: list[tuple[str, str]], 첫열_em: float, 둘째열_em: float) -> float:
    """표 몸통 높이 (줄 단위). 줄마다 칸 위아래 여백으로 0.6줄을 더한다."""
    return sum(max(줄수(a, 첫열_em), 줄수(b, 둘째열_em)) + 0.6 for a, b in 항목)


def 자르기(글: str, 한줄_em: float, 줄: int = 2) -> str:
    """마지막 수단 — 띄어쓰기 단위로 줄 수 안에 들어가는 데까지만."""
    낱말 = 글.split()
    while 낱말 and 줄수(" ".join(낱말), 한줄_em) > 줄:
        낱말.pop()
    return " ".join(낱말).rstrip(",·/ ")
