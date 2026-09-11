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

#: 칸 주소 (`A1`, `AB12`). 대문자만 — 소문자는 열 이름과 헷갈린다.
_주소_RE = re.compile(r"\b([A-Z]{1,2})([1-9][0-9]{0,3})\b")
#: 범위 (`A1:B3`)
_범위_RE = re.compile(r"\b([A-Z]{1,2}[1-9][0-9]{0,3}):([A-Z]{1,2}[1-9][0-9]{0,3})\b")


class SheetError(ValueError):
    """시트 칸을 계산할 수 없다."""


def 주소(행: int, 열: int) -> str:
    """0-based 자리 -> `A1`. (0,0) 이 A1 이다."""
    return f"{col_letter(열)}{행 + 1}"


def 자리(주소글: str) -> tuple[int, int]:
    """`A1` -> (0, 0). 주소가 아니면 ValueError."""
    m = re.fullmatch(r"([A-Za-z]{1,2})([0-9]+)", (주소글 or "").strip())
    if not m:
        raise ValueError(f"칸 주소가 아닙니다: {주소글!r}")
    return int(m.group(2)) - 1, col_index(m.group(1).upper())


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


def 집계먼저(글: str, rows, 아는열=None) -> str:
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
        if not 인자 or F._unquote(인자[0]) not in F.TARGETS:
            # 집계가 아니다. expr 이 알아서 한다 (SUM(A1,A2) 같은 것).
            # **여는 괄호까지만** 내보내고 속은 다시 훑는다 — 그 안에 집계가
            # 들어 있을 수 있다 (`=ROUND(COUNT(지원자)/2, 1)`).
            나온것.append(글[i:m.end()])
            i = m.end()
            continue
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


def 계산(수식: str, rows, 아는열=None, 값찾기=None) -> tuple[str, object]:
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
    집계오류 = None
    try:
        F.parse(글)                          # 통째로 집계 호출 하나인가
    except F.FormulaError as exc:
        # 집계로 안 읽혔다. 그래도 **모양이 집계 호출이었으면** 까닭을 들고
        # 간다 — 섞인 길에서도 터지면 이쪽 말이 훨씬 친절하다.
        # `=COUNT(없는대상)` 을 "모르는 열입니다" 라고 하면 대상을 잘못 쓴 줄
        # 모른다. 반대로 `=COUNT(지원자)/0` 은 모양부터 집계가 아니므로
        # "=함수(대상, 조건...) 모양이어야 합니다" 가 엉뚱한 말이 된다.
        if F._CALL_RE.match(글):
            집계오류 = exc
    else:
        return F.run(글, rows, 아는열)
    try:
        식 = 집계먼저(범위펼치기(글), rows, 아는열)
        묶음 = ({a: 값찾기(a) for a in 참조들(식)} if 값찾기 is not None else {})
        값 = E.evaluate(식, 묶음)
    except (E.ExprError, SheetError, ValueError):
        if 집계오류 is not None:
            raise 집계오류
        raise
    return 값, 값


def 값들(칸들: dict, rows, 아는열=None) -> tuple[dict[str, str], list[str]]:
    """모든 칸을 계산한다. ({주소: 보일 값}, 오류 목록)

    칸을 미리 줄 세우지 않는다. **필요할 때 계산하고 기억한다**(memo). 계산 중인
    칸을 따로 모아 두어, 그 칸을 다시 만나면 순환 참조다 — 안 그러면 무한히 돈다.

    틀린 칸은 `?` 를 두고 무엇이 틀렸는지 따로 모아 돌려준다. 조용히 0 을
    띄우면 아무도 못 알아챈다.
    """
    결과: dict[str, str] = {}
    오류: list[str] = []
    도는중: list[str] = []
    #: 계산하다 터진 칸 {주소: 까닭}. 이걸 가리킨 칸은 **그 사실을 그대로**
    #: 말한다. 안 그러면 A1 이 순환 참조인데 B1 은 "숫자가 아닙니다 '?'" 라고
    #: 해서, 진짜 까닭이 어디 있는지 알 수가 없다.
    실패: dict[str, str] = {}

    def 한칸(주소글: str) -> str:
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
            값, _ = 계산(원글, rows, 아는열, 값찾기=한칸)
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
