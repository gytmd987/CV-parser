"""1단계: 추출 정확도 수정 (졸업 판정 / 전공명 / 다중값 구분자)."""

from __future__ import annotations

import pytest

from cvtool import normalize as N
from cvtool.extract import _assemble


# --- 졸업일과 학위상태 모순 --------------------------------------------------
@pytest.mark.parametrize(
    "상태,졸업,기대",
    [
        ("재학", "202602", "졸업"),   # 졸업일이 지났으면 졸업
        ("예정", "202602", "졸업"),
        ("", "202602", "졸업"),
        ("졸업", "202602", "졸업"),
        ("수료", "202602", "수료"),   # 수료는 별개 상태라 건드리지 않는다
        ("재학", "202702", "재학"),   # 아직 안 왔으면 그대로
        ("재학", "", "재학"),         # 졸업일이 없으면 판단하지 않는다
    ],
)
def test_degree_status_matches_graduation_date(상태, 졸업, 기대):
    assert N.degree_status(상태, 졸업, "202608") == 기대


def test_the_status_is_recomputed_as_the_date_passes():
    """같은 값이라도 **오늘이 언제냐**에 따라 답이 달라져야 한다.

    예전에는 추출할 때 한 번 계산해 저장했다. 그래서 등록한 뒤에 졸업일이
    지나도 표에 '재학' 이 그대로 남았다.
    """
    assert N.degree_status("재학", "202602", "202601") == "재학"
    assert N.degree_status("재학", "202602", "202602") == "졸업"


def test_future_graduation_with_graduated_status_is_flagged():
    """시간이 지나도 안 풀리는 모순이라 사람이 봐야 한다."""
    assert "확인 필요" in N.degree_status_warning("졸업", "202712", "202608")
    assert N.degree_status("졸업", "202712", "202608") == "졸업"  # 값은 안 건드린다


def test_a_date_that_has_passed_is_not_a_warning():
    """저절로, 늘 맞게 되는 것은 사람에게 알릴 일이 아니다."""
    assert N.degree_status_warning("재학", "202602", "202608") == ""
    assert N.degree_status_warning("재학", "", "202608") == ""


def test_assemble_stores_what_the_cv_said():
    """추출은 고치지 않는다. 졸업일과 대조하는 일은 볼 때마다 한다."""
    rec = _assemble(
        {"education": {"박사_졸업": "202002", "박사_학위상태": "재학"}},
        [], 지원자_ID="T", 원본_파일명="a.pdf",
    )
    assert rec.박사_학위상태 == "재학"          # 저장은 이력서 그대로
    assert rec.to_row()["박사_학위상태"] == "졸업"   # 표에는 졸업
    # 저절로 맞는 것을 검토로 올리지 않는다 (예전의 «보정» 사유)
    assert "보정" not in rec.검토_사유


# --- 전공명 -----------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,기대",
    [
        ("전기공학전공", "전기공학"),
        ("컴퓨터공학과", "컴퓨터공학과"),      # ~과 는 정상 표기라 둔다
        ("기계공학", "기계공학"),
        ("전기·전자공학전공", "전기·전자공학"),  # 가운뎃점은 이름의 일부
        ("컴퓨터공학 협동과정", "컴퓨터공학"),
        ("전공", "전공"),                      # 너무 짧아지면 원문 유지
        ("", ""),
    ],
)
def test_major_suffix(raw, 기대):
    assert N.major(raw) == 기대


def test_double_major_uses_one_separator():
    assert N.major("전기공학전공/컴퓨터공학전공") == "전기공학 | 컴퓨터공학"


# --- 다중값 구분자 통일 ------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    ["a@x.com, b@y.com", "a@x.com / b@y.com", "a@x.com; b@y.com", "a@x.com\nb@y.com"],
)
def test_emails_always_use_the_same_separator(raw):
    """어떤 건 ',' 어떤 건 '/' 로 나오던 문제."""
    assert N.emails(raw) == "a@x.com | b@y.com"


def test_emails_lowercased_and_deduped():
    assert N.emails("A@X.com, a@x.COM, b@y.com") == "a@x.com | b@y.com"


def test_phones_multiple():
    assert N.phones("01012345678, 010-9999-8888") == "010-1234-5678 | 010-9999-8888"


def test_multi_drops_empty_and_duplicates():
    assert N.multi("a,,b, a ,b") == "a | b"


def test_multi_accepts_list():
    assert N.multi(["컴퓨터비전", "멀티모달"]) == "컴퓨터비전 | 멀티모달"


def test_multi_keeps_middle_dot():
    """'전기·전자' 는 한 단어다. 나누면 안 된다."""
    assert N.multi("전기·전자공학") == "전기·전자공학"


def test_assemble_unifies_separators_across_fields():
    rec = _assemble(
        {
            "basic": {"이메일": "A@x.com / b@y.com", "전화번호": "01012345678,010-9999-8888"},
            "education": {"박사_전공": "전기공학전공"},
            "research": {"연구분야_키워드": ["컴퓨터비전", "멀티모달"]},
        },
        [], 지원자_ID="T", 원본_파일명="a.pdf",
    )
    assert rec.이메일 == "a@x.com | b@y.com"
    assert rec.전화번호 == "010-1234-5678 | 010-9999-8888"
    assert rec.박사_전공 == "전기공학"
    assert rec.연구분야_키워드 == "컴퓨터비전 | 멀티모달"


def test_no_field_uses_comma_separator():
    """표 전체에서 구분자가 하나로 통일돼야 한다."""
    rec = _assemble(
        {
            "basic": {"이메일": "a@x.com, b@y.com"},
            "research": {"연구분야_키워드": ["가", "나"]},
        },
        [], 지원자_ID="T", 원본_파일명="a.pdf",
    )
    row = rec.to_row()
    for col in ("이메일", "연구분야_키워드"):
        assert "," not in row[col], f"{col} 에 쉼표 구분자가 남아 있다"
        assert N.MULTI_SEP in row[col]
