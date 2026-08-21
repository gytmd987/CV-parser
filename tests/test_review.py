"""검토 필요 항목 — 사유를 하나씩 처리할 수 있어야 한다."""

from __future__ import annotations

import pytest

from cvtool import review as R


사유 = (
    "이름 한글추정 (원문 대조 필요)"
    " / 현재_신분을 판단하지 못함"
    " / 연구분야 키워드를 뽑지 못함 (논문·과제에서도 못 읽음 — 확인 필요)"
    " / 형식 보정: 박사_졸업='2026'->202600"
)


def test_reasons_split_into_items():
    assert len(R.split(사유)) == 4
    assert R.split("") == []
    assert R.split(" / / ") == []


def test_each_item_points_at_its_columns():
    """상세 화면에서 그 줄을 짚어 주려면 어느 열인지 알아야 한다."""
    묶음 = {x["글"]: x["열"] for x in R.items(사유)}
    assert 묶음["현재_신분을 판단하지 못함"] == ["현재_신분"]
    assert 묶음["형식 보정: 박사_졸업='2026'->202600"] == ["박사_졸업"]
    assert 묶음["연구분야 키워드를 뽑지 못함 (논문·과제에서도 못 읽음 — 확인 필요)"] \
        == ["연구분야_키워드"]
    assert set(묶음["이름 한글추정 (원문 대조 필요)"]) == {"한글_이름", "영문_이름"}


def test_longer_column_names_win():
    """'박사_학교' 가 '학교' 보다 먼저 잡혀야 한다."""
    assert R.columns_for("박사_학교 확인") == ["박사_학교"]


def test_unknown_reason_has_no_column():
    assert R.columns_for("무슨 말인지 모를 사유") == []


def test_finished_items_are_marked():
    끝 = {"현재_신분을 판단하지 못함"}
    상태 = {x["글"]: x["완료"] for x in R.items(사유, 끝)}
    assert 상태["현재_신분을 판단하지 못함"] is True
    assert 상태["형식 보정: 박사_졸업='2026'->202600"] is False


def test_flag_clears_only_when_everything_is_done():
    assert R.flagged(사유) == "Y"
    assert R.flagged(사유, {"현재_신분을 판단하지 못함"}) == "Y"
    assert R.flagged(사유, set(R.split(사유))) == ""
    assert R.flagged("") == ""


def test_columns_needing_review_skips_finished_items():
    남은 = R.columns_needing_review(사유, {"현재_신분을 판단하지 못함"})
    assert "현재_신분" not in 남은
    assert "박사_졸업" in 남은


def test_short_drops_the_trailing_parenthetical():
    assert R.short("연구분야 키워드를 뽑지 못함 (논문·과제에서도 못 읽음 — 확인 필요)") \
        == "연구분야 키워드를 뽑지 못함"
    assert R.short("가" * 100, 10).endswith("…")
