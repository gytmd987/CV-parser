"""LLM 출력 정규화.

⚠️ 여기서 정규식을 쓰지만 **CV 본문을 파싱하는 것이 아니다.**
LLM 이 이미 뽑아준 짧은 값("2019.03", "Mar 2019")을 약속한 형식으로 맞출 뿐이다.
CV 를 읽고 판단하는 일은 전부 LLM 이 한다.

이게 없으면 모델이 형식을 조금만 어겨도 그대로 엑셀에 들어간다.
프롬프트로 "YYYYMM 6자리"라고 해도 실제로는 이런 값들이 섞여 나온다:
    "2019.03"  "2019-3"  "Mar 2019"  "2019년 3월"  "2019"  "present"
"""

from __future__ import annotations

import re

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# 진행 중을 뜻하는 표현. 날짜 칸에서는 비운다.
_ONGOING = {
    "present", "current", "now", "ongoing", "재직중", "재학중", "현재",
    "-", "–", "~", "진행중",
}

_DIGITS_RE = re.compile(r"\d+")
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _digits(text: str) -> str:
    return "".join(_DIGITS_RE.findall(text))


def yyyymm(value: str) -> str:
    """연·월을 YYYYMM 6자리로. 월을 모르면 YYYY00, 못 읽으면 빈 문자열."""
    if not value:
        return ""
    text = str(value).strip().lower()
    if text in _ONGOING:
        return ""

    # 이미 6자리 숫자면 그대로 (00 월 허용)
    digits = _digits(text)
    if len(digits) == 6 and _valid_month(digits[4:]):
        return digits
    if len(digits) == 8 and _valid_month(digits[4:6]):  # YYYYMMDD 가 왔을 때
        return digits[:6]

    year_m = _YEAR_RE.search(text)
    if not year_m:
        return ""
    year = year_m.group(0)

    # 영문 월 이름
    for name, num in _MONTHS.items():
        if name in text:
            return f"{year}{num:02d}"

    # 연도를 빼고 남은 첫 숫자를 월로 본다 ("2019.03", "2019년 3월", "2019-3")
    rest = text[: year_m.start()] + text[year_m.end() :]
    month_m = _DIGITS_RE.search(rest)
    if month_m:
        month = int(month_m.group(0))
        if 1 <= month <= 12:
            return f"{year}{month:02d}"
    return f"{year}00"  # 연도만 확인됨


def yyyymmdd(value: str) -> str:
    """생년월일을 YYYYMMDD 8자리로. 못 읽으면 빈 문자열.

    "1992년 3월 15일" 처럼 월·일이 한 자리로 와도 받아준다.
    """
    if not value:
        return ""
    text = str(value).strip()

    # 붙어 있는 8자리
    digits = _digits(text)
    if len(digits) == 8 and _ymd_ok(digits[:4], digits[4:6], digits[6:]):
        return digits

    # 구분자로 나뉜 경우: 연도를 찾고 나머지 숫자를 월·일로 본다
    year_m = _YEAR_RE.search(text)
    if not year_m:
        return ""
    year = year_m.group(0)
    rest = _DIGITS_RE.findall(text[: year_m.start()] + text[year_m.end() :])
    if len(rest) < 2:
        return ""
    month, day = rest[0].zfill(2), rest[1].zfill(2)
    return f"{year}{month}{day}" if _ymd_ok(year, month, day) else ""


def _ymd_ok(y: str, m: str, d: str) -> bool:
    try:
        return len(y) == 4 and 1 <= int(m) <= 12 and 1 <= int(d) <= 31
    except ValueError:
        return False


def _valid_month(mm: str) -> bool:
    try:
        return 0 <= int(mm) <= 12
    except ValueError:
        return False


def phone(value: str) -> str:
    """전화번호를 010-1234-5678 꼴로. 국가번호(+82)는 0 으로 되돌린다."""
    if not value:
        return ""
    text = str(value).strip()
    digits = _digits(text)
    if not digits:
        return ""
    if text.startswith("+82") or digits.startswith("82"):
        digits = "0" + digits[2:] if digits.startswith("82") else digits
    if len(digits) == 11 and digits.startswith("01"):
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if len(digits) == 10 and digits.startswith("01"):
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return text  # 형태를 모르면 원문 그대로 둔다 (지어내지 않는다)


def email(value: str) -> str:
    """공백만 정리. 유효성 판단은 하지 않는다 (지어내면 안 되므로)."""
    return str(value or "").strip()


def enum(value: str, allowed: list[str], fallback: str = "") -> str:
    """허용 목록 밖의 값이면 fallback 으로. 대소문자·공백 차이는 흡수한다."""
    text = str(value or "").strip()
    if text in allowed:
        return text
    lowered = {a.lower(): a for a in allowed}
    if text.lower() in lowered:
        return lowered[text.lower()]
    return fallback


def text(value: str) -> str:
    """셀 안 줄바꿈·탭을 없앤다. 엑셀 표가 깨지지 않게."""
    return " ".join(str(value or "").split())
