"""대시보드 칸에 넣는 작은 수식 언어.

**SQL 을 쓰지 않는다.** 지원자 DB 에는 이름·연락처·생년월일이 있어서, 임의
질의를 칸에 적을 수 있으면 대시보드 하나가 개인정보 유출 통로가 된다. 게다가
쓸 사람도 없고, LLM 이 만든 SQL 을 그대로 돌리면 틀려도 그럴듯한 숫자가 조용히
나온다. 그래서 **할 수 있는 일이 정해진** 언어를 따로 둔다.

    =COUNT(채용, 부서="차세대공정", 서류검토="합격")
    =PCT(채용, 최종상태~"*합격")
    =AVG(지원자, 저널_수)
    =LIST(채용, 부서="차세대공정", 열=한글_이름)

문법은 하나뿐이다.

    =함수(대상, 인자...)

- **함수**: COUNT PCT AVG SUM MIN MAX LIST
- **대상**: 지원자(인재 Pool 전체) / 채용(채용 시작한 사람)
- **조건**: `열="값"` `열~"패턴*"` `열!~"패턴*"` `열>숫자` `열!="값"` — AND 로만 잇는다
- **와일드카드**: 값에 `*`(아무 글자 몇 개든) `?`(한 글자)를 쓸 수 있다.
  `부서="*"` 는 부서를 안 가리고 전부. 별표 자체를 찾으려면 `~*`
- **열 이름**: 표 항목 탭에 있는 그 이름 그대로. 새로 외울 게 없다.

계산은 화면들이 이미 쓰는 행 목록을 그대로 받는다(`Rows`). 새 질의 계층을
만들지 않으므로 **화면 숫자와 대시보드 숫자가 어긋날 수 없다.**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import wildcard

#: 쓸 수 있는 함수. 여기 없는 이름은 저장 단계에서 막힌다.
FUNCTIONS = ("COUNT", "PCT", "AVG", "SUM", "MIN", "MAX", "LIST")

#: 셀 수 있는 대상
TARGETS = ("지원자", "채용")

#: 값을 세는 함수 (숫자 열 하나를 더 받는다)
_NUMERIC = ("AVG", "SUM", "MIN", "MAX")

_CALL_RE = re.compile(r"^\s*=\s*([A-Za-z]+)\s*\((.*)\)\s*$", re.DOTALL)
#: 조건 한 개: 열이름 연산자 값
_COND_RE = re.compile(r"""^\s*([^=!~<>]+?)\s*(>=|<=|!=|!~|~|=|>|<)\s*(.+?)\s*$""")


class FormulaError(ValueError):
    """수식이 잘못됐다. 사람이 읽고 고칠 수 있는 말이어야 한다."""


@dataclass
class Condition:
    열: str
    연산: str
    값: str

    def matches(self, 행: dict) -> bool:
        실제 = str(행.get(self.열, "") or "")
        기준 = self.값
        # `*` 나 `?` 가 있으면 엑셀 COUNTIF 처럼 와일드카드로 견준다.
        # `부서="*"` = 부서를 안 가리고 전부.
        if self.연산 in ("=", "!=") and wildcard.has(기준):
            맞나 = wildcard.like(실제, 기준)
            return 맞나 if self.연산 == "=" else not 맞나
        if self.연산 == "=":
            return 실제 == 기준
        if self.연산 == "!=":
            return 실제 != 기준
        if self.연산 == "~":
            return _like(실제, 기준)
        if self.연산 == "!~":
            return not _like(실제, 기준)
        # 크기 비교는 숫자로만 한다. 숫자가 아니면 그 줄은 조건에 안 맞는 것.
        왼, 오 = _number(실제), _number(기준)
        if 왼 is None or 오 is None:
            return False
        return {">": 왼 > 오, "<": 왼 < 오, ">=": 왼 >= 오, "<=": 왼 <= 오}[self.연산]


@dataclass
class Formula:
    함수: str
    대상: str
    조건: list[Condition] = field(default_factory=list)
    열: str = ""            # AVG/SUM/MIN/MAX 가 셀 열, LIST 가 뽑을 열

    def columns(self) -> list[str]:
        """이 수식이 건드리는 열 이름 전부 (검사용)."""
        이름 = [c.열 for c in self.조건]
        if self.열:
            이름.append(self.열)
        return 이름


def _like(값: str, 패턴: str) -> bool:
    """`*` `?` 를 쓰는 패턴. 정규식을 그대로 열어주지 않는다 (wildcard.py)."""
    return wildcard.like(값, 패턴)


def _number(값: str) -> float | None:
    글 = str(값 or "").strip().replace(",", "")
    if not 글:
        return None
    try:
        return float(글)
    except ValueError:
        return None


def _split_args(본문: str) -> list[str]:
    """따옴표 안의 쉼표는 건너뛰고 인자를 나눈다."""
    조각, 지금, 따옴표 = [], [], ""
    for ch in 본문:
        if 따옴표:
            지금.append(ch)
            if ch == 따옴표:
                따옴표 = ""
            continue
        if ch in "\"'":
            따옴표 = ch
            지금.append(ch)
            continue
        if ch == ",":
            조각.append("".join(지금))
            지금 = []
            continue
        지금.append(ch)
    조각.append("".join(지금))
    return [x.strip() for x in 조각 if x.strip()]


def _unquote(값: str) -> str:
    값 = 값.strip()
    if len(값) >= 2 and 값[0] == 값[-1] and 값[0] in "\"'":
        return 값[1:-1]
    return 값


def parse(수식: str) -> Formula:
    """수식 한 줄을 뜯는다. 틀리면 어디가 틀렸는지 말해 준다."""
    m = _CALL_RE.match(수식 or "")
    if not m:
        raise FormulaError(
            "수식은 =함수(대상, 조건...) 모양이어야 합니다. "
            '예: =COUNT(채용, 최종상태="최종 합격")'
        )
    함수 = m.group(1).upper()
    if 함수 not in FUNCTIONS:
        raise FormulaError(
            f"모르는 함수입니다: {m.group(1)} (쓸 수 있는 것: {', '.join(FUNCTIONS)})"
        )
    인자 = _split_args(m.group(2))
    if not 인자:
        raise FormulaError(f"{함수} 에 대상이 없습니다 (쓸 수 있는 것: {', '.join(TARGETS)})")

    대상 = _unquote(인자[0])
    if 대상 not in TARGETS:
        raise FormulaError(f"모르는 대상입니다: {대상} (쓸 수 있는 것: {', '.join(TARGETS)})")

    f = Formula(함수=함수, 대상=대상)
    맨열 = []            # 조건이 아닌 인자 = 그냥 열 이름 (=AVG(지원자, 저널_수))
    for 조각 in 인자[1:]:
        c = _COND_RE.match(조각)
        if not c:
            맨열.append(_unquote(조각))
            continue
        열, 연산, 값 = c.group(1).strip(), c.group(2), _unquote(c.group(3))
        if 열 == "열" and 연산 == "=":
            f.열 = 값
            continue
        f.조건.append(Condition(열=열, 연산=연산, 값=값))

    if 맨열:
        if 함수 not in _NUMERIC and 함수 != "LIST":
            raise FormulaError(
                f'조건 모양이 아닙니다: {맨열[0]} (예: 부서="차세대공정")'
            )
        f.열 = f.열 or 맨열[-1]
    if 함수 in _NUMERIC and not f.열:
        raise FormulaError(f"{함수} 는 셀 열이 필요합니다. 예: ={함수}(지원자, 저널_수)")
    if 함수 == "LIST" and not f.열:
        f.열 = "한글_이름"
    return f


def validate(수식: str, 아는열: set[str]) -> Formula:
    """저장 전에 본다. **없는 열 이름은 여기서 막는다.**

    LLM 이 가장 자주 하는 실수가 없는 열을 지어내는 것이다. 그대로 저장되면
    화면에는 그냥 0 이 뜨고, 아무도 틀린 줄 모른다.
    """
    f = parse(수식)
    모르는 = [c for c in f.columns() if c and c not in 아는열]
    if 모르는:
        raise FormulaError(
            "표에 없는 열입니다: " + ", ".join(sorted(set(모르는)))
            + " — 표 항목 탭에 있는 이름을 그대로 쓰세요."
        )
    return f


@dataclass
class Rows:
    """계산에 쓸 행 묶음. 화면들이 이미 만들어 둔 것을 그대로 받는다."""

    지원자: list[dict]
    채용: list[dict]

    def of(self, 대상: str) -> list[dict]:
        return self.채용 if 대상 == "채용" else self.지원자


def evaluate(f: Formula, rows: Rows) -> tuple[str, object]:
    """(보여줄 글, 원래 값). 원래 값은 형식 입히기·정렬에 쓴다."""
    대상행 = [r for r in rows.of(f.대상) if all(c.matches(r) for c in f.조건)]

    if f.함수 == "COUNT":
        return str(len(대상행)), len(대상행)

    if f.함수 == "PCT":
        전체 = rows.of(f.대상)
        if not 전체:
            return "-", None
        값 = len(대상행) * 100.0 / len(전체)
        return f"{값:.1f}%", 값

    if f.함수 == "LIST":
        이름들 = [str(r.get(f.열, "") or "") for r in 대상행]
        이름들 = [x for x in 이름들 if x]
        return (", ".join(이름들) if 이름들 else "-"), 이름들

    숫자 = [n for n in (_number(str(r.get(f.열, ""))) for r in 대상행) if n is not None]
    if not 숫자:
        return "-", None
    값 = {"AVG": sum(숫자) / len(숫자), "SUM": sum(숫자),
         "MIN": min(숫자), "MAX": max(숫자)}[f.함수]
    if f.함수 == "AVG":
        return f"{값:.1f}", 값
    return (f"{값:g}", 값)


def run(수식: str, rows: Rows, 아는열: set[str] | None = None) -> tuple[str, object]:
    """수식 하나를 끝까지. 틀리면 FormulaError."""
    f = validate(수식, 아는열) if 아는열 is not None else parse(수식)
    return evaluate(f, rows)


def is_formula(글: str) -> bool:
    """`=` 로 시작하면 수식, 아니면 그냥 글자."""
    return (글 or "").lstrip().startswith("=")
