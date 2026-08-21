"""인별 프로필 양식 — 한 사람을 정해진 문장 틀로 한 장에 담는다.

따로 있는 `박사_학교` `박사_전공` `박사_시작` `박사_졸업` 을 사람이 읽는
한 줄로 묶는 일을 한다.

    {박사_학교} {박사_전공}({기간:박사_시작~박사_졸업})
      →  서울대학교 기계공학('22.2~'26.2)

메일 템플릿의 자리표시자와 **같은 감각**이라 새로 배울 게 없다. 다른 점은
날짜를 다루는 조각과, **빈 값이 사라지는 규칙**이 있다는 것이다.

빈 값 규칙이 이 모듈의 핵심이다. `서울대 ('22.2~)` 처럼 반쪽만 남은 줄은
아무도 안 본다. 그래서 두 단계로 지운다.

  1. **조각째** — `{…}` 가 비면 거기 딸린 괄호·구분자까지 같이 빠진다.
  2. **줄째**   — 그 줄의 자리표시자가 전부 비면 줄 자체가 빠진다.
     석사를 안 한 사람 프로필에 빈 석사 줄이 남지 않는다.
"""

from __future__ import annotations

import re

#: `{열}` `{기간:시작~종료}` `{날짜:열}` `{수:열}`
_SLOT_RE = re.compile(r"\{([^{}]+)\}")

#: 값이 빈 조각 자리에 잠깐 세워 두는 표. 이 표 주변만 정리한다.
#: 전체 글에 대고 정규식을 돌리면, 원래 있던 구분자까지 지워 버린다.
_HOLE = "\x00"

#: 빈 조각 하나만 든 껍데기 (여는 짝이 있는 것만)
_EMPTY_WRAP_RE = re.compile(
    r"\(\s*" + _HOLE + r"\s*\)"
    r"|\[\s*" + _HOLE + r"\s*\]"
    r"|<\s*" + _HOLE + r"\s*>"
)

#: 빈 조각에 붙어 있던 구분자 (앞쪽 ", <빈칸>" 또는 뒤쪽 "<빈칸> ,")
_HOLE_SEP_RE = re.compile(
    r"\s*[,/·|]\s*" + _HOLE + r"|" + _HOLE + r"\s*[,/·|]\s*"
)

#: 다니는 중을 뜻하는 표시
_ONGOING = ("재직중", "재직 중", "현재", "present", "current", "now")


def _yy_m(ym: str) -> str:
    """`202602` → `'26.2`. 연도만 있으면 `'26`. 못 읽으면 원문 그대로."""
    글 = re.sub(r"\D", "", str(ym or ""))
    if len(글) >= 6:
        연, 월 = 글[:4], int(글[4:6] or 0)
        return f"'{연[2:]}.{월}" if 1 <= 월 <= 12 else f"'{연[2:]}"
    if len(글) == 4:
        return f"'{글[2:]}"
    return str(ym or "").strip()


def _period(시작: str, 종료: str) -> str:
    """`'22.2~'26.2`. 다니는 중이면 `'26.5~현재`. 둘 다 비면 빈 문자열."""
    s = _yy_m(시작)
    끝글 = str(종료 or "").strip()
    e = "현재" if 끝글.lower() in _ONGOING else _yy_m(끝글)
    if s and e:
        return f"{s}~{e}"
    if s:
        return f"{s}~"
    if e:
        return f"~{e}"
    return ""


def _slot_value(안: str, 값들: dict[str, str]) -> tuple[str, bool]:
    """자리표시자 하나를 (보일 글, 값이 있었나) 로.

    '값이 있었나' 를 따로 돌려주는 이유는 `{수:}` 때문이다. 값이 비면 `0` 을
    보여주지만, **그 0 이 줄을 살려 두면 안 된다.** 매칭을 안 돌린 사람 카드에
    `매칭 (0점)` 만 덩그러니 남았던 게 그 경우다.
    """
    안 = 안.strip()
    if ":" in 안:
        종류, 인자 = 안.split(":", 1)
        종류, 인자 = 종류.strip(), 인자.strip()
        if 종류 == "기간":
            시작, _, 종료 = 인자.partition("~")
            글 = _period(값들.get(시작.strip(), ""), 값들.get(종료.strip(), ""))
            return 글, bool(글)
        if 종류 == "날짜":
            글 = _yy_m(값들.get(인자, ""))
            return 글, bool(글)
        if 종류 == "수":
            원래 = str(값들.get(인자, "") or "").strip()
            return (원래 or "0"), bool(원래)
        # 모르는 종류는 그냥 열 이름으로 본다 (틀린 틀이 통째로 사라지지 않게)
        글 = str(값들.get(안, "") or "")
        return 글, bool(글.strip())
    글 = str(값들.get(안, "") or "")
    return 글, bool(글.strip())


def slots(틀: str) -> list[str]:
    """틀이 쓰는 자리표시자 원문들."""
    return [m.group(1).strip() for m in _SLOT_RE.finditer(틀 or "")]


def columns(틀: str) -> list[str]:
    """틀이 실제로 읽는 **열 이름**들 (검사용)."""
    이름: list[str] = []
    for 안 in slots(틀):
        if ":" in 안:
            종류, 인자 = (x.strip() for x in 안.split(":", 1))
            if 종류 == "기간":
                시작, _, 종료 = 인자.partition("~")
                이름 += [x.strip() for x in (시작, 종료) if x.strip()]
                continue
            if 종류 in ("날짜", "수"):
                이름.append(인자)
                continue
        이름.append(안)
    return [x for x in 이름 if x]


def render_line(틀: str, 값들: dict[str, str]) -> str:
    """한 줄을 채운다. 자리표시자가 전부 비면 **빈 문자열**을 돌려준다."""
    쓴것 = slots(틀)
    if not 쓴것:
        return 틀.strip()

    채움: list[bool] = []

    def 바꾸기(m: re.Match) -> str:
        값, 있었나 = _slot_value(m.group(1), 값들)
        채움.append(있었나)
        return 값 if 값.strip() else _HOLE

    글 = _SLOT_RE.sub(바꾸기, 틀)
    if not any(채움):
        return ""                       # 줄째 사라진다

    # 빈 조각 **자리만** 정리한다. 껍데기 → 붙어 있던 구분자 → 표 제거 순서.
    이전 = None
    while 이전 != 글:
        이전 = 글
        글 = _EMPTY_WRAP_RE.sub(_HOLE, 글)
    글 = _HOLE_SEP_RE.sub("", 글)
    글 = 글.replace(_HOLE, "")
    글 = re.sub(r"\s{2,}", " ", 글)
    return 글.strip(" \t,/·|-~")


def render(틀: str, 값들: dict[str, str]) -> str:
    """여러 줄짜리 양식. 빈 줄은 없어진다."""
    줄들 = [render_line(줄, 값들) for 줄 in (틀 or "").splitlines()]
    return "\n".join(x for x in 줄들 if x.strip())


def render_rows(줄틀: list[tuple[str, str]], 값들: dict[str, str]) -> list[tuple[str, str]]:
    """(라벨, 틀) 목록을 (라벨, 채운 값) 으로. 값이 빈 줄은 빠진다."""
    나온것 = []
    for 라벨, 틀 in 줄틀:
        값 = render(틀, 값들)
        if 값.strip():
            나온것.append((라벨, 값))
    return 나온것
