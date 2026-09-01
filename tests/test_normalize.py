"""LLM 출력 정규화 테스트.

프롬프트로 "YYYYMM 6자리"라고 해도 모델은 형식을 자주 어긴다.
정규화가 없으면 그 값이 그대로 엑셀에 들어가 결과가 지저분해진다.
"""

from __future__ import annotations

import pytest

from cvtool import normalize as N
from cvtool.schemas import 학위상태_ENUM, 현재_신분_ENUM


# --- 연·월 -------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("201903", "201903"),
        ("2019.03", "201903"),
        ("2019-03", "201903"),
        ("2019/03", "201903"),
        ("2019.3", "201903"),
        ("2019년 3월", "201903"),
        ("Mar 2019", "201903"),
        ("March 2019", "201903"),
        ("2019 Mar", "201903"),
        ("20190315", "201903"),  # 날짜가 와도 연월만
        ("2019", "201900"),      # 연도만 알 때
        ("", ""),
        ("present", ""),
        ("재직중", ""),
        ("현재", ""),
        ("미상", ""),
    ],
)
def test_yyyymm(raw, expected):
    assert N.yyyymm(raw) == expected


def test_yyyymm_rejects_impossible_month():
    """13월 같은 값은 월을 버리고 연도만 남긴다."""
    assert N.yyyymm("2019-13") == "201900"


# --- 생년월일 ----------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("19920315", "19920315"),
        ("1992-03-15", "19920315"),
        ("1992.03.15", "19920315"),
        ("1992년 3월 15일", "19920315"),
        ("", ""),
        ("1992", ""),        # 일자가 없으면 버린다 (지어내지 않는다)
        ("19920332", ""),    # 불가능한 일자
        ("19921315", ""),    # 불가능한 월
    ],
)
def test_yyyymmdd(raw, expected):
    assert N.yyyymmdd(raw) == expected


# --- 전화번호 ----------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("01012345678", "010-1234-5678"),
        ("010-1234-5678", "010-1234-5678"),
        ("010 1234 5678", "010-1234-5678"),
        ("+82-10-1234-5678", "010-1234-5678"),
        ("", ""),
    ],
)
def test_phone(raw, expected):
    assert N.phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # 국가번호 앞뒤에 무엇이 붙어 있어도 알아본다. 예전에는 원문 글자가
        # '+82' 로 **시작**하는지 봐서, 괄호 하나에 전부 못 알아봤다.
        "(+82) 10-1234-5678",
        "(+82)10.1234.5678",
        "[+82] 10 1234 5678",
        "Tel: +82-10-1234-5678",
        "+82 10-1234-5678",
        "0082 10 1234 5678",
        "82 10 1234 5678",
        "+82 (0)10-1234-5678",      # 국가번호와 0 이 같이 적힌 경우
    ],
)
def test_phone_understands_country_code_anywhere(raw):
    assert N.phone(raw) == "010-1234-5678"


def test_phone_keeps_unknown_shape_as_is():
    """모르는 형태를 억지로 고치면 없는 번호를 만들어낸다."""
    assert N.phone("02-123-4567") == "02-123-4567"


@pytest.mark.parametrize(
    "raw",
    [
        "+1 415 555 2671",          # 외국 번호는 손대지 않는다
        "8210-1234",                # 82 로 시작하지만 한국 번호가 아니다
        "+44 20 7946 0958",
    ],
)
def test_phone_leaves_non_korean_numbers_alone(raw):
    """국가번호를 뗀 결과가 국내 형식에 맞을 때만 고친다."""
    assert N.phone(raw) == raw


def test_phone_still_handles_seoul_with_country_code():
    assert N.phone("(+82) 2-1234-5678") == "02-1234-5678"


# --- enum --------------------------------------------------------------------
def test_enum_passes_valid_value():
    assert N.enum("포닥", 현재_신분_ENUM, "불명") == "포닥"


def test_enum_falls_back_on_unknown():
    """목록 밖 값이 엑셀에 들어가면 필터가 깨진다."""
    assert N.enum("박사후연구원", 현재_신분_ENUM, "불명") == "불명"


def test_enum_absorbs_case_and_space():
    assert N.enum("  졸업  ", 학위상태_ENUM, "") == "졸업"


def test_enum_empty_uses_fallback():
    assert N.enum("", 현재_신분_ENUM, "불명") == "불명"


# --- 텍스트 ------------------------------------------------------------------
def test_text_collapses_newlines_for_excel():
    """셀 안 줄바꿈은 엑셀 표를 깨뜨린다."""
    assert N.text("서울대학교\n전기정보공학부") == "서울대학교 전기정보공학부"
    assert N.text("A\tB   C") == "A B C"


def test_text_handles_none():
    assert N.text(None) == ""


# --- 파이프라인 반영 ---------------------------------------------------------
def test_record_gets_normalized_values():
    """모델이 형식을 어겨도 최종 레코드는 약속한 형식이어야 한다."""
    from cvtool.extract import _assemble

    rec = _assemble(
        {
            "basic": {
                "한글_이름": "홍길동",
                "생년월일": "1992-03-15",
                "전화번호": "+82-10-1234-5678",
                "현재_신분": "박사후연구원",  # 목록 밖
            },
            "education": {"박사_시작": "2019.03", "박사_졸업": "Feb 2025"},
        },
        [],
        지원자_ID="T",
        원본_파일명="a.pdf",
    )
    assert rec.생년월일 == "19920315"
    assert rec.전화번호 == "010-1234-5678"
    assert rec.박사_시작 == "201903"
    assert rec.박사_졸업 == "202502"
    assert rec.현재_신분 == "불명"          # 목록 밖 값은 찍지 않는다
    assert rec.검토_필요 == "Y"
    assert "형식 보정" in rec.검토_사유      # 무엇을 고쳤는지 남는다
