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


#: 두 자리 지역번호는 서울(02) 하나뿐이고 나머지는 세 자리다.
_AREA_3 = (
    "010", "011", "016", "017", "018", "019",          # 휴대전화
    "031", "032", "033", "041", "042", "043", "044",   # 경기·강원·충청
    "051", "052", "053", "054", "055",                 # 경상
    "061", "062", "063", "064",                        # 전라·제주
    "070", "050", "080",                               # 인터넷·안심·수신자부담
)
#: 1588 같은 대표번호
_SPECIAL_4 = ("15", "16", "18")


def _strip_kr_country_code(text: str, digits: str) -> str:
    """+82 / 0082 / 82 로 시작하면 국내 형식(0으로 시작)으로 되돌린다.

    '+82 (0)10-...' 처럼 국가번호와 0 이 같이 적힌 경우도 있어서,
    국가번호를 뗀 뒤 남은 0 을 한 번 더 걷어내고 0 을 새로 붙인다.
    """
    국가번호 = text.lstrip().startswith(("+82", "0082", "82"))
    if not 국가번호:
        return digits
    if digits.startswith("0082"):
        digits = digits[4:]
    elif digits.startswith("82"):
        digits = digits[2:]
    return "0" + digits.lstrip("0")


def _format_kr(digits: str) -> str:
    """국내 번호를 지역번호에 맞춰 끊는다. 모르면 빈 문자열."""
    if digits.startswith("02") and len(digits) in (9, 10):
        가운데 = digits[2:-4]
        return f"02-{가운데}-{digits[-4:]}"
    if digits[:3] in _AREA_3 and len(digits) in (10, 11):
        가운데 = digits[3:-4]
        return f"{digits[:3]}-{가운데}-{digits[-4:]}"
    if len(digits) == 8 and digits[:2] in _SPECIAL_4:
        return f"{digits[:4]}-{digits[4:]}"
    return ""


def phone(value: str) -> str:
    """전화번호를 010-1234-5678 꼴로. 국가번호(+82)는 0 으로 되돌린다.

    한국 번호가 아니면(+1 등) 손대지 않는다. 지어내는 것보다 원문이 낫다.
    """
    if not value:
        return ""
    text = str(value).strip()
    digits = _digits(text)
    if not digits:
        return ""
    digits = _strip_kr_country_code(text, digits)
    return _format_kr(digits) or text


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


# ---------------------------------------------------------------------------
# 다중값 — 여러 개일 때 항상 같은 형식으로
# ---------------------------------------------------------------------------
#: 값이 여러 개일 때 쓰는 유일한 구분자. 엑셀 셀 안에서 안전하다.
MULTI_SEP = " | "

# 모델은 같은 항목도 , / ; 줄바꿈을 섞어 쓴다. 전부 이걸로 나눈다.
# 가운뎃점(·)은 '전기·전자공학' 처럼 이름의 일부라 나누지 않는다.
_SPLIT_RE = re.compile(r"[,;/\n\r]+|\s\|\s")


def multi(value) -> str:
    """여러 값을 MULTI_SEP 하나로 통일한다. 중복과 빈 값은 버린다.

    이메일이 어떤 건 ',' 어떤 건 '/' 로 구분돼 나오던 문제를 없앤다.
    """
    if value is None:
        return ""
    items = value if isinstance(value, (list, tuple)) else _SPLIT_RE.split(str(value))
    out: list[str] = []
    for item in items:
        cleaned = text(item)
        if cleaned and cleaned not in out:  # 순서 유지 + 중복 제거
            out.append(cleaned)
    return MULTI_SEP.join(out)


def emails(value) -> str:
    """이메일 여러 개를 통일된 형식으로. 소문자로 맞춘다.

    소문자로 바꾼 뒤에 중복을 없애야 'A@x.com' 과 'a@X.com' 이 하나로 합쳐진다.
    """
    joined = multi(value)
    if not joined:
        return ""
    out: list[str] = []
    for part in joined.split(MULTI_SEP):
        low = part.lower()
        if low not in out:
            out.append(low)
    return MULTI_SEP.join(out)


def phones(value) -> str:
    """전화번호 여러 개를 통일된 형식으로."""
    joined = multi(value)
    if not joined:
        return ""
    return MULTI_SEP.join(phone(part) for part in joined.split(MULTI_SEP))


# ---------------------------------------------------------------------------
# 전공명
# ---------------------------------------------------------------------------
# 전공은 보통 '~학' 이나 '~과' 로 끝난다. 뒤에 붙는 이 꼬리표들은 떼어낸다.
# 긴 것부터 지워야 '전공과정' 이 '전공' 으로 잘못 잘리지 않는다.
_MAJOR_TAILS = ("전공과정", "학위과정", "협동과정", "세부전공", "전공", "과정")


def major(value) -> str:
    """전공명을 다듬는다. '전기공학전공' -> '전기공학'.

    여러 전공이면 MULTI_SEP 로 이어 붙인다(복수전공).
    남는 글자가 너무 짧아지면 원문을 그대로 둔다. 잘못 자르느니 그대로가 낫다.
    """
    joined = multi(value)
    if not joined:
        return ""
    out = []
    for part in joined.split(MULTI_SEP):
        for tail in _MAJOR_TAILS:
            if part.endswith(tail) and len(part) - len(tail) >= 2:
                part = part[: -len(tail)].strip()
                break
        out.append(part)
    return MULTI_SEP.join(out)


# ---------------------------------------------------------------------------
# 학위 상태 — 졸업일과 모순되지 않게
# ---------------------------------------------------------------------------
def degree_status(status: str, 졸업: str, 오늘: str) -> tuple[str, str]:
    """학위상태를 졸업일과 대조해 바로잡는다.

    졸업일이 이미 지났는데 '재학' 으로 나오는 오류가 있었다.
    지난 날짜면 졸업으로 보고, 무엇을 바꿨는지 함께 돌려준다.

    Args:
        status: 모델이 낸 학위상태
        졸업:   YYYYMM (빈 문자열이면 판단 불가)
        오늘:   YYYYMM (KST 기준)
    Returns:
        (보정된 상태, 보정 사유 — 없으면 빈 문자열)
    """
    현재 = (status or "").strip()
    if not 졸업 or len(졸업) != 6 or not 졸업.isdigit():
        return 현재, ""

    지남 = 졸업 <= 오늘  # YYYYMM 문자열은 그대로 비교해도 순서가 맞다
    if 지남 and 현재 in ("재학", "예정", ""):
        원래 = 현재 or "(빈칸)"
        return "졸업", f"졸업일({졸업})이 지나 학위상태를 {원래}->졸업 으로 보정"
    if not 지남 and 현재 == "졸업":
        return 현재, f"졸업일({졸업})이 아직 오지 않았는데 상태가 졸업 (확인 필요)"
    return 현재, ""
