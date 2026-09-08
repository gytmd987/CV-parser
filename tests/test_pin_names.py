"""소속·학교·전공을 사람이 직접 정하기.

기본은 명칭 사전을 따라간다. 손으로 고친 칸은 **그 지원자만** 사전을 안
따라간다 — 같은 학교라도 이 사람만 다르게 적어야 하는 일이 있다.
"""

from __future__ import annotations

import pytest

from cvtool.edit import (
    ValidationError,
    apply_edit,
    edit_field,
    보이는값,
    사전_따라가기,
)
from cvtool.names import NameRegistry
from cvtool.schemas import CVRecord


@pytest.fixture
def reg(tmp_path):
    r = NameRegistry(tmp_path / "n.db")
    나 = r.observe("소속", "서울대학교")
    r.classify(나.id, 표시명="서울대")
    return r


def _사람(cid="T") -> CVRecord:
    return CVRecord(지원자_ID=cid, 박사_학교="서울대학교")


# --- 고정 -------------------------------------------------------------------
def test_a_hand_written_value_stops_following_the_dictionary(reg):
    rec = _사람()
    assert rec.to_row(reg)["박사_학교"] == "서울대"          # 처음엔 따라간다

    apply_edit(rec, "박사_학교", "서울대 시흥캠퍼스", registry=reg)
    assert rec.to_row(reg)["박사_학교"] == "서울대 시흥캠퍼스"

    reg.classify(reg.lookup("소속", "서울대학교").id, 표시명="SNU")
    assert rec.to_row(reg)["박사_학교"] == "서울대 시흥캠퍼스"   # 안 움직인다


def test_the_other_applicants_still_follow(reg):
    """한 사람을 고정해도 나머지는 사전을 따라가야 한다. 이게 요점이다."""
    고정 = _사람("A")
    apply_edit(고정, "박사_학교", "서울대 시흥캠퍼스", registry=reg)
    보통 = _사람("B")

    reg.classify(reg.lookup("소속", "서울대학교").id, 표시명="SNU")
    assert 고정.to_row(reg)["박사_학교"] == "서울대 시흥캠퍼스"
    assert 보통.to_row(reg)["박사_학교"] == "SNU"


def test_pinning_does_not_overwrite_what_the_cv_said(reg):
    """원표기를 덮으면 되돌릴 수가 없다 (names.py 맨 앞의 약속)."""
    rec = _사람()
    apply_edit(rec, "박사_학교", "서울대 시흥캠퍼스", registry=reg)
    assert rec.박사_학교 == "서울대학교"
    assert rec.직접입력["박사_학교"] == "서울대 시흥캠퍼스"


def test_a_hand_written_value_is_not_added_to_the_dictionary(reg):
    """그 사람만의 예외다. 일회성 값이 쌓이면 명칭 관리가 지저분해진다."""
    rec = _사람()
    apply_edit(rec, "박사_학교", "서울대 시흥캠퍼스", registry=reg)
    assert reg.lookup("소속", "서울대 시흥캠퍼스") is None
    assert reg.display_names("소속") == ["서울대"]


# --- 되돌리기 ---------------------------------------------------------------
def test_typing_a_dictionary_name_releases_the_pin(reg):
    rec = _사람()
    apply_edit(rec, "박사_학교", "서울대 시흥캠퍼스", registry=reg)
    apply_edit(rec, "박사_학교", "서울대", registry=reg)      # 표시명 그대로
    assert "박사_학교" not in rec.직접입력
    assert rec.to_row(reg)["박사_학교"] == "서울대"


def test_a_value_that_only_normalizes_the_same_stays_pinned(reg):
    """`서울대학교(본교)` 는 괄호를 떼면 `서울대학교` 지만 **다른 값**이다.

    lookup 의 느슨함(정규화키)은 CV 표기를 묶을 때는 맞지만 여기서는 아니다 —
    고정하려고 적은 값이 조용히 풀려 버린다.
    """
    rec = _사람()
    apply_edit(rec, "박사_학교", "서울대학교(본교)", registry=reg)
    assert rec.직접입력["박사_학교"] == "서울대학교(본교)"
    assert rec.to_row(reg)["박사_학교"] == "서울대학교(본교)"


def test_the_unpin_helper_puts_it_back(reg):
    rec = _사람()
    apply_edit(rec, "박사_학교", "서울대 시흥캠퍼스", registry=reg)
    전, 후 = 사전_따라가기(rec, "박사_학교", reg)
    assert (전, 후) == ("서울대 시흥캠퍼스", "서울대")
    assert "박사_학교" not in rec.직접입력


def test_clearing_the_cell_clears_both(reg):
    """비운 것은 «빈칸» 이지 «이 사람은 예외» 가 아니다."""
    rec = _사람()
    apply_edit(rec, "박사_학교", "서울대 시흥캠퍼스", registry=reg)
    apply_edit(rec, "박사_학교", "", registry=reg)
    assert "박사_학교" not in rec.직접입력
    assert rec.박사_학교 == ""
    assert rec.to_row(reg)["박사_학교"] == ""


# --- 화면이 다 같은 값을 본다 -------------------------------------------------
def test_every_surface_shows_the_same_value(reg):
    rec = _사람()
    apply_edit(rec, "박사_학교", "서울대 시흥캠퍼스", registry=reg)
    assert 보이는값(rec, "박사_학교", reg) == "서울대 시흥캠퍼스"
    assert rec.to_row(reg)["박사_학교"] == "서울대 시흥캠퍼스"


def test_edit_field_reports_a_change_the_raw_value_cannot_show(reg):
    """고정하거나 풀면 원표기는 그대로다. 날값만 보면 «안 바뀜» 으로 새어 나간다.

    부르는 쪽이 그 판단으로 저장 여부를 정하므로, 여기서 놓치면 고정이
    저장되지 않는다.
    """
    rec = _사람()
    전, 후 = edit_field(rec, "박사_학교", "서울대 시흥캠퍼스", registry=reg)
    assert (전, 후) == ("서울대", "서울대 시흥캠퍼스")
    assert rec.박사_학교 == "서울대학교"          # 날값은 그대로였다

    전2, 후2 = edit_field(rec, "박사_학교", "서울대", registry=reg)
    assert (전2, 후2) == ("서울대 시흥캠퍼스", "서울대")


def test_editing_without_a_dictionary_is_refused(reg):
    """사전이 없으면 적은 값이 사전에 있는 이름인지 가릴 수가 없다."""
    with pytest.raises(ValidationError):
        apply_edit(_사람(), "박사_학교", "아무거나")


def test_all_eight_dictionary_columns_can_be_pinned(reg):
    from cvtool.schemas import NAME_COLUMNS

    for col in NAME_COLUMNS:
        rec = CVRecord(지원자_ID="T")
        apply_edit(rec, col, "손으로 적은 값", registry=reg)
        assert rec.직접입력[col] == "손으로 적은 값"
        assert rec.to_row(reg)[col] == "손으로 적은 값"


def test_an_old_record_has_no_pins(reg):
    """«직접입력» 이 없던 시절의 레코드. 빈 dict 로 읽히고 그대로 따라간다."""
    rec = CVRecord.model_validate({"지원자_ID": "T", "박사_학교": "서울대학교"})
    assert rec.직접입력 == {}
    assert rec.to_row(reg)["박사_학교"] == "서울대"


# --- 검색 -------------------------------------------------------------------
def test_a_pinned_value_is_searchable(tmp_path, reg):
    """방금 적어 넣은 값으로 그 사람을 못 찾으면 적어 넣은 뜻이 없다."""
    from cvtool.store import CandidateStore

    store = CandidateStore(tmp_path / "c.db", tmp_path / "files")
    rec = _사람()
    apply_edit(rec, "박사_학교", "서울대 시흥캠퍼스", registry=reg)
    store.save(rec, 저장_파일명="", 지문=[])

    assert [r.지원자_ID for r in store.list_filtered("시흥")] == ["T"]
