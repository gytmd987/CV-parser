"""엑셀 TEXT 의 **숫자** 서식 — `TEXT(1234.5,"#,##0.0")` → `1,234.5`.

날짜 서식(`'yy.m`)은 `expr.text_format` 이 따로 본다. 서식에 `y` `m` `d` 가
(따옴표 밖에) 있으면 날짜, 아니면 여기로 온다.

되는 것 (엑셀과 같게):

    0  #  ?        자리. 0 은 모자라면 0 을 채우고, # 은 안 채우고, ? 은 빈칸을
    .              소수점
    ,              자리 사이면 천 단위 쉼표, 자리 **뒤**면 1000 으로 나눈다
    %              100 을 곱한다
    E+00 E-00      지수
    "글" \\글       그대로 나가는 글자 (따옴표 없는 한글·기호도 그대로)
    @              값 글자 그대로 (숫자가 아닐 때)
    ;              양수;음수;0;글자 — 엑셀과 같은 차례
    General        그냥 숫자

안 되는 것: 분수(`# ?/?`) — 숫자를 그대로 낸다.

반올림은 엑셀처럼 **0.5 는 올린다** (파이썬 round 의 짝수 맞추기가 아니다).
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_자리 = "0#?"


def _구역나누기(서식: str) -> list[str]:
    """`;` 로 나눈다. 따옴표·`\\` 안의 `;` 는 글자다."""
    구역, 지금, i, 따옴 = [], [], 0, False
    while i < len(서식):
        c = 서식[i]
        if c == '"':
            따옴 = not 따옴
        elif c == "\\" and not 따옴 and i + 1 < len(서식):
            지금.append(서식[i:i + 2])
            i += 2
            continue
        elif c == ";" and not 따옴:
            구역.append("".join(지금))
            지금 = []
            i += 1
            continue
        지금.append(c)
        i += 1
    구역.append("".join(지금))
    return 구역


def 글자만(서식: str) -> str:
    """따옴표·`\\`·`[...]` 안을 뺀 서식 — 날짜 서식인지 볼 때 쓴다."""
    나온, i, 따옴 = [], 0, False
    while i < len(서식):
        c = 서식[i]
        if c == '"':
            따옴 = not 따옴
        elif 따옴:
            pass
        elif c == "\\":
            i += 1
        elif c == "[":
            j = 서식.find("]", i)
            i = j if j >= 0 else len(서식)
        else:
            나온.append(c)
        i += 1
    return "".join(나온)


def 숫자서식인가(서식: str) -> bool:
    남은 = 글자만(서식)
    if 남은.strip().lower() in ("general", "g/표준"):
        return True
    return any(c in 남은 for c in _자리 + "@") and not any(c in 남은.lower() for c in "ymd")


def _쪼개기(구역: str) -> list[tuple[str, str]]:
    """서식 한 구역을 조각으로. ('d', '0') ('.', '') (',', '') ('%', '')
    ('E', '+00') ('@', '') ('lit', 글자)."""
    조각: list[tuple[str, str]] = []
    i = 0
    while i < len(구역):
        c = 구역[i]
        if c == '"':
            j = 구역.find('"', i + 1)
            j = len(구역) if j < 0 else j
            조각.append(("lit", 구역[i + 1:j]))
            i = j + 1
            continue
        if c == "\\" and i + 1 < len(구역):
            조각.append(("lit", 구역[i + 1]))
            i += 2
            continue
        if c == "_" and i + 1 < len(구역):         # 그 글자 너비만큼 빈칸
            조각.append(("lit", " "))
            i += 2
            continue
        if c == "*" and i + 1 < len(구역):         # 채우기 — 칸 너비가 없으니 뺀다
            i += 2
            continue
        if c == "[":                               # [Red] [$-409] 같은 것은 뺀다
            j = 구역.find("]", i)
            i = len(구역) if j < 0 else j + 1
            continue
        if c in "Ee" and i + 1 < len(구역) and 구역[i + 1] in "+-":
            j = i + 2
            while j < len(구역) and 구역[j] in _자리:
                j += 1
            if j > i + 2:
                조각.append(("E", 구역[i + 1:j]))
                i = j
                continue
        if c in _자리:
            조각.append(("d", c))
        elif c in ".,%@":
            조각.append((c, ""))
        else:
            조각.append(("lit", c))
        i += 1
    return 조각


def _반올림(값: Decimal, 자리수: int) -> Decimal:
    return 값.quantize(Decimal(1).scaleb(-자리수), rounding=ROUND_HALF_UP)


def _쉼표(글: str) -> str:
    나온 = []
    for i, c in enumerate(reversed(글)):
        if i and i % 3 == 0:
            나온.append(",")
        나온.append(c)
    return "".join(reversed(나온))


def _정수채우기(조각들: list[tuple[str, str]], 숫자: str, 쉼표: bool) -> str:
    """정수 쪽 자리에 숫자를 **오른쪽부터** 채운다. 남는 숫자는 맨 왼쪽 자리에."""
    자리들 = [i for i, (k, _v) in enumerate(조각들) if k == "d"]
    if not 자리들:
        return "".join(v for k, v in 조각들 if k == "lit")
    if 숫자 == "0":
        숫자 = ""                          # `#.0` 에 0.5 → `.5` (엑셀과 같다)
    if 쉼표:
        # 쉼표 서식(#,##0)은 사이에 글자가 끼지 않는다 — 통째로 만들어 첫 자리에 둔다.
        최소 = sum(1 for i in 자리들 if 조각들[i][1] == "0")
        숫자 = 숫자.rjust(최소, "0")
        채움 = _쉼표(숫자) if 숫자 else ""
        if not 숫자 and any(조각들[i][1] == "?" for i in 자리들):
            채움 = " "
        나온 = []
        for i, (k, v) in enumerate(조각들):
            if k == "lit":
                나온.append(v)
            elif i == 자리들[0]:
                나온.append(채움)
        return "".join(나온)
    나온: list[str] = [""] * len(조각들)
    남은 = 숫자
    for i in reversed(range(len(조각들))):
        k, v = 조각들[i]
        if k == "lit":
            나온[i] = v
            continue
        if i == 자리들[0]:
            나온[i] = 남은 if 남은 else ("0" if v == "0" else " " if v == "?" else "")
            남은 = ""
        elif 남은:
            나온[i], 남은 = 남은[-1], 남은[:-1]
        else:
            나온[i] = "0" if v == "0" else " " if v == "?" else ""
    return "".join(나온)


def _소수채우기(조각들: list[tuple[str, str]], 숫자: str) -> str:
    자리 = [v for k, v in 조각들 if k == "d"]
    글 = list(숫자.ljust(len(자리), "0"))
    # 뒤쪽의 # · ? 자리는 0 이면 지운다 (? 는 빈칸으로)
    for j in reversed(range(len(자리))):
        if 자리[j] == "0" or 글[j] != "0":
            break
        글[j] = " " if 자리[j] == "?" else ""
    나온, j = [], 0
    for k, v in 조각들:
        if k == "lit":
            나온.append(v)
        elif k == "d":
            나온.append(글[j])
            j += 1
    return "".join(나온)


def _일반(값: float) -> str:
    if 값 == int(값) and abs(값) < 1e15:
        return str(int(값))
    return f"{값:.10g}"


def _구역으로(구역: str, 값: float, 부호붙이기: bool) -> str:
    if 글자만(구역).strip().lower() in ("general", "g/표준"):
        return ("-" if 부호붙이기 and 값 < 0 else "") + _일반(abs(값))
    if "/" in 글자만(구역):
        # 분수 서식(`# ?/?`)은 안 한다 — 엉뚱한 글자를 내느니 숫자 그대로.
        return ("-" if 부호붙이기 and 값 < 0 else "") + _일반(abs(값))
    if not any(k == "d" for k, _v in _쪼개기(구역)):
        # 자리가 없다 — 글자만 (`"없음"`), 또는 `@` 자리에 숫자 그대로.
        return "".join(_일반(값) if k == "@" else v for k, v in _쪼개기(구역)
                       if k in ("@", "lit"))
    조각들 = [c for c in _쪼개기(구역) if c[0] != "@"]
    수 = Decimal(repr(abs(값)))
    수 *= Decimal(100) ** sum(1 for k, _v in 조각들 if k == "%")
    조각들 = [("lit", "%") if k == "%" else (k, v) for k, v in 조각들]   # 곱했으니 이제 글자

    # 정수/소수/지수 자리 나누기
    끝 = next((i for i, (k, _v) in enumerate(조각들) if k == "E"), len(조각들))
    지수조각 = 조각들[끝] if 끝 < len(조각들) else None
    본체, 꼬리 = 조각들[:끝], 조각들[끝 + 1:]
    점 = next((i for i, (k, _v) in enumerate(본체) if k == "."), None)
    정수쪽 = 본체 if 점 is None else 본체[:점]
    소수쪽 = [] if 점 is None else 본체[점 + 1:]

    # 쉼표: 자리 사이면 천 단위, **마지막 자리 뒤**면 1000 으로 나누기 (`0.0,,` 는 백만)
    끝자리 = max((i for i, (k, _v) in enumerate(본체) if k == "d"), default=-1)
    나누기 = sum(1 for i, (k, _v) in enumerate(본체) if k == "," and i > 끝자리)
    정수끝 = max((i for i, (k, _v) in enumerate(정수쪽) if k == "d"), default=-1)
    쉼표 = any(k == "," for i, (k, _v) in enumerate(정수쪽) if i < 정수끝)
    수 /= Decimal(1000) ** 나누기
    정수쪽 = [c for c in 정수쪽 if c[0] != ","]
    소수쪽 = [c for c in 소수쪽 if c[0] not in (",", ".")]
    소수자리 = sum(1 for k, _v in 소수쪽 if k == "d")

    지수글 = ""
    if 지수조각 is not None:
        정수자리 = max(1, sum(1 for k, _v in 정수쪽 if k == "d"))
        지수 = 0 if 수 == 0 else math.floor(math.log10(float(수))) - (정수자리 - 1)
        가수 = _반올림(수.scaleb(-지수), 소수자리)
        if 가수 >= Decimal(10) ** 정수자리:        # 9.99 → 10.0 이 되면 한 칸 민다
            지수 += 1
            가수 = _반올림(수.scaleb(-지수), 소수자리)
        수 = 가수
        표시, 자리 = 지수조각[1][0], 지수조각[1][1:]
        지수글 = ("E" + ("-" if 지수 < 0 else ("+" if 표시 == "+" else ""))
                 + str(abs(지수)).rjust(자리.count("0"), "0"))
    else:
        수 = _반올림(수, 소수자리)

    정수글, _, 소수글 = f"{수:f}".partition(".")
    나온 = _정수채우기(정수쪽, 정수글, 쉼표)
    if 점 is not None:
        나온 += "." + _소수채우기(소수쪽, 소수글)
    나온 += 지수글 + "".join(v for k, v in 꼬리 if k == "lit")
    if 부호붙이기 and 값 < 0 and any(c in "123456789" for c in 나온):
        나온 = "-" + 나온
    return 나온


def 숫자서식(값, 서식: str, 글자: str) -> str:
    """엑셀 TEXT 와 같게. `글자` 는 값을 글로 본 것 (숫자가 아닐 때 그대로 쓴다)."""
    구역들 = _구역나누기(서식)
    try:
        수 = float(값) if isinstance(값, (int, float)) else float(
            Decimal(글자.strip().replace(",", "")))
    except (InvalidOperation, ValueError):
        수 = None
    if 수 is None or not math.isfinite(수):
        # 숫자가 아니면 글자 구역(4번째) 또는 @ 가 있는 구역에 넣는다. 없으면 그대로.
        글구역 = 구역들[3] if len(구역들) >= 4 else next(
            (g for g in 구역들 if "@" in 글자만(g)), None)
        if 글구역 is None:
            return 글자
        return "".join(글자 if k == "@" else v for k, v in _쪼개기(글구역)
                       if k in ("@", "lit"))
    if 수 < 0 and len(구역들) >= 2 and 구역들[1] != "":
        return _구역으로(구역들[1], 수, False)
    if 수 == 0 and len(구역들) >= 3 and 구역들[2] != "":
        return _구역으로(구역들[2], 수, False)
    return _구역으로(구역들[0], 수, True)
