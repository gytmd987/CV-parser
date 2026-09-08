"""학위상태는 **볼 때마다** 졸업일과 대조한다.

예전에는 추출할 때 한 번 계산해 저장했다. 그래서 2025년에 「졸업 202602 /
재학」으로 등록된 사람이 지금 졸업일이 지났는데도 표에 계속 재학으로 남았다.
재분석하기 전에는 아무도 모른다.
"""

from __future__ import annotations

import pytest

from cvtool import normalize as N
from cvtool.edit import ConflictError, apply_edit
from cvtool.extract import _assemble
from cvtool.schemas import CVRecord


def _사람(상태: str, 졸업: str) -> CVRecord:
    return CVRecord(지원자_ID="T", 한글_이름="홍길동",
                    박사_학위상태=상태, 박사_졸업=졸업)


def test_a_passed_graduation_date_shows_as_graduated(monkeypatch):
    """저장값이 재학이어도 졸업일이 지났으면 표에는 졸업."""
    _오늘로(monkeypatch, "202608")
    rec = _사람("재학", "202602")
    assert rec.to_row()["박사_학위상태"] == "졸업"
    assert rec.박사_학위상태 == "재학"          # 저장값은 안 바뀐다


def test_the_same_record_changes_answer_as_time_passes(monkeypatch):
    """재분석도, 저장도 없이 **날짜만 지나도** 따라와야 한다. 이게 요점이다."""
    rec = _사람("재학", "202602")

    _오늘로(monkeypatch, "202601")
    assert rec.to_row()["박사_학위상태"] == "재학"

    _오늘로(monkeypatch, "202602")
    assert rec.to_row()["박사_학위상태"] == "졸업"


@pytest.mark.parametrize("상태", ["수료", "졸업"])
def test_a_status_the_recruiter_chose_is_left_alone(monkeypatch, 상태):
    """담당자가 «수료» 를 골라 두면 그대로 남아야 한다."""
    _오늘로(monkeypatch, "202608")
    assert _사람(상태, "202602").to_row()["박사_학위상태"] == 상태


def test_without_a_graduation_date_nothing_is_decided(monkeypatch):
    _오늘로(monkeypatch, "202608")
    assert _사람("재학", "").to_row()["박사_학위상태"] == "재학"
    assert _사람("재학", "20260").to_row()["박사_학위상태"] == "재학"   # 6자리가 아니다


def test_extraction_stores_what_the_cv_said():
    """추출은 고치지 않는다. 고치면 그 시점에 얼어붙는다.

    (`extract` 는 `now_kst` 를 모듈 위에서 가져와서 `_오늘로` 가 안 먹는다.
    어차피 202002 는 어느 날로 봐도 지난 날짜라 고정할 필요가 없다.)
    """
    rec = _assemble({"education": {"박사_졸업": "202002", "박사_학위상태": "재학"}},
                    [], 지원자_ID="T", 원본_파일명="a.pdf")
    assert rec.박사_학위상태 == "재학"
    assert "보정" not in rec.검토_사유      # 저절로 맞는 것은 알릴 일이 아니다


def test_a_graduation_date_that_has_not_come_is_still_flagged():
    """시간이 지나도 안 풀리는 모순은 사람이 봐야 한다."""
    rec = _assemble({"education": {"박사_졸업": "209912", "박사_학위상태": "졸업"}},
                    [], 지원자_ID="T", 원본_파일명="a.pdf")
    assert "확인 필요" in rec.검토_사유
    assert rec.검토_필요 == "Y"


def test_editing_the_status_does_not_collide_with_the_computed_one(monkeypatch):
    """화면에는 «졸업» 이 보이고 저장값은 «재학» 이다.

    날값끼리 견주면 손도 안 댄 칸이 매번 "다른 사람이 방금 바꿨습니다" 가 된다
    — 명칭 사전 열에서 이미 겪은 일이다.
    """
    _오늘로(monkeypatch, "202608")
    rec = _사람("재학", "202602")
    보이던값 = rec.to_row()["박사_학위상태"]
    assert 보이던값 == "졸업"

    옛값, 저장값 = apply_edit(rec, "박사_학위상태", "수료", 기대_이전값=보이던값)
    assert 저장값 == "수료"
    assert rec.to_row()["박사_학위상태"] == "수료"


def test_a_real_collision_is_still_caught(monkeypatch):
    """누군가 먼저 고친 것은 여전히 막아야 한다."""
    _오늘로(monkeypatch, "202608")
    rec = _사람("재학", "202602")
    with pytest.raises(ConflictError):
        apply_edit(rec, "박사_학위상태", "수료", 기대_이전값="예정")


def _오늘로(monkeypatch, yyyymm: str) -> None:
    """`now_kst` 를 그 달로 고정한다. 시간이 흐른 것처럼 굴게 하는 유일한 길."""
    import datetime

    from cvtool import timeutil

    가짜 = datetime.datetime(int(yyyymm[:4]), int(yyyymm[4:]), 15,
                           tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(timeutil, "now_kst", lambda: 가짜)


def test_the_helper_pins_today(monkeypatch):
    """위 시험들이 기대는 장치가 실제로 먹는지 확인한다."""
    _오늘로(monkeypatch, "203001")
    from cvtool.timeutil import now_kst

    assert now_kst().strftime("%Y%m") == "203001"


def test_normalize_only_promotes_the_three_open_states():
    for 상태 in ("재학", "예정", ""):
        assert N.degree_status(상태, "202602", "202608") == "졸업"
    for 상태 in ("수료", "졸업"):
        assert N.degree_status(상태, "202602", "202608") == 상태
