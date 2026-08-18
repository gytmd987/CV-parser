"""중복 지원자 검토.

같은 사람이 두 번 등록되는 것을 막는다. 두 가지로 나눠서 본다.

  확실  — 이메일이나 전화번호가 같다. 이름+생년월일이 같다.
          같은 사람으로 봐도 무방하다.
  의심  — 이름이 같고 학교도 같다. 또는 **CV 내용이 많이 겹친다.**
          지원서를 조금 고쳐서 다시 낸 경우가 여기 걸린다.

CV 내용 비교는 원문을 통째로 들고 비교하지 않는다. 개인정보를 덜 남기고
빠르게 보려고 **지문(fingerprint)** 만 저장해 겹치는 정도를 잰다.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

#: 지문에 담을 최대 조각 수. 많을수록 정확하지만 저장이 커진다.
FINGERPRINT_SIZE = 128

#: 이 정도 겹치면 같은 문서로 의심한다 (0~1)
SIMILAR_THRESHOLD = 0.55

_WORD_RE = re.compile(r"[0-9a-zA-Z가-힣]+")


def fingerprint(text: str) -> list[str]:
    """CV 텍스트의 지문. 단어 3개씩 묶어 해시하고 작은 것부터 고른다.

    문장이 조금 바뀌어도 대부분의 조각은 그대로 남아서 겹침이 유지된다.
    원문을 복원할 수는 없다.
    """
    words = _WORD_RE.findall((text or "").lower())
    if not words:
        return []
    shingles = {
        hashlib.md5(" ".join(words[i : i + 3]).encode("utf-8")).hexdigest()[:12]
        for i in range(max(1, len(words) - 2))
    }
    return sorted(shingles)[:FINGERPRINT_SIZE]


def similarity(a: list[str], b: list[str]) -> float:
    """두 지문이 겹치는 정도 (자카드 유사도)."""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


def _norm(value: str) -> str:
    return "".join((value or "").split()).lower()


def _email_set(value: str) -> set[str]:
    from .normalize import MULTI_SEP

    return {p.strip().lower() for p in (value or "").split(MULTI_SEP) if p.strip()}


def _phone_set(value: str) -> set[str]:
    from .normalize import MULTI_SEP

    return {
        re.sub(r"\D", "", p) for p in (value or "").split(MULTI_SEP) if re.sub(r"\D", "", p)
    }


@dataclass
class Match:
    지원자_ID: str
    수준: str        # "확실" | "의심"
    이유: str
    유사도: float = 0.0

    def __str__(self) -> str:
        점수 = f" (내용 {self.유사도:.0%} 일치)" if self.유사도 else ""
        return f"{self.지원자_ID}: {self.이유}{점수}"


def compare(new_rec, new_fp: list[str], old_rec, old_fp: list[str]) -> Match | None:
    """지원자 둘을 비교해 중복 여부를 판정한다."""
    이유: list[str] = []

    공통_이메일 = _email_set(new_rec.이메일) & _email_set(old_rec.이메일)
    if 공통_이메일:
        이유.append(f"이메일 일치({', '.join(sorted(공통_이메일))})")

    공통_전화 = _phone_set(new_rec.전화번호) & _phone_set(old_rec.전화번호)
    if 공통_전화:
        이유.append("전화번호 일치")

    이름같음 = bool(_norm(new_rec.한글_이름)) and _norm(new_rec.한글_이름) == _norm(
        old_rec.한글_이름
    )
    영문같음 = bool(_norm(new_rec.영문_이름)) and _norm(new_rec.영문_이름) == _norm(
        old_rec.영문_이름
    )
    생일같음 = bool(new_rec.생년월일) and new_rec.생년월일 == old_rec.생년월일

    if (이름같음 or 영문같음) and 생일같음:
        이유.append("이름+생년월일 일치")

    if 이유:
        return Match(old_rec.지원자_ID, "확실", " / ".join(이유))

    # --- 여기부터는 의심 ---
    점수 = similarity(new_fp, old_fp)
    if 점수 >= SIMILAR_THRESHOLD:
        return Match(old_rec.지원자_ID, "의심", "CV 내용이 많이 겹침", 점수)

    학교같음 = bool(_norm(new_rec.박사_학교)) and _norm(new_rec.박사_학교) == _norm(
        old_rec.박사_학교
    )
    if (이름같음 or 영문같음) and 학교같음:
        return Match(old_rec.지원자_ID, "의심", "이름+박사 학교 일치", 점수)
    if 이름같음 or 영문같음:
        return Match(old_rec.지원자_ID, "의심", "이름 일치", 점수)
    return None


def find_duplicates(new_rec, new_fp, candidates) -> list[Match]:
    """등록된 지원자들과 비교해 중복 후보를 찾는다.

    Args:
        candidates: (레코드, 지문) 목록
    Returns:
        확실한 것부터, 유사도 높은 순
    """
    matches = []
    for old_rec, old_fp in candidates:
        if old_rec.지원자_ID == new_rec.지원자_ID:
            continue
        m = compare(new_rec, new_fp, old_rec, old_fp)
        if m:
            matches.append(m)
    matches.sort(key=lambda m: (m.수준 != "확실", -m.유사도))
    return matches
