"""논문 손질과 심사중 논문.

LLM 이 논문을 틀리는 방식은 여러 가지다 — 심사중 논문을 실적에 섞고, 저자구분을
잘못 보고, 논문을 빠뜨리거나 없는 것을 지어낸다. 예전에는 고치는 길이 «재분석»
뿐이었는데, 그건 그 사람의 손질을 통째로 날리는 것이라 고치는 수단이 못 된다.
"""

from __future__ import annotations

import pytest

from cvtool.edit import ValidationError, validate_paper
from cvtool.names import NameRegistry
from cvtool.schemas import CVRecord, Paper


@pytest.fixture
def reg(tmp_path):
    r = NameRegistry(tmp_path / "n.db")
    for 이름, 등급 in (("CVPR", "최우수"), ("Nano Energy", "최우수")):
        나 = r.observe("학회" if 이름 == "CVPR" else "저널", 이름)
        r.classify(나.id, 등급=등급, 국내해외="해외",
                   유형="학회" if 이름 == "CVPR" else "저널",
                   IF="" if 이름 == "CVPR" else "17.9")
    return r


def _사람() -> CVRecord:
    return CVRecord(지원자_ID="T", 논문=[
        Paper(제출처="CVPR", 연도="2024", 유형="학회", 국내해외="해외",
              저자구분="주저자"),
        Paper(제출처="Nano Energy", 연도="2023", 유형="저널", 국내해외="해외",
              저자구분="주저자", 게재상태="심사중"),
    ])


# --- 심사중은 실적에서 빠진다 -------------------------------------------------
def test_a_paper_under_review_is_left_out_of_every_count(reg):
    rec = _사람()
    센것 = rec.논문_수(reg)
    assert 센것["학회_수"] == 1
    assert 센것["저널_수"] == 0                       # 심사중이던 그 저널
    assert "Nano Energy" not in rec.해외논문_제출처(reg)
    assert "Nano Energy" not in rec.등급별_제출처(reg).get("최우수", "")
    assert rec.등급별_해외논문_수(reg).get("최우수") == 1
    assert rec.최고_임팩트팩터(reg) == ""             # IF 는 그 저널에만 있었다


def test_but_it_still_shows_in_the_detail_list(reg):
    """보여주는 것과 세는 것은 다른 일이다. 뭘 냈는지도 정보다."""
    rec = _사람()
    보기 = rec.papers_view(reg)
    assert len(보기) == 2
    assert [v["게재상태"] for v in 보기] == ["게재", "심사중"]
    assert len(rec.실적논문(reg)) == 1


def test_flipping_it_to_published_brings_it_back(reg):
    rec = _사람()
    rec.논문[1].게재상태 = "게재"
    assert rec.논문_수(reg)["저널_수"] == 1
    assert rec.최고_임팩트팩터(reg) == "17.9"


# --- 기본값이 «게재» 여야 한다 (가장 중요한 회귀) -------------------------------
def test_a_paper_with_no_status_counts_as_published():
    """이력서 대부분은 그냥 `CVPR 2024` 라고만 적고 상태를 안 밝힌다."""
    assert Paper(제출처="CVPR").게재상태 == "게재"


def test_an_old_record_without_the_field_still_counts(reg):
    """«게재상태» 가 없던 시절의 레코드. 숫자가 흔들리면 안 된다."""
    rec = CVRecord.model_validate({
        "지원자_ID": "T",
        "논문": [{"제출처": "CVPR", "연도": "2024", "유형": "학회",
                "국내해외": "해외", "저자구분": "주저자"}],
    })
    assert rec.논문[0].게재상태 == "게재"
    assert rec.논문_수(reg)["학회_수"] == 1


# --- 한 줄 검사 --------------------------------------------------------------
def test_a_row_with_no_venue_is_dropped():
    """화면 맨 아래 **추가용 빈 줄**이 그대로 저장되면 안 된다."""
    assert validate_paper({"제출처": "", "연도": "2024"}) is None
    assert validate_paper({"제출처": "   "}) is None


def test_a_row_keeps_the_defaults_when_boxes_are_empty():
    p = validate_paper({"제출처": "CVPR"})
    assert (p.게재상태, p.저자구분, p.국내해외) == ("게재", "주저자", "불명")


@pytest.mark.parametrize("값들,말", [
    ({"제출처": "X", "연도": "24"}, "4자리"),
    ({"제출처": "X", "연도": "이천이십사"}, "4자리"),
    ({"제출처": "X", "게재상태": "출원"}, "게재상태"),
    ({"제출처": "X", "유형": "포스터"}, "유형"),
    ({"제출처": "X", "저자구분": "제1저자"}, "저자구분"),
])
def test_a_bad_value_is_refused(값들, 말):
    with pytest.raises(ValidationError) as exc:
        validate_paper(값들)
    assert 말 in str(exc.value)


def test_an_empty_year_is_fine():
    assert validate_paper({"제출처": "X", "연도": ""}).연도 == ""
