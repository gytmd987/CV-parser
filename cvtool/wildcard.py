"""`*` 와 `?` — 엑셀 와일드카드.

엑셀에서 조건에 `*` 를 쓰면 "아무거나"라는 뜻이다. `COUNTIF(A:A,"*")` 가
전부를 세고, `="*합격"` 이 '합격' 으로 끝나는 걸 고른다. 이 프로그램의 조건도
같은 규칙을 쓴다. 두 문맥 모두에서 똑같이 동작해야 해서 여기 한 곳에 둔다.

  - **행 문맥** (`expr.py`)     `=부서="*"` `=IF(최종상태="*합격","O","")`
  - **집계 문맥** (`formula.py`) `=COUNT(채용, 부서="*")`

규칙은 넷뿐이다.

  ``*``   아무 글자 몇 개든 (0개도 된다)
  ``?``   아무 글자 딱 한 개
  ``~*`` ``~?`` ``~~``   그 글자 자체 (엑셀과 같은 탈출 문자)
  대소문자는 구분하지 않는다 (엑셀도 그렇다)

`*` 하나만 적으면 **빈 값까지 포함해 전부**가 걸린다. 엑셀은 빈 칸을 빼지만,
여기서 `부서="*"` 라고 적는 사람은 '부서를 안 가리고 전체' 를 뜻한다.

정규식을 그대로 열어 주지는 않는다. 패턴에 적힌 다른 글자는 전부 그대로
찾는 글자로 다룬다.
"""

from __future__ import annotations

import functools
import re

#: 탈출 문자 뒤에 올 수 있는 글자
_ESCAPABLE = "*?~"


def has(패턴: str) -> bool:
    """`~` 로 막지 않은 진짜 와일드카드가 들어 있는가."""
    글 = 패턴 or ""
    i = 0
    while i < len(글):
        if 글[i] == "~":
            i += 2                       # 뒤 한 글자는 글자 그대로
            continue
        if 글[i] in "*?":
            return True
        i += 1
    return False


@functools.lru_cache(maxsize=512)
def _regex(패턴: str) -> re.Pattern:
    글 = 패턴 or ""
    조각, i = [], 0
    while i < len(글):
        ch = 글[i]
        if ch == "~" and i + 1 < len(글) and 글[i + 1] in _ESCAPABLE:
            조각.append(re.escape(글[i + 1]))
            i += 2
            continue
        if ch == "*":
            조각.append(".*")
        elif ch == "?":
            조각.append(".")
        else:
            조각.append(re.escape(ch))
        i += 1
    return re.compile("".join(조각), re.DOTALL | re.IGNORECASE)


def like(값, 패턴: str) -> bool:
    """값이 패턴에 **전부** 맞는가. 엑셀처럼 앞뒤가 다 맞아야 한다."""
    return _regex(패턴 or "").fullmatch("" if 값 is None else str(값)) is not None
