"""시트 블록의 계산기 — 칸 주소를 값으로 바꿔 주는 층.

대시보드의 다른 블록은 칸마다 수식이 **외톨이**였다. 옆 칸 값을 가져다 쓸 수가
없어서, 비율 하나를 내려면 같은 집계식을 두 번 통째로 적어야 했다. 여기서는
엑셀처럼 `=B2/B3*100` 이라고 쓴다.

계산기를 새로 만들지 않는다. 이미 둘이 있다.

    expr.py     `=IF(A1>10,"많음","적음")` — 이름을 **값 묶음(dict)** 에서 찾는다
    formula.py  `=COUNT(지원자, 부서="A")` — 지원자 줄들을 센다

`expr.py` 의 이름 규칙이 `A1`·`B2` 를 그냥 이름으로 읽으므로, 값 묶음에 칸 값을
넣어 주면 **지금 코드 그대로** 칸 참조가 된다. 이 파일이 하는 일은 그 값 묶음을
만들어 주는 것뿐이다.

칸 하나를 계산하는 차례는 넷이다.

    1. `=` 로 시작 안 하면 글자 그대로
    2. 범위 펼치기   `SUM(A1:A3)` -> `SUM(A1,A2,A3)`
    3. 집계 먼저     `COUNT(지원자, 부서="A")` -> 그 자리에 값을 박는다
    4. 나머지를 `expr.evaluate(식, 값들)` 로

2번을 `expr.py` 에 `:` 연산자를 넣어 풀지 않는 까닭: 그 파일은 목록·프로필·
조건서식이 함께 쓰는 자리라, 문법을 늘리면 그쪽까지 다시 봐야 한다.

3번이 있어야 집계와 계산을 **섞어 쓸 수** 있다 —
`=COUNT(지원자, 부서="A") / COUNT(지원자) * 100`.
"""

from __future__ import annotations

import re

from . import expr as E
from . import formula as F
from .export import col_letter
from .xlsx_read import col_index

#: 격자 크기 상한. 넘으면 JSON 도 화면도 감당이 안 된다.
MAX_ROWS = 100
MAX_COLS = 26          # A~Z. 더 늘리려면 col_letter 가 AA 도 내주니 숫자만 올리면 된다

#: 칸 주소 한 덩어리. `$` 는 **옮길 때 안 밀린다**는 표시다 (엑셀과 같다).
#: 앞뒤에 낱말 글자가 붙으면 주소가 아니다 (`부서A1` 은 열 이름일 수 있다).
_주소글 = r"\$?[A-Z]{1,2}\$?[1-9][0-9]{0,3}"
#: `\w` 로 본다 — 한글도 낱말 글자다. `부서A1` 은 열 이름이지 주소가 아니다.
_앞 = r"(?<![\w$])"
_뒤 = r"(?![\w$])"

#: 칸 주소 (`A1`, `AB12`, `$A$1`). 대문자만 — 소문자는 열 이름과 헷갈린다.
#: 1=열고정($), 2=열, 3=행고정($), 4=행
_주소_RE = re.compile(_앞 + r"(\$?)([A-Z]{1,2})(\$?)([1-9][0-9]{0,3})" + _뒤)
#: 범위 (`A1:B3`, `$A$1:$B$3`)
_범위_RE = re.compile(_앞 + f"({_주소글}):({_주소글})" + _뒤)
#: 딱 주소 하나 (`A3`). 조건 값이 칸을 가리키는지 볼 때 쓴다.
_주소하나_RE = re.compile(_주소글)

#: 옮기다 격자 밖으로 나간 참조. 엑셀의 `#REF!` 자리다 — **조용히 지우면**
#: 수식이 그럴듯한 딴 칸을 가리키게 되어 아무도 틀린 줄 모른다.
밖_표시 = "#참조!"


class SheetError(ValueError):
    """시트 칸을 계산할 수 없다."""


def 주소(행: int, 열: int) -> str:
    """0-based 자리 -> `A1`. (0,0) 이 A1 이다."""
    return f"{col_letter(열)}{행 + 1}"


def 자리(주소글: str) -> tuple[int, int]:
    """`A1` -> (0, 0). `$A$1` 도 같은 자리다. 주소가 아니면 ValueError."""
    m = re.fullmatch(r"\$?([A-Za-z]{1,2})\$?([0-9]+)", (주소글 or "").strip())
    if not m:
        raise ValueError(f"칸 주소가 아닙니다: {주소글!r}")
    return int(m.group(2)) - 1, col_index(m.group(1).upper())


def 고정떼기(글: str) -> str:
    """수식에서 `$` 를 뗀다. 따옴표 안은 그대로 둔다.

    `$` 는 **옮길 때만** 쓰는 표시다 — 계산할 때 `$A$1` 과 `A1` 은 같은 칸이다.
    계산기(`expr`)의 이름 규칙에 `$` 가 없으므로, 계산에 넘기기 전에 여기서
    뗀다. 그래야 `expr` 에 문법을 하나 더 얹지 않아도 된다.
    """
    def 한번(조각: str) -> str:
        return _주소_RE.sub(lambda m: m.group(2) + m.group(4), 조각)
    return _따옴표_밖에서(주소대문자(글 or ""), 한번)


#: 소문자로 적은 칸 주소 (`a1`, `$b$2`). 뒤에 `(` 가 오면 함수 이름이라 뺀다.
_소문자주소_RE = re.compile(r"(?<![\w$])(\$?[A-Za-z]{1,2}\$?[1-9][0-9]{0,3})(?![\w$(])")


def 주소대문자(글: str) -> str:
    """`=countif(부서,a1)` 의 `a1` 을 `A1` 로 — 엑셀도 소문자 주소를 받아 대문자로 바꾼다.

    따옴표 안은 안 건드린다 (`"a1"` 은 글자다).
    """
    if not 글 or not E.is_formula(글):
        return 글
    return _따옴표_밖에서(글, lambda 조각: _소문자주소_RE.sub(lambda m: m.group(1).upper(), 조각))


def 옮기기(글: str, 행차: int, 열차: int, *, 행수: int = MAX_ROWS,
        열수: int = MAX_COLS) -> str:
    """수식을 `행차`·`열차` 만큼 옮긴 것으로 다시 쓴다 (자동 채우기·복사).

    엑셀과 같은 규칙이다. `=SUM(A1:C1)` 을 한 줄 아래로 채우면
    `=SUM(A2:C2)` 가 된다. `$` 가 붙은 쪽은 **안 밀린다** —
    `=SUM($A$1:$C$1)` 은 어디로 옮겨도 그대로다. 열만 고정(`$A1`)도,
    행만 고정(`A$1`)도 된다.

    수식이 아니면(= 로 시작 안 하면) 글자 그대로 돌려준다. 따옴표 안도 안
    건드린다 — `="A1 을 보라"` 의 A1 은 주소가 아니라 글이다.

    격자 밖으로 나가면 `#참조!` 로 바꾼다. 조용히 0 열로 붙여 두면 수식이
    그럴듯한 딴 칸을 가리키게 되어 아무도 틀린 줄 모른다.
    """
    if not E.is_formula(글 or "") or (행차 == 0 and 열차 == 0):
        return 글
    def 하나(m: re.Match) -> str:
        열고정, 열글, 행고정, 행글 = m.groups()
        r = int(행글) - 1 + (0 if 행고정 else 행차)
        c = col_index(열글) + (0 if 열고정 else 열차)
        if not (0 <= r < 행수 and 0 <= c < 열수):
            return 밖_표시
        return f"{열고정}{col_letter(c)}{행고정}{r + 1}"
    return _따옴표_밖에서(글, lambda 조각: _주소_RE.sub(하나, 조각))


def _따옴표_밖에서(글: str, 바꾸기):
    """따옴표 **밖**의 조각에만 `바꾸기` 를 먹인다.

    `="A1:A3 을 더한다"` 처럼 글 안에 주소처럼 생긴 것이 있어도 안 건드려야 한다.
    """
    나온것, 지금, 따옴표 = [], [], ""
    for ch in 글:
        if 따옴표:
            지금.append(ch)
            if ch == 따옴표:
                나온것.append("".join(지금))
                지금, 따옴표 = [], ""
            continue
        if ch in "\"'":
            나온것.append(바꾸기("".join(지금)))
            지금, 따옴표 = [ch], ch
            continue
        지금.append(ch)
    나온것.append("".join(지금) if 따옴표 else 바꾸기("".join(지금)))
    return "".join(나온것)


def 참조밀기(글: str, 축: str, 기준: int, 차: int, *, 행수: int = MAX_ROWS,
          열수: int = MAX_COLS) -> str:
    """행·열을 끼우거나 뺄 때 수식의 칸 참조를 따라 옮긴다 (엑셀과 같다).

    `축` 은 "행" 또는 "열", `기준` 은 0-based 자리. `차` 가 양수면 그 자리에
    `차` 개를 **끼운** 것 — 기준 이상을 가리키던 참조가 그만큼 밀린다.
    음수면 기준부터 `-차` 개를 **뺀** 것 — 뺀 줄을 가리키던 참조는 `#참조!`,
    그 뒤를 가리키던 참조는 당겨진다.

    `$` 가 붙어 있어도 옮긴다. `$` 는 «채우기·복사 때 안 밀린다» 는 뜻이지,
    가리키던 칸이 자리를 옮겼는데 엉뚱한 칸을 가리키라는 뜻이 아니다 (엑셀도
    그렇게 한다).
    """
    if not E.is_formula(글 or "") or not 차:
        return 글

    def 하나(m: re.Match) -> str:
        열고정, 열글, 행고정, 행글 = m.groups()
        r, c = int(행글) - 1, col_index(열글)
        i = r if 축 == "행" else c
        if 차 < 0 and 기준 <= i < 기준 - 차:
            return 밖_표시
        if i >= 기준:
            i += 차
        r, c = (i, c) if 축 == "행" else (r, i)
        if not (0 <= r < 행수 and 0 <= c < 열수):
            return 밖_표시
        return f"{열고정}{col_letter(c)}{행고정}{r + 1}"
    return _따옴표_밖에서(글, lambda 조각: _주소_RE.sub(하나, 조각))


#: 칸 자리를 읽는 함수들. 계산기(`expr`)는 칸을 모르므로, 넘기기 전에 여기서
#: 숫자나 속 함수로 바꿔 넣는다.
#: 칸 **자리**를 읽는 함수 — 계산기에 넘기기 전에 숫자로 바꾼다. INDEX·MATCH·
#: VLOOKUP·ROWS 는 이제 계산기가 범위를 그대로 받는다 (`expr._f_index2` …) —
#: 그래야 지원자 열(`한글_이름`)도 범위로 넘길 수 있다.
_위치함수 = ("ROW", "COLUMN")


def _인자나누기(속: str) -> list[str]:
    """쉼표로 인자를 가른다. 따옴표 안과 **괄호 안**의 쉼표는 안 가른다.

    `formula._split_args` 는 집계 조건용이라 괄호를 안 센다 —
    `INDEX(B1:B3, MATCH("감", A1:A3, 0))` 가 거기서는 네 조각이 된다.
    """
    조각, 지금, 깊이, 따 = [], [], 0, ""
    for ch in 속:
        if 따:
            지금.append(ch)
            if ch == 따:
                따 = ""
            continue
        if ch in "\"'":
            따 = ch
        elif ch == "(":
            깊이 += 1
        elif ch == ")":
            깊이 -= 1
        elif ch == "," and 깊이 == 0:
            조각.append("".join(지금).strip())
            지금 = []
            continue
        지금.append(ch)
    조각.append("".join(지금).strip())
    return 조각


def _범위모양(글: str) -> tuple[list[str], int, int]:
    """`A1:C3` 또는 `A1` → (칸 주소들 줄 순서, 높이, 너비). 범위가 아니면 SheetError."""
    글 = (글 or "").strip()
    m = _범위_RE.fullmatch(글)
    if m:
        (r1, c1), (r2, c2) = 자리(m.group(1)), 자리(m.group(2))
    elif _주소하나_RE.fullmatch(글):
        r1, c1 = r2, c2 = 자리(글)
    else:
        raise SheetError(f"칸 범위를 적어야 합니다 (예: A1:C5): {글}")
    r0, r9, c0, c9 = min(r1, r2), max(r1, r2), min(c1, c2), max(c1, c2)
    if (r9 - r0 + 1) * (c9 - c0 + 1) > MAX_ROWS * MAX_COLS:
        raise SheetError(f"범위가 너무 넓습니다: {글}")
    칸 = [주소(r, c) for r in range(r0, r9 + 1) for c in range(c0, c9 + 1)]
    return 칸, r9 - r0 + 1, c9 - c0 + 1


def 위치함수풀기(글: str, 현재칸: str | None = None) -> str:
    """ROW·COLUMN 을 숫자로 바꾼다 — 칸의 **자리**는 계산기가 모른다.

        ROW()            → 이 칸의 행 번호      ROW(B7)        → 7
        COLUMN()         → 이 칸의 열 번호      COLUMN(C1)     → 3

    INDEX·MATCH·VLOOKUP·ROWS·COLUMNS 는 계산기가 범위를 그대로 받는다.
    """
    if not 글 or not any(f"{이름}(" in 글.upper().replace(" ", "") for 이름 in _위치함수):
        return 글
    이름RE = re.compile(r"(" + "|".join(sorted(_위치함수, key=len, reverse=True)) + r")\s*\(",
                      re.IGNORECASE)

    def 바꾸기(이름: str, 인자: list[str]) -> str:
        if 이름 in ("ROW", "COLUMN"):
            if not 인자 or not 인자[0]:
                if 현재칸 is None:
                    return f"{이름}()"          # 칸이 없는 자리 — 계산기가 말해 준다
                r, c = 자리(현재칸)
                return str(r + 1 if 이름 == "ROW" else c + 1)
            칸, _h, _w = _범위모양(인자[0])
            r, c = 자리(칸[0])
            return str(r + 1 if 이름 == "ROW" else c + 1)
        return f"{이름}()"

    for _ in range(20):                      # 안쪽에 또 있을 수 있다 (INDEX(…, MATCH(…)))
        바뀜 = False
        나온것: list[str] = []
        i = 0
        while i < len(글):
            ch = 글[i]
            if ch in "\"'":
                j = i + 1
                while j < len(글) and 글[j] != ch:
                    j += 1
                나온것.append(글[i:min(j + 1, len(글))])
                i = j + 1
                continue
            m = 이름RE.match(글, i)
            if not m or (i and (글[i - 1].isalnum() or 글[i - 1] in "_$")):
                나온것.append(ch)
                i += 1
                continue
            깊이, j, 따 = 1, m.end(), ""
            while j < len(글) and 깊이:
                c = 글[j]
                if 따:
                    if c == 따:
                        따 = ""
                elif c in "\"'":
                    따 = c
                elif c == "(":
                    깊이 += 1
                elif c == ")":
                    깊이 -= 1
                j += 1
            if 깊이:
                raise SheetError(f"괄호가 안 닫혔습니다: {글[i:][:40]}")
            속 = 글[m.end():j - 1]
            새것 = 바꾸기(m.group(1).upper(), _인자나누기(속) if 속.strip() else [])
            나온것.append(새것)
            바뀜 = 바뀜 or 새것 != 글[i:j]
            i = j
        글 = "".join(나온것)
        if not 바뀜:
            break
    return 글


def 범위펼치기(글: str) -> str:
    """`A1:A3` 을 `A1,A2,A3` 으로. 따옴표 안은 그대로 둔다."""
    def 한번(조각: str) -> str:
        def 펼치기(m: re.Match) -> str:
            r1, c1 = 자리(m.group(1))
            r2, c2 = 자리(m.group(2))
            칸수 = (abs(r2 - r1) + 1) * (abs(c2 - c1) + 1)
            if 칸수 > MAX_ROWS * MAX_COLS:
                raise SheetError(f"범위가 너무 넓습니다: {m.group(0)}")
            return ",".join(
                 주소(r, c)
                 for r in range(min(r1, r2), max(r1, r2) + 1)
                 for c in range(min(c1, c2), max(c1, c2) + 1)
            )
        return _범위_RE.sub(펼치기, 조각)
    return _따옴표_밖에서(글, 한번)


def 범위묶기(글: str) -> str:
    """`A1:B3` 을 `_범위(3,2,A1,B1,…)` 로. 칸들을 **범위 하나**로 넘긴다.

    `범위펼치기` 처럼 칸들로 흩어 넘기면 `COUNTIF(A1:A10, ">3")` 의 인자 수가
    달라져 어디까지가 범위인지 알 수 없다. 범위로 묶어 두면 SUM 처럼 칸들이
    필요한 함수는 알아서 펼쳐 받는다 (`expr._부르기`).
    """
    def 한번(조각: str) -> str:
        def 묶기(m: re.Match) -> str:
            r1, c1 = 자리(m.group(1))
            r2, c2 = 자리(m.group(2))
            h, w = abs(r2 - r1) + 1, abs(c2 - c1) + 1
            if h * w > MAX_ROWS * MAX_COLS:
                raise SheetError(f"범위가 너무 넓습니다: {m.group(0)}")
            칸 = [주소(r, c) for r in range(min(r1, r2), max(r1, r2) + 1)
                 for c in range(min(c1, c2), max(c1, c2) + 1)]
            return f"_범위({h},{w},{','.join(칸)})"
        return _범위_RE.sub(묶기, 조각)
    return _따옴표_밖에서(글, 한번)


#: 엑셀 모양(범위, 조건, …)으로 부를 수 있는 이름. 예전 모양
#: (`COUNTIFS(지원자, 부서="A")`) 과 이름이 같아서, 인자 모양으로 가른다.
_엑셀조건함수 = ("COUNTIF", "COUNTIFS", "SUMIF", "SUMIFS", "AVERAGEIF", "AVERAGEIFS")


def _옛조건인가(조각: str, 아는열) -> bool:
    """`부서="A"` · `저널_수>2` 처럼 **열 이름 연산자 값** 한 덩어리인가 (예전 모양)."""
    m = F._COND_RE.match(조각)
    if not m:
        return False
    이름 = m.group(1).strip()
    if 이름 != F._unquote(이름):
        return False                          # "…" 로 시작한다 (엑셀 모양의 조건 값)
    if _주소하나_RE.fullmatch(이름) and not (아는열 and 이름 in 아는열):
        return False                          # 칸 주소 (그런 이름의 열이 없으면)
    if not E._이름_RE.fullmatch(이름) and not 이름.startswith("["):
        return False
    return 아는열 is None or 이름.strip("[]") in 아는열


def _집계인가(함수: str, 인자: list[str], 아는열) -> bool:
    """이 호출이 **사람을 세는 집계**인가, 칸을 셈하는 함수인가.

    `SUM`·`MIN`·`MAX`·`COUNT`·`AVERAGE` 는 두 계산기에 똑같이 있다. 넷으로 가른다.
    """
    if not 인자:
        return 함수 not in E.FUNC_NAMES
    if 함수 in E.FUNC_NAMES and 함수 not in F.FUNCTIONS and 함수 not in F.ALIASES:
        return False                         # 계산기에만 있는 이름 (XLOOKUP·INDEX…)
    if F._unquote(인자[0]) in F.TARGETS:
        return True                          # =COUNT(지원자, ...)
    if 함수 in _엑셀조건함수:
        # 예전 모양은 조건이 `열=값` 한 덩어리다. 엑셀 모양은 범위와 조건이
        # 따로 온다 — `COUNTIFS(부서, "A")`. 그건 계산기(expr)가 한다.
        return any(_옛조건인가(x, 아는열) for x in 인자)
    if 함수 not in E.FUNC_NAMES:
        return True                          # 집계에만 있는 이름 (PCT·LIST…)

    첫 = 인자[0].strip()
    if (not F._COND_RE.match(첫) and 첫 == F._unquote(첫)
            and not _주소하나_RE.fullmatch(첫) and not _범위_RE.fullmatch(첫)
            and not 첫.replace(".", "", 1).lstrip("-").isdigit()
            and E._이름_RE.fullmatch(첫)):
        # 첫 인자가 **맨 낱말**이다 (칸 주소도 숫자도 따옴표도 아니다).
        # 대상이나 열을 적으려 한 것이다 — `=AVERAGE(저널_수)` 는 맞고
        # `=COUNT(없는대상)` 은 틀렸는데, 둘 다 집계로 보내야 "대상을 적으려
        # 했다면…" 이라는 제대로 된 말이 나온다. expr 로 보내면
        # "모르는 열입니다" 로 끝나 대상을 잘못 쓴 줄 모른다.
        return True

    if 아는열:
        for 조각 in 인자:
            m = F._COND_RE.match(조각)
            이름 = (m.group(1).strip() if m else F._unquote(조각)).strip()
            if 이름 in 아는열:
                # =COUNT(부서="A") — 표의 열을 가리키는 조건이 들어 있다.
                return True
    return False


def _따옴표(값: str) -> str:
    """조건 값으로 넣을 수 있게 감싼다.

    안 감싸면 공백이나 연산자가 든 값이 조건을 깨뜨린다. 값에 `"` 가 있으면
    `'` 로 감싼다 — `formula._split_args` 와 `_unquote` 가 둘 다 안다.
    """
    글 = str(값 or "")
    따 = "'" if '"' in 글 else '"'
    return 따 + 글.replace(따, "") + 따


def _조건값_풀기(값: str, 값찾기) -> str | None:
    """조건의 값 쪽을 풀어 **실제 값**으로. 풀 것이 없으면 None.

    두 가지를 푼다.

    **칸 주소** — `=COUNTIF(부서=A3)` 의 `A3` 는 「A3 라는 글자」가 아니라 A3
    칸이다. 안 바꾸면 부서가 "A3" 인 사람이 없어 **오류도 없이 0** 이 나온다.

    **함수 호출** — `=COUNTIFS(입사월=MONTH(TODAY()))` 도 똑같이 조용히 0 이
    나왔다. `formula` 는 값 쪽을 글자로만 읽어서 「MONTH(TODAY()) 라는 글자」를
    찾았기 때문이다. `=MONTH(TODAY())` 는 따로 적으면 9 가 나오는데 조건 안에
    넣으면 안 되니, 같은 수식이 자리에 따라 다르게 도는 셈이었다.

    **괄호가 있을 때만 계산한다.** 아무 값이나 계산하면 `번호=10-2020-0012345`
    같은 **멀쩡한 글자가 뺄셈이 되어** 조용히 딴 값을 찾는다. 괄호는 「이건
    함수다」라는 분명한 표시라 그런 사고가 없다. 그래서 `부서=소재분석` 처럼
    따옴표 없이 적은 낱말은 지금처럼 **글자 그대로** 남는다.
    """
    if _주소하나_RE.fullmatch(값):
        return 값찾기(값) if 값찾기 is not None else None
    if "(" not in 값:
        return None
    # 값 안에서 가리킨 칸도 풀어 준다 (`입사월=MONTH(A1)`). 칸이 없는 자리
    # (숫자·축표·자유표)에서는 빈 묶음이라 칸을 안 쓰는 함수만 돈다.
    묶음 = ({a: 값찾기(a) for a in 참조들(값)} if 값찾기 is not None else {})
    try:
        return E._글(E.evaluate("=" + 값, 묶음))
    except (E.ExprError, ValueError) as exc:
        # 조용히 글자로 되돌리면 또 0 이 나온다. 함수를 적었는데 안 도는
        # 까닭을 그 자리에서 말해 준다.
        raise SheetError(f"조건 값을 계산하지 못했습니다: {값} → {exc}") from exc


def _조건에_칸값(인자: list[str], 값찾기) -> list[str]:
    """집계 조건의 **값 쪽**을 풀어 놓는다 (`_조건값_풀기` 를 인자마다).

    **열 쪽은 안 건드린다.** 열 이름이 우연히 `A3` 처럼 생겼을 수 있고, 거기서
    칸을 가리킬 일도 없다. **따옴표로 감싼 값도 안 건드린다** — `부서="A3"` 은
    칸이 아니라 그 글자를 찾겠다는 뜻이다 (`_COND_RE` 가 따옴표째 넘겨주므로
    `_주소하나_RE` 에도 `(` 검사에도 안 걸린다).
    """
    나온것 = []
    for 조각 in 인자:
        m = F._COND_RE.match(조각)
        값 = m.group(3).strip() if m else ""
        푼것 = _조건값_풀기(값, 값찾기) if m else None
        if 푼것 is None:
            나온것.append(조각)
        else:
            나온것.append(f"{m.group(1).strip()}{m.group(2)}{_따옴표(푼것)}")
    return 나온것


def _칸값넣기(글: str, 값찾기) -> str:
    """`=FUNC(...)` 한 줄의 조건 값을 푼다.

    통째로 집계 하나인 수식은 `계산` 이 빠른 길로 `formula.run` 에 바로
    넘기는데, 그 길에서도 조건 값은 풀려 있어야 한다. 안 그러면
    `=COUNTIF(부서=A3)` 만 0 이 되고 `=COUNTIF(부서=A3)/2` 는 맞는,
    앞뒤가 안 맞는 일이 생긴다.

    **`값찾기` 가 없어도 돈다.** 칸이 없는 자리(숫자·축표·자유표)에도 함수는
    있다 — `=COUNTIFS(입사월=MONTH(TODAY()))` 는 거기서도 돌아야 한다.
    """
    m = F._CALL_RE.match(글 or "")
    if not m:
        return 글
    인자 = _조건에_칸값(F._split_args(m.group(2)), 값찾기)
    return f"={m.group(1)}({','.join(인자)})"


def 집계먼저(글: str, rows, 아는열=None, 값찾기=None) -> str:
    """`COUNT(지원자, ...)` 같은 집계 호출을 **값으로 바꿔** 놓는다.

    `formula.py` 는 수식 한 줄이 통째로 집계 호출일 때만 읽는다. 시트에서는
    그것이 더 큰 식의 **한 조각**일 수 있어서, 조각을 찾아 먼저 계산한다.

    **따옴표를 이 함수가 직접 센다.** 집계 호출은 `부서="소재분석"` 처럼 제
    안에 따옴표를 품고 있어서, 바깥에서 따옴표로 미리 잘라 주면 호출이 두
    동강 나 괄호 짝을 못 찾는다.

    **이름만 보고 집계라고 단정하지 않는다.** `SUM`·`MIN`·`MAX` 는 두 계산기에
    똑같이 있다. 첫 인자가 대상(`지원자`·`채용`)일 때만 집계로 본다 —
    `SUM(A1,A2)` 는 칸을 더하는 것이지 지원자를 세는 것이 아니다.
    """
    # **부를 수 있는 이름 전부**를 본다 (COUNTIFS·AVERAGE 같은 별칭 포함).
    # `FUNCTIONS`(계산이 있는 이름)만 보면 별칭이 섞인 식에서 안 잡힌다.
    # 긴 이름을 먼저 봐야 `COUNTIFS` 가 `COUNT` 로 잘리지 않는다.
    함수RE = re.compile(r"(" + "|".join(sorted(F.CALLABLE, key=len, reverse=True))
                      + r")\s*\(")
    나온것: list[str] = []
    i = 0
    while i < len(글):
        ch = 글[i]
        if ch in "\"'":                       # 글자는 통째로 건너뛴다
            j = i + 1
            while j < len(글) and 글[j] != ch:
                j += 1
            나온것.append(글[i:min(j + 1, len(글))])
            i = j + 1
            continue
        m = 함수RE.match(글, i)
        # 앞 글자가 이름의 일부면 함수 이름이 아니다 (`부서COUNT(` 같은 것)
        if not m or (i and (글[i - 1].isalnum() or 글[i - 1] == "_")):
            나온것.append(ch)
            i += 1
            continue
        깊이, j, 따옴표 = 1, m.end(), ""
        while j < len(글) and 깊이:
            c = 글[j]
            if 따옴표:
                if c == 따옴표:
                    따옴표 = ""
            elif c in "\"'":
                따옴표 = c
            elif c == "(":
                깊이 += 1
            elif c == ")":
                깊이 -= 1
            j += 1
        if 깊이:
            raise SheetError(f"괄호가 안 닫혔습니다: {글[i:][:40]}")
        호출 = 글[i:j]
        인자 = F._split_args(글[m.end():j - 1])
        if not _집계인가(m.group(1).upper(), 인자, 아는열):
            # 집계가 아니다. expr 이 알아서 한다 (SUM(A1,A2) 같은 것).
            # **여는 괄호까지만** 내보내고 속은 다시 훑는다 — 그 안에 집계가
            # 들어 있을 수 있다 (`=ROUND(COUNT(지원자)/2, 1)`).
            나온것.append(글[i:m.end()])
            i = m.end()
            continue
        # 조건 값이 칸을 가리키면(`부서=A3`) 먼저 그 칸 값으로 바꾼다.
        호출 = f"{m.group(1)}({','.join(_조건에_칸값(인자, 값찾기))})"
        try:
            보일글, 값 = F.run("=" + 호출, rows, 아는열)
        except F.FormulaError as exc:
            raise SheetError(f"{호출} → {exc}") from exc
        # 숫자면 숫자로, 아니면 따옴표를 씌워 글자로 넣는다.
        나온것.append(repr(float(값)) if isinstance(값, (int, float))
                   else '"' + str(보일글).replace('"', '""') + '"')
        i = j
    return "".join(나온것)


def 참조들(글: str) -> list[str]:
    """이 수식이 읽는 칸 주소들. 따옴표 안은 안 센다."""
    본 = []
    def 한번(조각: str) -> str:
        for m in _주소_RE.finditer(조각):
            if m.group(0) not in 본:
                본.append(m.group(0))
        return 조각
    _따옴표_밖에서(글, 한번)
    return 본


def 계산(수식: str, rows, 아는열=None, 값찾기=None,
       현재칸: str | None = None) -> tuple[str, object]:
    """수식 하나를 끝까지. (보일 글, 값) — `formula.run` 과 **같은 모양**이다.

    대시보드의 네 자리(숫자·축표·자유표·시트)가 전부 이것을 쓴다. 예전에는
    앞의 셋이 `formula.run` 만 불러서 `=함수(대상, 조건...)` 을 **통째로 한 줄**
    로만 받았다 — `=COUNT(지원자)/2` 가 안 됐다. 시트에서는 되는데 숫자
    블록에서는 안 되니 문법이 두 가지처럼 보였다.

    `값찾기(주소)` 를 주면 칸 참조까지 푼다 (시트). 함수로 받는 까닭은 참조가
    **필요할 때 하나씩** 풀려야 하기 때문이다 — 미리 다 계산하면 순환 참조를
    못 잡고, 안 쓰는 칸까지 계산한다.

    **통째로 집계 호출 하나면 `formula.run` 을 그대로 부른다.** 이 빠른 길이
    중요하다 — `PCT` 는 `("50.0%", 50.0)`, `LIST` 는 `("가, 나", [...])` 처럼
    글과 값이 다른 것을 돌려주는데, expr 을 거치면 그 모양이 무너진다. 이미
    저장된 수식은 전부 이 길로 가므로 보이는 것이 안 바뀐다.
    """
    글 = (수식 or "").strip()
    if not E.is_formula(글):
        return 글, None
    if 밖_표시 in 고정떼기(글):
        # 자동 채우기·복사가 격자 밖을 가리키게 만든 자리다. 그냥 두면
        # "모르는 함수입니다: 참조" 같은 엉뚱한 말이 나온다.
        raise SheetError(f"{밖_표시} — 옮기다 격자 밖을 가리켰습니다: {글}")
    # `$` 는 옮길 때만 쓰는 표시다. 계산기에 넘기기 전에 뗀다.
    글 = 고정떼기(글)
    # ROW()·INDEX(A1:C3,…) 처럼 **칸 자리**를 읽는 함수를 먼저 푼다.
    글 = 위치함수풀기(글, 현재칸)
    # 통째로 집계 호출 하나인가. **모양만 보면 안 된다** — 대상을 생략할 수
    # 있게 되면서 `=SUM(A1:A3)` 도 `formula` 가 "지원자의 A1:A3 열을 더해라"
    # 로 읽어 버린다(그런 열이 없으니 조용히 `-`). 칸을 셈하는 것인지
    # 사람을 세는 것인지 `_집계인가` 로 먼저 가른다.
    부름 = F._CALL_RE.match(글)
    집계호출 = bool(부름) and _집계인가(
        부름.group(1).upper(), F._split_args(부름.group(2)), 아는열)
    집계오류 = None
    try:
        if not 집계호출:
            raise F.FormulaError("집계 호출이 아닙니다")
        F.parse(글)
    except F.FormulaError as exc:
        # 집계로 안 읽혔다. 그래도 **모양이 집계 호출이었으면** 까닭을 들고
        # 간다 — 섞인 길에서도 터지면 이쪽 말이 훨씬 친절하다.
        # `=COUNT(없는대상)` 을 "모르는 열입니다" 라고 하면 대상을 잘못 쓴 줄
        # 모른다. 반대로 `=COUNT(지원자)/0` 은 모양부터 집계가 아니므로
        # "=함수(대상, 조건...) 모양이어야 합니다" 가 엉뚱한 말이 된다.
        if 집계호출:
            집계오류 = exc
    else:
        return F.run(_칸값넣기(글, 값찾기), rows, 아는열)
    try:
        식 = 집계먼저(범위묶기(글), rows, 아는열, 값찾기)
        묶음 = ({a: 값찾기(a) for a in 참조들(식)} if 값찾기 is not None else {})
        값 = E.evaluate(식, 집계문맥(묶음, rows, 아는열))
    except (E.ExprError, SheetError, ValueError):
        if 집계오류 is not None:
            raise 집계오류
        raise
    return 값, 값


class 집계문맥(dict):
    """시트·숫자·축표 칸의 계산 문맥. 칸 값은 그대로, **열 이름은 범위**다.

    `부서` 는 지원자 전원의 부서 값 (줄 순서대로). 그래서 엑셀처럼
    `COUNTIFS(부서, "소재분석", 최종상태, "*합격")` · `SUMIFS(저널_수, 부서, A1)` ·
    `TEXTJOIN(", ", TRUE, FILTER(한글_이름, 부서="소재분석"))` 가 된다.
    채용 중인 사람만 보려면 `채용중` 열을 조건으로 건다 (`채용중, "Y"`).
    """

    def __init__(self, 칸값: dict, rows, 아는열) -> None:
        super().__init__(칸값)
        self._줄 = list(getattr(rows, "지원자", []) or []) if rows is not None else []
        self._아는열 = set(아는열 or ()) | ({"채용중"} if self._줄 else set())
        self._열 = {}

    def __contains__(self, 이름) -> bool:
        return dict.__contains__(self, 이름) or 이름 in self._아는열

    def get(self, 이름, 없으면=None):
        if dict.__contains__(self, 이름):
            return dict.get(self, 이름)
        if 이름 in self._아는열:
            if 이름 not in self._열:
                self._열[이름] = E.범위([r.get(이름, "") for r in self._줄])
            return self._열[이름]
        return 없으면


def _열을쓰나(n) -> bool:
    """이 식이 지원자 열(칸 주소가 아닌 이름)을 읽나 — 사람을 세는 범위인가."""
    if isinstance(n, E.Col):
        return (n.이름.upper() not in ("TRUE", "FALSE")
                and not _주소하나_RE.fullmatch(n.이름))
    if isinstance(n, E.Bin):
        return _열을쓰나(n.왼) or _열을쓰나(n.오)
    if isinstance(n, E.Neg):
        return _열을쓰나(n.안)
    if isinstance(n, E.Call):
        return n.이름 != "_범위" and any(_열을쓰나(x) for x in n.인자)
    return False


def _조건집계들(n) -> list:
    """식 안의 엑셀 모양 조건 집계 호출들 (안쪽까지)."""
    나온 = []
    if isinstance(n, E.Call):
        if n.이름 in _엑셀조건함수:
            나온.append(n)
        for x in n.인자:
            나온 += _조건집계들(x)
    elif isinstance(n, E.Bin):
        나온 += _조건집계들(n.왼) + _조건집계들(n.오)
    elif isinstance(n, E.Neg):
        나온 += _조건집계들(n.안)
    return 나온


def 조건집계짜임(수식: str):
    """수식 안에 **사람을 세는** 엑셀 모양 조건 집계가 딱 하나면 (그 호출, 범위·조건 짝들).

    `=COUNTIFS(부서, "A")` 는 물론 `=IFERROR(COUNTIF(…),0)` · `=SUM(COUNTIF(…))` ·
    `=COUNTIF(…)+0` 처럼 **감싸도** 센 사람은 분명하다. 둘 이상
    (`=COUNTIF(…)/COUNTIF(…)`)이면 어느 쪽인지 정할 수 없어 None.

    범위 자리는 지원자 열을 읽어야 한다 (`부서`, `LEFT(생년월일,4)`). 칸 범위
    (`A1:A5`)만 세는 것은 사람이 아니라서 None.
    """
    글 = 위치함수풀기(고정떼기((수식 or "").strip()))
    if not E.is_formula(글):
        return None
    try:
        나무 = E.parse(범위묶기(글))
    except (E.ExprError, SheetError):
        return None
    찾은 = _조건집계들(나무)
    if len(찾은) != 1:
        return None
    나무 = 찾은[0]
    인자 = 나무.인자
    짝 = 인자 if 나무.이름 in ("COUNTIF", "COUNTIFS") else (
        인자[1:] if 나무.이름 in ("SUMIFS", "AVERAGEIFS") else 인자[:2])
    if len(짝) < 2 or len(짝) % 2 or not all(_열을쓰나(짝[i]) for i in range(0, len(짝), 2)):
        return None
    return 나무, 짝


def 조건줄(수식: str, rows, 아는열=None, 값찾기=None,
         현재칸: str | None = None) -> list[dict] | None:
    """수식 안의 조건 집계 하나(`=COUNTIFS(부서, "A", …)`)가 **센 사람들**. 아니면 None."""
    return 조건줄_까닭(수식, rows, 아는열, 값찾기, 현재칸)[0]


def 조건줄_까닭(수식: str, rows, 아는열=None, 값찾기=None,
            현재칸: str | None = None) -> tuple[list[dict] | None, str]:
    """`조건줄` 과 같고, 못 찾았으면 **왜 못 찾았는지**도 돌려준다. (사람들, 까닭)

    대시보드에서 숫자를 누르면 누구인지 보여줄 때 쓴다. 조건 값과 범위에
    함수·칸이 있어도 된다 — 계산해서 쓴다. `현재칸` 은 그 수식이 있는 칸이다
    (`ROW()` 처럼 자리를 읽는 함수가 시트 계산과 같은 값을 내도록).
    """
    짜임 = 조건집계짜임(수식)
    if 짜임 is None:
        return None, "사람을 세는 조건 집계가 하나뿐인 수식이 아닙니다"
    # 시트 계산(`계산`)과 **같은 순서**로 다듬는다 — 다르면 숫자와 명단이 갈린다.
    try:
        글 = 범위묶기(위치함수풀기(고정떼기((수식 or "").strip()), 현재칸))
        글 = 집계먼저(글, rows, 아는열, 값찾기)
        나무 = _조건집계들(E.parse(글))[0]
    except IndexError:
        return None, "사람을 세는 조건 집계를 찾지 못했습니다"
    except (E.ExprError, SheetError, F.FormulaError, ValueError) as exc:
        return None, str(exc)
    인자 = 나무.인자
    짝 = 인자 if 나무.이름 in ("COUNTIF", "COUNTIFS") else (
        인자[1:] if 나무.이름 in ("SUMIFS", "AVERAGEIFS") else 인자[:2])
    # 조건 값에 칸(`A3`)을 썼으면 시트의 그 칸 값으로 푼다.
    칸값 = ({a: 값찾기(a) for a in 참조들(글)} if 값찾기 is not None else {})
    문맥 = 집계문맥(칸값, rows, 아는열)
    try:
        값들 = [E._계산(x, 문맥) for x in 짝]
        # 범위 자리는 **사람마다 하나씩**이어야 한다 — 줄과 짝을 맞춰 고른다.
        for i in range(0, len(값들), 2):
            if not (isinstance(값들[i], E.범위) and len(값들[i]) == len(문맥._줄)):
                return None, f"{i // 2 + 1}번째 범위가 지원자 열이 아닙니다"
        맞음 = E._조건짝(값들, 나무.이름)
    except E.ExprError as exc:
        return None, str(exc)
    return [r for r, m in zip(문맥._줄, 맞음) if m], ""


def 덮인칸(칸들: dict, 행수: int = MAX_ROWS,
        열수: int = MAX_COLS) -> dict[str, str]:
    """병합에 **덮인 칸 -> 주인 칸**. 병합이 없으면 빈 묶음.

    `A1` 과 `A2` 를 합치면 격자에는 칸 하나만 그려지지만, 수식에서는 둘 다
    **그 값**을 가리켜야 한다. 안 그러면 `=A1` 은 값이 나오고 `=A2` 는 빈칸이
    나와서, 보이는 것과 가리키는 것이 어긋난다.

    그리는 쪽(`dashboards.render_sheet`)과 계산하는 쪽이 **같은 이것**을 본다.
    두 벌로 두면 «그려지는 칸» 과 «값이 있는 칸» 이 조용히 갈라진다.
    """
    나온것: dict[str, str] = {}
    for 주소글, 칸 in (칸들 or {}).items():
        가로, 세로 = int((칸 or {}).get("가로병합", 1) or 1), \
                   int((칸 or {}).get("세로병합", 1) or 1)
        if 가로 <= 1 and 세로 <= 1:
            continue
        try:
            r, c = 자리(주소글)
        except ValueError:
            continue
        for rr in range(r, min(r + 세로, 행수)):
            for cc in range(c, min(c + 가로, 열수)):
                if (rr, cc) != (r, c):
                    나온것[주소(rr, cc)] = 주소글
    return 나온것


def 칸읽기(칸값: dict, 칸들: dict, 행수: int = MAX_ROWS, 열수: int = MAX_COLS):
    """계산을 마친 시트 값으로 **칸 주소 → 값** 을 읽는 함수 (`값찾기` 자리에 넘긴다).

    병합에 덮인 자리는 주인 칸 값이다 — `값들` 이 계산할 때와 같아야 한다.
    안 그러면 숫자는 `=COUNTIFS(과제,LEFT(C4,…))` 로 맞게 나오는데, 누르면 C4 를
    빈칸으로 읽어 명단이 비었다 (C4 가 병합 칸일 때).
    """
    덮인 = 덮인칸(칸들, 행수, 열수)
    return lambda 주소글: 칸값.get(덮인.get(주소글, 주소글), "")


def 값들(칸들: dict, rows, 아는열=None, *, 행수: int = MAX_ROWS,
       열수: int = MAX_COLS) -> tuple[dict[str, str], list[str]]:
    """모든 칸을 계산한다. ({주소: 보일 값}, 오류 목록)

    칸을 미리 줄 세우지 않는다. **필요할 때 계산하고 기억한다**(memo). 계산 중인
    칸을 따로 모아 두어, 그 칸을 다시 만나면 순환 참조다 — 안 그러면 무한히 돈다.

    틀린 칸은 `?` 를 두고 무엇이 틀렸는지 따로 모아 돌려준다. 조용히 0 을
    띄우면 아무도 못 알아챈다.

    **병합된 칸은 덮인 자리도 같은 값이다** — `A1`·`A2` 를 합쳐 «abc» 를 적으면
    `=A1` 도 `=A2` 도 abc 다.
    """
    덮인 = 덮인칸(칸들, 행수, 열수)
    결과: dict[str, str] = {}
    오류: list[str] = []
    도는중: list[str] = []
    #: 계산하다 터진 칸 {주소: 까닭}. 이걸 가리킨 칸은 **그 사실을 그대로**
    #: 말한다. 안 그러면 A1 이 순환 참조인데 B1 은 "숫자가 아닙니다 '?'" 라고
    #: 해서, 진짜 까닭이 어디 있는지 알 수가 없다.
    실패: dict[str, str] = {}

    def 한칸(주소글: str) -> str:
        # 병합에 덮인 자리는 **주인 칸을 가리킨다.** 여기서 갈아 끼우면
        # 순환 참조 검사도 저절로 따라온다 (주인 칸을 계산하는 중이면 걸린다).
        주소글 = 덮인.get(주소글, 주소글)
        if 주소글 in 실패:
            raise SheetError(f"{주소글} 칸을 계산하지 못했습니다")
        if 주소글 in 결과:
            return 결과[주소글]
        if 주소글 in 도는중:
            고리 = " → ".join(도는중[도는중.index(주소글):] + [주소글])
            raise SheetError(f"순환 참조: {고리}")
        원글 = str((칸들.get(주소글) or {}).get("글") or "")
        if not E.is_formula(원글):
            결과[주소글] = 원글
            return 원글
        도는중.append(주소글)
        try:
            # 칸 하나도 다른 자리와 **같은 계산기**를 쓴다. 그래야 시트에 적은
            # `=PCT(지원자, …)` 가 숫자 블록에 적은 것과 똑같이 보인다.
            값, _ = 계산(원글, rows, 아는열, 값찾기=한칸, 현재칸=주소글)
        except (SheetError, E.ExprError, F.FormulaError, ValueError) as exc:
            실패[주소글] = str(exc)
            raise
        finally:
            도는중.pop()
        결과[주소글] = 값
        return 값

    for 주소글 in 칸들:
        try:
            한칸(주소글)
        except (SheetError, E.ExprError, F.FormulaError, ValueError) as exc:
            결과[주소글] = "?"
            메시지 = f"{주소글}: {실패.get(주소글) or exc}"
            if 메시지 not in 오류:
                오류.append(메시지)
    return 결과, 오류
