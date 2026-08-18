"""중복 지원자 검토."""

from __future__ import annotations

import pytest

from cvtool.dedup import (
    SIMILAR_THRESHOLD,
    Match,
    compare,
    find_duplicates,
    fingerprint,
    similarity,
)
from cvtool.schemas import CVRecord

원본 = "홍길동 서울대학교 박사 컴퓨터공학 NeurIPS 2024 1저자 논문 지도교수 김철수 이메일 hong@example.com"
살짝수정 = 원본 + " CVPR 2025 1저자 논문 한 줄 추가"
전혀다름 = "김영희 카이스트 석사 기계공학 유체역학 연구 경력 삼성전자 3년"


def _rec(**kw) -> CVRecord:
    kw.setdefault("지원자_ID", "X")
    return CVRecord(**kw)


# --- 지문 -------------------------------------------------------------------
def test_fingerprint_is_not_reversible():
    """원문을 복원할 수 없어야 한다 (개인정보를 덜 남긴다)."""
    fp = fingerprint(원본)
    assert fp and all("홍길동" not in chunk for chunk in fp)


def test_similar_documents_overlap():
    assert similarity(fingerprint(원본), fingerprint(살짝수정)) >= SIMILAR_THRESHOLD


def test_different_documents_do_not_overlap():
    assert similarity(fingerprint(원본), fingerprint(전혀다름)) < SIMILAR_THRESHOLD


def test_empty_text_is_safe():
    assert fingerprint("") == []
    assert similarity([], fingerprint(원본)) == 0.0


# --- 확실한 중복 ------------------------------------------------------------
def test_same_email_is_certain():
    a = _rec(지원자_ID="A", 이메일="hong@example.com")
    b = _rec(지원자_ID="B", 이메일="hong@example.com | other@x.com")
    m = compare(a, [], b, [])
    assert m and m.수준 == "확실" and "이메일" in m.이유


def test_same_phone_is_certain():
    a = _rec(지원자_ID="A", 전화번호="010-1234-5678")
    b = _rec(지원자_ID="B", 전화번호="01012345678")   # 표기가 달라도 같은 번호
    m = compare(a, [], b, [])
    assert m and m.수준 == "확실"


def test_name_plus_birthdate_is_certain():
    a = _rec(지원자_ID="A", 한글_이름="홍길동", 생년월일="19920315")
    b = _rec(지원자_ID="B", 한글_이름="홍길동", 생년월일="19920315")
    m = compare(a, [], b, [])
    assert m and m.수준 == "확실"


def test_same_name_different_birthdate_is_not_certain():
    """동명이인을 같은 사람으로 만들면 안 된다."""
    a = _rec(지원자_ID="A", 한글_이름="홍길동", 생년월일="19920315")
    b = _rec(지원자_ID="B", 한글_이름="홍길동", 생년월일="19880101")
    m = compare(a, [], b, [])
    assert m is None or m.수준 == "의심"


# --- 의심 -------------------------------------------------------------------
def test_modified_cv_is_suspected():
    """지원서를 조금 고쳐 다시 낸 경우."""
    a = _rec(지원자_ID="A")
    b = _rec(지원자_ID="B")
    m = compare(a, fingerprint(원본), b, fingerprint(살짝수정))
    assert m and m.수준 == "의심" and m.유사도 >= SIMILAR_THRESHOLD


def test_name_plus_school_is_suspected():
    a = _rec(지원자_ID="A", 한글_이름="홍길동", 박사_학교="서울대학교")
    b = _rec(지원자_ID="B", 한글_이름="홍길동", 박사_학교="서울대학교")
    m = compare(a, [], b, [])
    assert m and m.수준 == "의심"


def test_unrelated_people_are_not_flagged():
    a = _rec(지원자_ID="A", 한글_이름="홍길동", 이메일="hong@x.com")
    b = _rec(지원자_ID="B", 한글_이름="김영희", 이메일="kim@y.com")
    assert compare(a, fingerprint(원본), b, fingerprint(전혀다름)) is None


# --- 목록 -------------------------------------------------------------------
def test_certain_matches_come_first():
    new = _rec(지원자_ID="NEW", 한글_이름="홍길동", 이메일="hong@x.com")
    cands = [
        (_rec(지원자_ID="SUSPECT", 한글_이름="홍길동"), []),
        (_rec(지원자_ID="CERTAIN", 이메일="hong@x.com"), []),
    ]
    ids = [m.지원자_ID for m in find_duplicates(new, [], cands)]
    assert ids[0] == "CERTAIN"


def test_self_is_skipped():
    """재분석할 때 자기 자신이 중복으로 잡히면 안 된다."""
    rec = _rec(지원자_ID="SAME", 이메일="a@x.com")
    assert find_duplicates(rec, [], [(rec, [])]) == []


def test_no_candidates_is_empty():
    assert find_duplicates(_rec(), [], []) == []


def test_match_message_includes_similarity():
    assert "80%" in str(Match("A", "의심", "내용 겹침", 0.8))
