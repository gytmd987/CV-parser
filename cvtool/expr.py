"""한 사람의 값으로 글을 만드는 수식 — **엑셀 문법 그대로.**

    =박사_학교 & " " & 박사_전공 & " (" & TEXT(박사_시작,"'yy.m") & ")"
      →  서울대학교 기계공학 ('22.2)

예전에는 이 자리에 자리표시자 틀이 있었다.

    {박사_학교} {박사_전공}({기간:박사_시작~박사_졸업})

틀은 배우기 쉬웠지만 **할 수 있는 일이 정해져 있었다.** `yyyymm` 을 `'yy.mm`
으로 바꾸거나 `08` 을 `8` 로 보이게 하려면 그때마다 새 조각(`{날짜2:…}`)을
만들어 붙여야 했다. 쓰는 사람이 스스로 넓힐 수가 없었다.

**그래서 문법을 새로 만들지 않고 엑셀 것을 가져왔다.** `TEXT` 의 서식 코드는
이미 아는 규칙이고(`m` 은 한 자리, `mm` 은 두 자리), 빈 값을 건너뛰는 일은
`TEXTJOIN` 의 두 번째 인자가 원래 하던 일이다. 새로 외울 게 없다.

## 문맥이 둘

엑셀도 데이터 줄의 수식과 요약 칸의 수식이 다르게 동작한다. 여기도 같다.

  - **행 문맥** (이 파일)   — 열 이름은 *그 사람의 값*. 계산 열·프로필 문장.
  - **집계 문맥** (formula.py) — 열 이름은 *대상 전체에 대한 집계*. 대시보드 숫자.

## 안 하는 것

SQL 도, 임의의 파이썬도 아니다. 함수 목록이 정해져 있고, 값은 화면들이 이미
불러온 행 하나에서만 온다. 지원자 DB 에는 이름·연락처·생년월일이 있어서,
칸에 아무 질의나 적을 수 있으면 대시보드 하나가 개인정보 유출 통로가 된다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .timeutil import now_kst
from .wildcard import has as _와일드카드있나
from .wildcard import like as _패턴맞나


class ExprError(ValueError):
    """수식이 잘못됐다. **사람이 읽고 고칠 수 있는 말**이어야 한다."""


# ---------------------------------------------------------------------------
# 값
# ---------------------------------------------------------------------------
# DB 에서 오는 값은 전부 글자다. 계산이 필요할 때만 숫자로 본다.
# 파이썬 bool 은 int 라서 여기서는 쓰지 않는다 — TRUE/FALSE 도 글자로 다룬다.
TRUE, FALSE = "TRUE", "FALSE"


def _글(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        # 12.0 을 "12" 로. 엑셀도 정수는 소수점을 안 붙인다.
        return str(int(v)) if v == int(v) else repr(round(v, 10))
    return str(v)


def _수(v, 어디: str = "") -> float:
    """숫자로 본다. 못 보면 말해 준다 (조용히 0 으로 두면 틀린 값이 조용히 퍼진다)."""
    if isinstance(v, (int, float)):
        return float(v)
    글 = str(v or "").strip().replace(",", "")
    if not 글:
        return 0.0
    try:
        return float(글)
    except ValueError:
        raise ExprError(
            f"숫자가 아닙니다: {v!r}" + (f" ({어디})" if 어디 else "")
        ) from None


def _참인가(v) -> bool:
    글 = _글(v).strip().upper()
    if 글 in ("", FALSE, "0", "아니오", "N"):
        return False
    return True


# ---------------------------------------------------------------------------
# 날짜 — DB 에는 yyyymm / yyyymmdd 로 들어 있다
# ---------------------------------------------------------------------------
_ONGOING = ("재직중", "재직 중", "현재", "present", "current", "now")

#: 서식 코드. **긴 것부터** 본다 (yyyy 를 yy 둘로 읽으면 안 된다).
_FMT = ("yyyy", "yy", "mm", "m", "dd", "d")


def _날짜조각(값) -> tuple[str, str, str]:
    """`202602` → ('2026','02',''). 숫자만 남겨서 길이로 판단한다."""
    숫자 = re.sub(r"\D", "", _글(값))
    if len(숫자) >= 8:
        return 숫자[:4], 숫자[4:6], 숫자[6:8]
    if len(숫자) >= 6:
        return 숫자[:4], 숫자[4:6], ""
    if len(숫자) == 4:
        return 숫자, "", ""
    return "", "", ""


def text_format(값, 서식: str) -> str:
    """엑셀 TEXT 와 같다. `TEXT(202602,"'yy.m")` → `'26.2`

    **서식 코드 밖의 글자는 그대로 나간다.** `'` 도, `년` 도, `.` 도.
    읽을 수 없는 값이면 원문을 그대로 돌려준다 — 지어내지 않는다.
    """
    원문 = _글(값).strip()
    if 원문.lower() in _ONGOING:
        return 원문
    연, 월, 일 = _날짜조각(원문)
    if not 연:
        return 원문
    나온글, i = [], 0
    서식 = 서식 or ""
    while i < len(서식):
        for 코드 in _FMT:
            if 서식.startswith(코드, i):
                if 코드 == "yyyy":
                    나온글.append(연)
                elif 코드 == "yy":
                    나온글.append(연[2:])
                elif 코드 == "mm":
                    나온글.append(월 or "")
                elif 코드 == "m":
                    나온글.append(str(int(월)) if _두자리(월) else "")
                elif 코드 == "dd":
                    나온글.append(일 or "")
                elif 코드 == "d":
                    나온글.append(str(int(일)) if _두자리(일) else "")
                i += len(코드)
                break
        else:
            나온글.append(서식[i])
            i += 1
    return "".join(나온글)


def _두자리(글: str) -> bool:
    """두 자리 조각이 실제로 쓸 수 있는 숫자인가 (`00` 은 '모른다' 는 뜻이다)."""
    return bool(글) and 글.isdigit() and int(글) > 0


# ---------------------------------------------------------------------------
# 토큰
# ---------------------------------------------------------------------------
@dataclass
class Tok:
    종류: str          # 수 글 이름 연산 괄호열림 괄호닫힘 쉼표 끝
    값: str
    자리: int


_연산자 = ("<>", "<=", ">=", "!=", "&", "=", "<", ">", "+", "-", "*", "/")
#: 열 이름에 쓸 수 있는 글자. 한글·영문·숫자·밑줄. 대괄호로 감싸면 무엇이든.
_이름_RE = re.compile(r"[^\W\d][\w]*", re.UNICODE)


def tokenize(글: str) -> list[Tok]:
    s = 글 or ""
    i, out = 0, []
    while i < len(s):
        ch = s[i]
        if ch.isspace():
            i += 1
            continue
        if ch == '"':
            j = i + 1
            조각 = []
            while j < len(s):
                if s[j] == '"':
                    if j + 1 < len(s) and s[j + 1] == '"':   # "" 는 따옴표 한 개
                        조각.append('"')
                        j += 2
                        continue
                    break
                조각.append(s[j])
                j += 1
            if j >= len(s):
                raise ExprError(f'따옴표가 닫히지 않았습니다 ({i + 1}번째 글자부터)')
            out.append(Tok("글", "".join(조각), i))
            i = j + 1
            continue
        if ch == "[":                                   # [열 이름] — 띄어쓰기 허용
            j = s.find("]", i)
            if j < 0:
                raise ExprError(f"대괄호가 닫히지 않았습니다 ({i + 1}번째 글자부터)")
            out.append(Tok("이름", s[i + 1:j].strip(), i))
            i = j + 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < len(s) and s[i + 1].isdigit()):
            j = i
            while j < len(s) and (s[j].isdigit() or s[j] == "."):
                j += 1
            out.append(Tok("수", s[i:j], i))
            i = j
            continue
        if ch == "(":
            out.append(Tok("괄호열림", ch, i)); i += 1; continue
        if ch == ")":
            out.append(Tok("괄호닫힘", ch, i)); i += 1; continue
        if ch == ",":
            out.append(Tok("쉼표", ch, i)); i += 1; continue
        for op in _연산자:
            if s.startswith(op, i):
                out.append(Tok("연산", op, i)); i += len(op); break
        else:
            m = _이름_RE.match(s, i)
            if not m:
                raise ExprError(f"알 수 없는 글자입니다: {ch!r} ({i + 1}번째)")
            out.append(Tok("이름", m.group(0), i))
            i = m.end()
    out.append(Tok("끝", "", len(s)))
    return out


# ---------------------------------------------------------------------------
# 나무
# ---------------------------------------------------------------------------
@dataclass
class Num:
    값: float


@dataclass
class Str:
    값: str


@dataclass
class Col:
    이름: str


@dataclass
class Call:
    이름: str
    인자: list


@dataclass
class Bin:
    연산: str
    왼: object
    오: object


@dataclass
class Neg:
    안: object


# ---------------------------------------------------------------------------
# 파서 — 우선순위는 엑셀과 같다
# ---------------------------------------------------------------------------
#   & (잇기)  <  비교  <  + -  <  * /  <  단항 -
class _Parser:
    def __init__(self, toks: list[Tok]) -> None:
        self.t = toks
        self.i = 0

    def 지금(self) -> Tok:
        return self.t[self.i]

    def 먹기(self, 종류: str, 값: str = "") -> Tok:
        tk = self.지금()
        if tk.종류 != 종류 or (값 and tk.값 != 값):
            뭐 = 값 or 종류
            raise ExprError(f"{뭐} 가 있어야 할 자리에 {tk.값 or '끝'} 이 있습니다")
        self.i += 1
        return tk

    def parse(self):
        나무 = self.concat()
        if self.지금().종류 != "끝":
            raise ExprError(
                f"수식이 {self.지금().값!r} 에서 끝나지 않았습니다 "
                "(괄호나 연산자를 빠뜨리지 않았나요?)"
            )
        return 나무

    def concat(self):
        왼 = self.compare()
        while self.지금().종류 == "연산" and self.지금().값 == "&":
            self.i += 1
            왼 = Bin("&", 왼, self.compare())
        return 왼

    def compare(self):
        왼 = self.add()
        while self.지금().종류 == "연산" and self.지금().값 in ("=", "<>", "!=", "<", ">", "<=", ">="):
            op = self.지금().값
            self.i += 1
            왼 = Bin("<>" if op == "!=" else op, 왼, self.add())
        return 왼

    def add(self):
        왼 = self.mul()
        while self.지금().종류 == "연산" and self.지금().값 in ("+", "-"):
            op = self.지금().값
            self.i += 1
            왼 = Bin(op, 왼, self.mul())
        return 왼

    def mul(self):
        왼 = self.unary()
        while self.지금().종류 == "연산" and self.지금().값 in ("*", "/"):
            op = self.지금().값
            self.i += 1
            왼 = Bin(op, 왼, self.unary())
        return 왼

    def unary(self):
        if self.지금().종류 == "연산" and self.지금().값 == "-":
            self.i += 1
            return Neg(self.unary())
        return self.primary()

    def primary(self):
        tk = self.지금()
        if tk.종류 == "수":
            self.i += 1
            return Num(float(tk.값))
        if tk.종류 == "글":
            self.i += 1
            return Str(tk.값)
        if tk.종류 == "괄호열림":
            self.i += 1
            안 = self.concat()
            self.먹기("괄호닫힘")
            return 안
        if tk.종류 == "이름":
            self.i += 1
            if self.지금().종류 == "괄호열림":
                self.i += 1
                인자 = []
                if self.지금().종류 != "괄호닫힘":
                    인자.append(self.concat())
                    while self.지금().종류 == "쉼표":
                        self.i += 1
                        인자.append(self.concat())
                self.먹기("괄호닫힘")
                return Call(tk.값.upper(), 인자)
            return Col(tk.값)
        raise ExprError(
            f"값이 있어야 할 자리에 {tk.값 or '끝'} 이 있습니다"
            if tk.종류 != "끝" else "수식이 도중에 끝났습니다"
        )


# ---------------------------------------------------------------------------
# 함수 — 이름은 **엑셀 그대로**. 새로 외울 게 없어야 한다.
# ---------------------------------------------------------------------------
def _f_if(a: list):
    if len(a) < 2:
        raise ExprError('IF 는 IF(조건, 참일 때, 거짓일 때) 입니다')
    return a[1] if _참인가(a[0]) else (a[2] if len(a) > 2 else "")


def _f_ifs(a: list):
    if len(a) < 2:
        raise ExprError("IFS 는 조건과 값을 짝으로 받습니다")
    for i in range(0, len(a) - 1, 2):
        if _참인가(a[i]):
            return a[i + 1]
    return ""


def _f_textjoin(a: list):
    """TEXTJOIN(구분자, 빈값건너뛰기, 값...) — 빈 조각이 사라지는 규칙이 여기 있다."""
    if len(a) < 2:
        raise ExprError('TEXTJOIN 은 TEXTJOIN(구분자, TRUE, 값...) 입니다')
    구분, 건너뛰기 = _글(a[0]), _참인가(a[1])
    조각 = [_글(x) for x in a[2:]]
    if 건너뛰기:
        조각 = [x for x in 조각 if x.strip()]
    return 구분.join(조각)


def _f_mid(a: list):
    글, 시작, 길이 = _글(a[0]), int(_수(a[1], "MID 시작")), int(_수(a[2], "MID 길이"))
    if 시작 < 1:
        raise ExprError("MID 의 시작은 1부터입니다 (엑셀과 같습니다)")
    return 글[시작 - 1:시작 - 1 + 길이]


def _f_datedif(a: list):
    """DATEDIF(시작, 끝, 단위) — 단위 Y/M. 나이 계산에 쓴다."""
    연1, 월1, _ = _날짜조각(a[0])
    연2, 월2, _ = _날짜조각(a[1])
    if not 연1 or not 연2:
        return ""
    단위 = _글(a[2]).upper() if len(a) > 2 else "Y"
    개월 = (int(연2) - int(연1)) * 12 + (int(월2 or 1) - int(월1 or 1))
    return float(개월) if 단위 == "M" else float(개월 // 12)


def _f_round(a: list):
    자리 = int(_수(a[1], "ROUND 자릿수")) if len(a) > 1 else 0
    return round(_수(a[0], "ROUND"), 자리)


def _숫자만(a: list, 어디: str) -> list[float]:
    """인자 중 **숫자로 읽히는 것만**. 빈 칸·글자는 조용히 건너뛴다.

    시트에서 `A1:A10` 을 펼쳐 넘기면 아직 안 적은 칸이 섞여 온다. 그걸 0 으로
    치면 평균이 내려앉고, 터뜨리면 범위를 못 쓴다 — 엑셀도 건너뛴다.
    """
    나온것 = []
    for x in a:
        글 = _글(x).strip()
        if not 글:
            continue
        try:
            나온것.append(float(글))
        except ValueError:
            continue
    if not 나온것:
        raise ExprError(f"{어디} 에 셀 숫자가 없습니다")
    return 나온것


def _f_average(a: list):
    수 = _숫자만(a, "AVERAGE")
    return sum(수) / len(수)


def _f_median(a: list):
    수 = sorted(_숫자만(a, "MEDIAN"))
    가운데 = len(수) // 2
    return 수[가운데] if len(수) % 2 else (수[가운데 - 1] + 수[가운데]) / 2


def _f_count(a: list):
    """숫자로 읽히는 칸의 개수. 집계 COUNT 와 이름이 같지만 **첫 인자로 갈린다**
    (`sheet.집계먼저` 가 대상일 때만 집계로 본다)."""
    return float(sum(1 for x in a if _글(x).strip()
                     and _글(x).strip().replace(".", "", 1).lstrip("-").isdigit()))


def _f_counta(a: list):
    """빈 칸이 아닌 것의 개수. 글자도 센다."""
    return float(sum(1 for x in a if _글(x).strip()))


def _f_올림(a: list, 위로: bool):
    import math

    자리 = int(_수(a[1], "자릿수")) if len(a) > 1 else 0
    배 = 10 ** 자리
    n = _수(a[0], "ROUNDUP" if 위로 else "ROUNDDOWN") * 배
    return (math.ceil(n) if 위로 else math.floor(n)) / 배


def _f_mod(a: list):
    b = _수(a[1], "MOD")
    if b == 0:
        raise ExprError("MOD 의 나누는 수가 0 입니다")
    return _수(a[0], "MOD") % b


def _f_sqrt(a: list):
    n = _수(a[0], "SQRT")
    if n < 0:
        raise ExprError("SQRT 에 음수를 넣을 수 없습니다")
    return n ** 0.5


def _f_find(a: list, 대소문자: bool):
    """FIND 는 대소문자를 가리고 SEARCH 는 안 가린다 (엑셀과 같다).

    못 찾으면 **0** 이다. 엑셀은 오류를 내지만, 여기서는 오류가 칸 하나를
    통째로 `?` 로 만들어 버려서 `IF(FIND(...)>0, ...)` 을 쓸 수가 없다.
    """
    찾을것, 안에서 = _글(a[0]), _글(a[1])
    시작 = int(_수(a[2], "FIND 시작")) - 1 if len(a) > 2 else 0
    if not 대소문자:
        찾을것, 안에서 = 찾을것.lower(), 안에서.lower()
    자리 = 안에서.find(찾을것, max(0, 시작))
    return float(자리 + 1)


def _f_replace(a: list):
    """REPLACE(글, 시작, 글자수, 새글) — 자리로 바꾼다 (SUBSTITUTE 는 글자로)."""
    글 = _글(a[0])
    시작 = int(_수(a[1], "REPLACE 시작"))
    개수 = int(_수(a[2], "REPLACE 글자수"))
    if 시작 < 1:
        raise ExprError("REPLACE 의 시작은 1부터입니다 (엑셀과 같습니다)")
    return 글[:시작 - 1] + _글(a[3]) + 글[시작 - 1 + max(0, 개수):]


def _f_switch(a: list):
    """SWITCH(값, 이것이면, 이것을, ..., 아니면) — IF 중첩을 줄인다."""
    if len(a) < 3:
        raise ExprError("SWITCH 는 SWITCH(값, 이것이면, 이것을, ..., 아니면) 입니다")
    기준 = _글(a[0])
    남은 = a[1:]
    for i in range(0, len(남은) - 1, 2):
        if _글(남은[i]) == 기준:
            return 남은[i + 1]
    return 남은[-1] if len(남은) % 2 else ""


def _숫자인가(v) -> bool:
    글 = _글(v).strip()
    if not 글:
        return False
    try:
        float(글)
        return True
    except ValueError:
        return False


def _오늘() -> str:
    return now_kst().strftime("%Y%m%d")


#: (최소 인자 수, 계산). 인자는 이미 다 계산된 값으로 들어온다.
FUNCS: dict[str, tuple[int, Callable[[list], object]]] = {
    # -- 글자
    "TEXT": (2, lambda a: text_format(a[0], _글(a[1]))),
    "CONCAT": (1, lambda a: "".join(_글(x) for x in a)),
    "TEXTJOIN": (2, _f_textjoin),
    "LEFT": (1, lambda a: _글(a[0])[:int(_수(a[1], "LEFT")) if len(a) > 1 else 1]),
    "RIGHT": (1, lambda a: _글(a[0])[-(int(_수(a[1], "RIGHT")) if len(a) > 1 else 1):]
              if _글(a[0]) else ""),
    "MID": (3, _f_mid),
    "LEN": (1, lambda a: float(len(_글(a[0])))),
    "TRIM": (1, lambda a: " ".join(_글(a[0]).split())),
    "UPPER": (1, lambda a: _글(a[0]).upper()),
    "LOWER": (1, lambda a: _글(a[0]).lower()),
    "SUBSTITUTE": (3, lambda a: _글(a[0]).replace(_글(a[1]), _글(a[2]))),
    "REPLACE": (4, _f_replace),
    "FIND": (2, lambda a: _f_find(a, True)),
    "SEARCH": (2, lambda a: _f_find(a, False)),
    "REPT": (2, lambda a: _글(a[0]) * int(_수(a[1], "REPT"))),
    # 줄바꿈은 엑셀과 같이 CHAR(10) 이다. 입력칸이 한 줄짜리라 엔터를 칠 수
    # 없으므로, 글자를 번호로 넣는 이 방법이 유일한 길이기도 하다.
    "CHAR": (1, lambda a: chr(int(_수(a[0], "CHAR")))),
    "CODE": (1, lambda a: float(ord(_글(a[0])[0])) if _글(a[0]) else ""),
    # -- 판단
    "IF": (2, _f_if),
    "IFS": (2, _f_ifs),
    "AND": (1, lambda a: TRUE if all(_참인가(x) for x in a) else FALSE),
    "OR": (1, lambda a: TRUE if any(_참인가(x) for x in a) else FALSE),
    "NOT": (1, lambda a: FALSE if _참인가(a[0]) else TRUE),
    "ISBLANK": (1, lambda a: TRUE if not _글(a[0]).strip() else FALSE),
    "ISNUMBER": (1, lambda a: TRUE if _숫자인가(a[0]) else FALSE),
    "ISTEXT": (1, lambda a: TRUE if (_글(a[0]).strip()
                                     and not _숫자인가(a[0])) else FALSE),
    "EXACT": (2, lambda a: TRUE if _글(a[0]) == _글(a[1]) else FALSE),
    "SWITCH": (3, _f_switch),
    # -- 숫자
    "VALUE": (1, lambda a: _수(a[0], "VALUE")),
    "ROUND": (1, _f_round),
    "ROUNDUP": (1, lambda a: _f_올림(a, True)),
    "ROUNDDOWN": (1, lambda a: _f_올림(a, False)),
    "MOD": (2, _f_mod),
    "POWER": (2, lambda a: _수(a[0], "POWER") ** _수(a[1], "POWER")),
    "SQRT": (1, _f_sqrt),
    # -- 여러 값 셈. 시트에서 `A1:A10` 을 펼쳐 넘기면 그대로 먹는다.
    # 이름이 집계와 겹치는 것들은 **첫 인자가 대상이냐**로 갈린다
    # (`sheet.집계먼저`) — COUNT(A1:A5) 는 칸을, COUNT(지원자) 는 사람을 센다.
    "AVERAGE": (1, _f_average),
    "MEDIAN": (1, _f_median),
    "COUNT": (1, _f_count),
    "COUNTA": (1, _f_counta),
    "INT": (1, lambda a: float(int(_수(a[0], "INT")))),
    "ABS": (1, lambda a: abs(_수(a[0], "ABS"))),
    # 빈 칸을 건너뛴다. 0 으로 치면 `MIN(A1:A10)` 이 아직 안 적은 칸 때문에
    # 늘 0 이 된다 — 엑셀도 건너뛴다. (SUM 은 0 을 더해도 같아서 그대로 둔다.)
    "MIN": (1, lambda a: min(_숫자만(a, "MIN"))),
    "MAX": (1, lambda a: max(_숫자만(a, "MAX"))),
    "SUM": (1, lambda a: sum(_수(x, "SUM") for x in a)),
    # -- 날짜 (DB 는 yyyymm / yyyymmdd 로 들고 있다)
    "TODAY": (0, lambda a: _오늘()),
    "YEAR": (1, lambda a: float(_날짜조각(a[0])[0]) if _날짜조각(a[0])[0] else ""),
    "MONTH": (1, lambda a: float(_날짜조각(a[0])[1]) if _두자리(_날짜조각(a[0])[1]) else ""),
    "DAY": (1, lambda a: float(_날짜조각(a[0])[2]) if _두자리(_날짜조각(a[0])[2]) else ""),
    "DATEDIF": (2, _f_datedif),
}

#: 값이 없어도 부를 수 있는 것 (IFERROR 는 인자를 먼저 계산하면 안 되므로 특별하다)
#: `FUNCS` 표에 못 들어가는 함수들. 그 표의 람다는 **계산이 끝난 인자**만
#: 받는데, 이 둘은 그러면 안 된다.
#:   IFERROR — 첫 인자가 터지는 게 요점이라 미리 계산하면 안 된다.
#:   ROW     — 인자가 아니라 **문맥**(몇 번째 줄인가)을 읽는다.
_LAZY = ("IFERROR", "ROW")

#: 목록·프로필이 줄마다 넣어 주는 자리. 제어문자라 진짜 열 이름과 못 겹친다.
#: 값 묶음에 이게 없으면 `ROW()` 를 쓸 수 없는 자리라는 뜻이다.
줄번호_키 = "\x00줄번호"

#: 쓸 수 있는 함수 이름 전부. 화면의 자동완성이 이걸 그대로 보여준다 —
#: 목록을 두 군데에 적어 두면 하나를 늘렸을 때 다른 하나가 조용히 뒤처진다.
FUNC_NAMES: tuple[str, ...] = tuple(sorted(set(FUNCS) | set(_LAZY)))


# ---------------------------------------------------------------------------
# 계산
# ---------------------------------------------------------------------------
def _비교(op: str, 왼, 오) -> str:
    """숫자끼리면 숫자로, 아니면 글자로 견준다 (엑셀도 그렇게 한다).

    한쪽에 `*` 나 `?` 가 있으면 **와일드카드로 견준다.** 엑셀의 COUNTIF 와
    같은 규칙이다 — `=부서="*"` 는 부서를 안 가리고 전부, `="*합격"` 은
    '합격' 으로 끝나는 것. 글자 그대로의 별표를 찾을 때는 `~*` 로 적는다.
    """
    if op in ("=", "<>"):
        글왼, 글오 = _글(왼), _글(오)
        맞나 = None
        if _와일드카드있나(글오):            # 보통 오른쪽이 패턴이다
            맞나 = _패턴맞나(글왼, 글오)
        elif _와일드카드있나(글왼):
            맞나 = _패턴맞나(글오, 글왼)
        if 맞나 is not None:
            return TRUE if (맞나 if op == "=" else not 맞나) else FALSE
    try:
        a, b = _수(왼), _수(오)
    except ExprError:
        a, b = _글(왼), _글(오)
    결과 = {
        "=": a == b, "<>": a != b,
        "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b,
    }[op]
    return TRUE if 결과 else FALSE


def _계산(나무, 값들: dict) -> object:
    if isinstance(나무, Num):
        return 나무.값
    if isinstance(나무, Str):
        return 나무.값
    if isinstance(나무, Col):
        이름 = 나무.이름
        if 이름.upper() in ("TRUE", "FALSE"):
            return 이름.upper()
        if 이름 not in 값들:
            raise ExprError(f"모르는 열입니다: {이름}")
        return 값들.get(이름, "")
    if isinstance(나무, Neg):
        return -_수(_계산(나무.안, 값들), "-")
    if isinstance(나무, Bin):
        op = 나무.연산
        if op == "&":
            return _글(_계산(나무.왼, 값들)) + _글(_계산(나무.오, 값들))
        왼, 오 = _계산(나무.왼, 값들), _계산(나무.오, 값들)
        if op in ("=", "<>", "<", ">", "<=", ">="):
            return _비교(op, 왼, 오)
        a, b = _수(왼, op), _수(오, op)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if b == 0:
            raise ExprError("0 으로 나눌 수 없습니다")
        return a / b
    if isinstance(나무, Call):
        이름 = 나무.이름
        if 이름 == "ROW":
            # 몇 번째 줄인가. 목록 표가 **정렬·자르기를 끝낸 뒤** 넣어 주므로
            # 화면에 보이는 순서와 늘 같다. 행 고르기·정렬은 번호가 정해지기
            # 전에 도는 수식이라 여기서 걸린다.
            if 줄번호_키 not in 값들:
                raise ExprError(
                    "ROW() 는 목록 표의 열에서만 쓸 수 있습니다 "
                    "— 줄이 정해진 뒤에야 번호가 생깁니다."
                )
            return float(값들[줄번호_키])
        if 이름 == "IFERROR":
            # 인자를 미리 계산하면 안 된다. 첫 인자가 터지는 게 요점이다.
            if len(나무.인자) < 2:
                raise ExprError("IFERROR 는 IFERROR(수식, 틀렸을 때) 입니다")
            try:
                return _계산(나무.인자[0], 값들)
            except ExprError:
                return _계산(나무.인자[1], 값들)
        if 이름 not in FUNCS:
            raise ExprError(
                f"모르는 함수입니다: {이름} "
                f"(쓸 수 있는 것: {', '.join(sorted(set(FUNCS) | set(_LAZY)))})"
            )
        최소, 계산 = FUNCS[이름]
        인자 = [_계산(x, 값들) for x in 나무.인자]
        if len(인자) < 최소:
            raise ExprError(f"{이름} 에 인자가 모자랍니다 ({최소}개 이상)")
        return 계산(인자)
    raise ExprError("계산할 수 없는 수식입니다")


# ---------------------------------------------------------------------------
# 밖에서 쓰는 것
# ---------------------------------------------------------------------------
def is_formula(글: str) -> bool:
    """`=` 로 시작하면 수식이다. 엑셀과 같은 약속."""
    return (글 or "").lstrip().startswith("=")


def parse(수식: str):
    """수식 한 줄 → 나무. 틀리면 어디가 틀렸는지 말해 준다."""
    글 = (수식 or "").lstrip()
    if not 글.startswith("="):
        raise ExprError("수식은 = 로 시작합니다")
    return _Parser(tokenize(글[1:])).parse()


def columns(수식: str) -> list[str]:
    """이 수식이 읽는 열 이름들 (검사·미리보기용)."""
    본 = []

    def 훑기(n):
        if isinstance(n, Col):
            이름 = n.이름
            if 이름.upper() not in ("TRUE", "FALSE") and 이름 not in 본:
                본.append(이름)
        elif isinstance(n, Bin):
            훑기(n.왼); 훑기(n.오)
        elif isinstance(n, Neg):
            훑기(n.안)
        elif isinstance(n, Call):
            for x in n.인자:
                훑기(x)

    훑기(parse(수식))
    return 본


def validate(수식: str, 아는열) -> list[str]:
    """저장하기 전에 본다. 모르는 열이 있으면 **저장 자체를 막는다.**

    나중에 화면에서 조용히 빈칸으로 나오면, 값이 없는 건지 이름을 잘못 쓴
    건지 알 방법이 없다.
    """
    쓴것 = columns(수식)
    아는것 = set(아는열)
    모르는것 = [c for c in 쓴것 if c not in 아는것]
    if 모르는것:
        raise ExprError(
            "모르는 열입니다: " + ", ".join(모르는것)
            + " — 표 항목 탭에 있는 이름 그대로 적으세요."
        )
    return 쓴것


def evaluate(수식: str, 값들: dict) -> str:
    """수식 + 한 사람의 값 → 보일 글."""
    return _글(_계산(parse(수식), 값들))


def render(수식: str, 값들: dict) -> tuple[str, str]:
    """화면용. (보일 글, 오류) — 오류가 나도 화면 전체가 죽지 않는다."""
    try:
        return evaluate(수식, 값들), ""
    except ExprError as exc:
        return "", str(exc)
