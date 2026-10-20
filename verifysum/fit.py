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


def 감싸기(글: str, 한줄_em: float, 앞폭: float = 0.0, 다음폭: float | None = None) -> list[str]:
    """한 문단을 칸에서 보이는 줄들로 나눈다 (띄어쓰기에서 끊고, 칸보다 긴 낱말은 글자로).

    `앞폭` · `다음폭` 은 첫 줄 · 다음 줄 앞에 붙을 머리('- ', 들여쓰기 칸) 의 너비(em).
    """
    다음폭 = 앞폭 if 다음폭 is None else 다음폭
    줄들: list[str] = []
    지금, 너비 = "", 0.0

    def 한도() -> float:
        return 한줄_em * 여유 - (다음폭 if 줄들 else 앞폭)

    for 토막 in re.findall(r"\S+|\s+", 글 or ""):
        if 토막.isspace():
            if 지금:
                지금, 너비 = 지금 + 토막, 너비 + 폭(토막)
            continue
        w = 폭(토막)
        if 너비 + w <= 한도():
            지금, 너비 = 지금 + 토막, 너비 + w
            continue
        if 지금.strip():
            줄들.append(지금.rstrip())
            지금, 너비 = "", 0.0
        if w <= 한도():
            지금, 너비 = 토막, w
            continue
        for c in 토막:                   # 칸보다 긴 낱말은 글자 단위로 끊긴다
            cw = 글자폭(c)
            if 너비 + cw > 한도() and 지금:
                줄들.append(지금)
                지금, 너비 = "", 0.0
            지금, 너비 = 지금 + c, 너비 + cw
    if 지금.strip() or not 줄들:
        줄들.append(지금.rstrip())
    return 줄들


def 줄수(글: str, 한줄_em: float) -> int:
    """`한줄_em` 너비 칸에 넣으면 몇 줄인가 (줄 앞 띄어쓰기는 세지 않는다)."""
    return sum(len(감싸기(문단.strip(), 한줄_em)) for 문단 in (글 or "").split("\n"))


# --- 글머리 ('- ' · '· ') ------------------------------------------------------------
#: 줄 맨 앞에 오면 글머리로 보는 기호
글머리들 = "-–—·•∙●○▪*ㆍ‧◦"


def 항목_나누기(글: str) -> list[str]:
    """칸 글을 내용 덩어리로. 글머리로 시작하는 줄이 새 덩어리이고, 글머리 없는 줄은
    앞 덩어리에 이어 붙인다 (손으로 줄을 바꾼 것으로 본다). 글머리가 하나도 없으면 줄마다 한 덩어리."""
    줄들 = [x.strip() for x in (글 or "").splitlines() if x.strip()]
    있음 = any(x[0] in 글머리들 for x in 줄들)
    나온것: list[str] = []
    for x in 줄들:
        if x[0] in 글머리들:
            나온것.append(x.lstrip(글머리들).strip())
        elif 있음 and 나온것:
            나온것[-1] = (나온것[-1] + " " + x).strip()
        else:
            나온것.append(x)
    return [x for x in 나온것 if x]


def 이음(머리: str) -> str:
    """글머리 없는 줄 앞에 넣어 글자 시작을 맞추는 띄어쓰기 ('- ' → 두 칸)."""
    return " " * max(1, round(폭(머리) / 0.5))


def 글머리_줄들(덩어리: str, 한줄_em: float, 머리: str, 들임: str | None = None) -> list[str]:
    """덩어리 하나를 칸에서 보이는 줄들로. 첫 줄은 `머리`, 다음 줄은 띄어쓰기로 시작을 맞춘다."""
    들임 = 이음(머리) if 들임 is None else 들임
    줄들 = 감싸기(덩어리, 한줄_em, 폭(머리), 폭(들임))
    return [(머리 if i == 0 else 들임) + x for i, x in enumerate(줄들)]


def 글머리_맞추기(글: str, 한줄_em: float, 머리: str, 들임: str | None = None) -> str:
    """칸 글 전체를 글머리 모양으로 (줄마다 '\n')."""
    return "\n".join(줄 for 덩 in 항목_나누기(글) for 줄 in 글머리_줄들(덩, 한줄_em, 머리, 들임))


def 줄에_맞는_글자수(한줄_em: float, 줄: int = 2) -> int:
    """한글로만 썼을 때 대략 몇 자까지 (프롬프트에 알려 줄 숫자 — 넉넉히 낮춘다)."""
    return max(8, int(한줄_em * 여유 * 줄 * 0.88))


def 표높이(항목: list[tuple[str, str]], 첫열_em: float, 둘째열_em: float) -> float:
    """표 몸통 높이 (줄 단위). 줄마다 칸 위아래 여백으로 0.6줄, 칸 안에서 '- ' 로
    새 내용이 시작될 때마다 벌린 간격으로 0.5줄을 더한다."""
    def 사이(b: str) -> float:
        return 0.5 * max(0, sum(1 for x in b.splitlines() if x.lstrip().startswith("-")) - 1)
    return sum(max(줄수(a, 첫열_em), 줄수(b, 둘째열_em) + 사이(b)) + 0.6 for a, b in 항목)


def 자르기(글: str, 한줄_em: float, 줄: int = 2, 머리: str = "", 들임: str | None = None) -> str:
    """마지막 수단 — 띄어쓰기 단위로 줄 수 안에 들어가는 데까지만 (덩어리 하나)."""
    낱말 = 글.split()
    while 낱말 and len(글머리_줄들(" ".join(낱말), 한줄_em, 머리, 들임) if 머리
                     else 감싸기(" ".join(낱말), 한줄_em)) > 줄:
        낱말.pop()
    return " ".join(낱말).rstrip(",·/ ")
