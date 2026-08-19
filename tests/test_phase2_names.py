"""2단계: 명칭 사전 · 표 즉시 반영 · 등급별 개수 열."""

from __future__ import annotations

import pytest

from cvtool.names import GRADED_KINDS, KINDS, NameRegistry, normalize, observe_record
from cvtool.schemas import CVRecord, Paper, columns


@pytest.fixture
def reg(tmp_path):
    return NameRegistry(tmp_path / "n.db")


# --- 정규화 -----------------------------------------------------------------
def test_parentheses_merge_automatically():
    """'포항공과대학교(POSTECH)' 와 '포항공과대학교' 는 저절로 묶여야 한다."""
    assert normalize("포항공과대학교(POSTECH)", "학교") == normalize("포항공과대학교", "학교")


def test_different_spellings_stay_separate_until_merged():
    """'포항공대' 와 'POSTECH' 은 자동으로 묶으면 위험하다. 사람이 묶는다."""
    assert normalize("포항공대", "학교") != normalize("POSTECH", "학교")


def test_venue_noise_removed():
    assert normalize("Proc. of ICML 2023", "학회") == normalize("ICML", "학회")


# --- 등록 · 병합 ------------------------------------------------------------
def test_observe_registers_and_counts(reg):
    a = reg.observe("학교", "서울대학교")
    b = reg.observe("학교", "서울대학교")
    assert a.id == b.id and b.발견횟수 == 2


def test_same_name_different_kinds_are_separate(reg):
    학회 = reg.observe("학회", "ICML")
    저널 = reg.observe("저널", "ICML")
    assert 학회.id != 저널.id


def test_merge_makes_alias_resolve_to_target(reg):
    a = reg.observe("학교", "포항공대")
    b = reg.observe("학교", "포항공과대학교")
    reg.merge(a.id, b.id)
    reg.classify(b.id, 표시명="POSTECH")
    assert reg.display("학교", "포항공대") == "POSTECH"
    assert reg.display("학교", "포항공과대학교") == "POSTECH"
    assert len(reg.list_all("학교")) == 1


def test_merge_rejects_different_kinds(reg):
    a = reg.observe("학회", "ICML")
    b = reg.observe("학교", "ICML")
    with pytest.raises(ValueError):
        reg.merge(a.id, b.id)


def test_unknown_name_falls_back_to_raw(reg):
    assert reg.display("학교", "듣도보도못한대학교") == "듣도보도못한대학교"


def test_only_graded_kinds_get_tier(reg):
    assert reg.observe("학회", "ICML").등급 == "미분류"
    assert reg.observe("학교", "서울대학교").등급 == ""
    assert set(GRADED_KINDS) <= set(KINDS)


# --- 표 즉시 반영 (핵심) ----------------------------------------------------
def _rec() -> CVRecord:
    return CVRecord(
        지원자_ID="T",
        박사_학교="포항공과대학교(POSTECH)",
        박사_전공="전기공학",
        논문=[
            Paper(제출처="International Conference on Machine Learning", 연도="2024",
                  유형="학회", 국내해외="해외"),
            Paper(제출처="NeurIPS", 연도="2023", 유형="학회", 국내해외="해외"),
        ],
    )


def test_editing_registry_updates_existing_rows(reg):
    """관리화면에서 고치면 **이미 등록된 지원자 표**가 바로 바뀌어야 한다."""
    rec = _rec()
    observe_record(rec, reg)
    assert rec.to_row(reg)["박사_학교"] == "포항공과대학교(POSTECH)"

    학교 = reg.lookup("학교", "포항공과대학교")
    reg.classify(학교.id, 표시명="POSTECH")
    # 레코드를 다시 저장하거나 재분석하지 않았는데도 바뀐다
    assert rec.to_row(reg)["박사_학교"] == "POSTECH"


def test_venue_display_name_updates_in_table(reg):
    rec = _rec()
    observe_record(rec, reg)
    icml = reg.lookup("학회", "International Conference on Machine Learning")
    reg.classify(icml.id, 표시명="ICML", 국내해외="해외")
    assert "ICML 2024" in rec.to_row(reg)["1저자_해외논문_제출처"]


def test_observe_does_not_mutate_the_record(reg):
    """원문 표기는 절대 바뀌면 안 된다. 그래야 나중에 재분류가 가능하다."""
    rec = _rec()
    observe_record(rec, reg)
    icml = reg.lookup("학회", "International Conference on Machine Learning")
    reg.classify(icml.id, 표시명="ICML", 등급="최우수", 국내해외="국내")

    assert rec.논문[0].제출처 == "International Conference on Machine Learning"
    assert rec.논문[0].국내해외 == "해외"      # 저장값은 그대로
    assert rec.박사_학교 == "포항공과대학교(POSTECH)"
    # 화면에서만 담당자 판별이 적용된다
    assert "ICML" not in rec.to_row(reg)["1저자_해외논문_제출처"]  # 국내로 바꿨으니 빠짐


def test_reviewer_classification_beats_llm_guess(reg):
    rec = CVRecord(지원자_ID="T", 논문=[Paper(제출처="어떤학회", 유형="학회", 국내해외="국내")])
    observe_record(rec, reg)
    v = reg.lookup("학회", "어떤학회")
    reg.classify(v.id, 등급="우수", 국내해외="해외")
    assert rec.to_row(reg)["1저자_해외논문_제출처"].startswith("어떤학회")


# --- 등급별 개수 열 ---------------------------------------------------------
def test_default_tier_columns_are_top_two(reg):
    assert reg.column_tiers() == ["최우수", "우수"]


def test_tier_columns_appear_in_table(reg):
    cols = columns(reg)
    assert "1저자_해외논문_최우수" in cols
    assert "1저자_해외논문_우수" in cols
    assert "1저자_해외논문_제외" not in cols   # 제외는 열로 내지 않는다


def test_tier_counts(reg):
    rec = _rec()
    observe_record(rec, reg)
    for 제출처, 등급 in [("International Conference on Machine Learning", "최우수"),
                       ("NeurIPS", "우수")]:
        v = reg.lookup("학회", 제출처)
        reg.classify(v.id, 등급=등급, 국내해외="해외")
    row = rec.to_row(reg)
    assert row["1저자_해외논문_최우수"] == "1"
    assert row["1저자_해외논문_우수"] == "1"


def test_toggling_tier_changes_columns(reg):
    reg.set_tier_column("일반", True)
    assert "1저자_해외논문_일반" in columns(reg)
    reg.set_tier_column("우수", False)
    assert "1저자_해외논문_우수" not in columns(reg)


def test_columns_without_registry_have_no_tier_columns():
    assert not any("최우수" in c for c in columns(None))


# --- 워크샵·세션 분리 --------------------------------------------------------
def test_session_names_stay_separate_from_main_venue():
    """워크샵 논문을 본 학회와 같은 등급으로 세면 최우수 개수가 부풀어 오른다."""
    from cvtool.names import normalize

    본학회 = normalize("ICML", "학회")
    for 같음 in ("ICML 2023", "Proc. of ICML 2023", "ICML (Oral)"):
        assert normalize(같음, "학회") == 본학회
    for 다름 in ("ICML Workshop on Federated Learning",
                 "NeurIPS Datasets and Benchmarks Track"):
        assert normalize(다름, "학회") != 본학회


def test_workshop_can_be_merged_into_main_venue(tmp_path):
    """따로 잡히더라도 담당자가 묶으면 본 학회 등급을 따라간다."""
    from cvtool.names import NameRegistry

    reg = NameRegistry(tmp_path / "n.db")
    본 = reg.observe("학회", "ICML")
    워크샵 = reg.observe("학회", "ICML Workshop on Federated Learning")
    assert 본.id != 워크샵.id

    reg.classify(본.id, 등급="최우수")
    reg.merge(워크샵.id, 본.id)
    assert reg.display("학회", "ICML Workshop on Federated Learning") == "ICML"
    assert reg.lookup("학회", "ICML Workshop on Federated Learning").등급 == "최우수"
