"""경력의 회사 이름도 명칭 사전을 거친다.

「소속」사전은 *"학교와 회사를 함께 담는다"* 고 처음부터 적혀 있는데
(names.py 맨 앞) 정작 경력 열이 그걸 안 쓰고 있었다.
"""

from __future__ import annotations

import pytest

from cvtool.names import NameRegistry, observe_record
from cvtool.schemas import NAME_COLUMNS, Career, CVRecord, 경력_요약_만들기


@pytest.fixture
def reg(tmp_path):
    return NameRegistry(tmp_path / "n.db")


def _사람() -> CVRecord:
    경력 = [
        Career(회사="(주)가나다소프트", 직무="연구원", 시작="202103", 종료="202406"),
        Career(회사="한국대학교", 직무="박사후연구원", 시작="202405", 종료="재직중"),
    ]
    return CVRecord(지원자_ID="T", 경력=경력, 경력_회사="한국대학교",
                    직책="박사후연구원", 경력_요약=경력_요약_만들기(경력))


def test_the_career_company_uses_the_affiliation_dictionary():
    assert NAME_COLUMNS["경력_회사"] == "소속"


def test_renaming_a_company_changes_both_career_columns(reg):
    """열 하나만 바뀌고 요약은 옛 이름이면 표가 앞뒤가 안 맞는다."""
    rec = _사람()
    observe_record(rec, reg)
    reg.classify(reg.lookup("소속", "한국대학교").id, 표시명="한국대")

    행 = rec.to_row(reg)
    assert 행["경력_회사"] == "한국대"
    assert "한국대/박사후연구원(202405-재직중)" in 행["경력_요약"]
    assert "한국대학교" not in 행["경력_요약"]
    assert rec.경력_회사 == "한국대학교"          # 저장값은 그대로


def test_every_company_in_the_list_gets_registered(reg):
    """열로 뽑히는 것은 대표 경력 하나뿐이지만 요약에는 전부 나온다."""
    observe_record(_사람(), reg)
    assert reg.lookup("소속", "(주)가나다소프트") is not None
    assert reg.lookup("소속", "한국대학교") is not None


def test_a_summary_the_recruiter_rewrote_is_left_alone(reg):
    """사람이 손으로 쓴 글을 우리가 다시 만들어 덮으면 안 된다."""
    rec = _사람()
    observe_record(rec, reg)
    reg.classify(reg.lookup("소속", "한국대학교").id, 표시명="한국대")
    rec.경력_요약 = "포닥 2년차. 반도체 공정 쪽."

    assert rec.to_row(reg)["경력_요약"] == "포닥 2년차. 반도체 공정 쪽."


def test_an_old_record_without_the_list_keeps_its_summary(reg):
    """경력 목록이 없던 시절의 레코드. 저장된 글자가 전부다."""
    rec = CVRecord(지원자_ID="T", 경력_회사="한국대학교",
                   경력_요약="한국대학교/박사후연구원(202405-재직중)")
    observe_record(rec, reg)
    reg.classify(reg.lookup("소속", "한국대학교").id, 표시명="한국대")

    행 = rec.to_row(reg)
    assert 행["경력_회사"] == "한국대"           # 열은 사전을 거치고
    assert 행["경력_요약"] == "한국대학교/박사후연구원(202405-재직중)"   # 요약은 그대로


def test_only_the_career_company_column_becomes_a_review_reason(reg):
    """경력 목록까지 사유로 올리면 사람마다 서너 줄씩 붙어 안 읽힌다."""
    사유 = [s for s in observe_record(_사람(), reg) if "사전에 없는" in s]
    assert len(사유) == 1
    assert "경력_회사: 한국대학교" in 사유[0]
    assert "가나다소프트" not in 사유[0]


def test_a_company_the_dictionary_knows_is_not_flagged(reg):
    reg.classify(reg.observe("소속", "한국대학교").id, 표시명="한국대")
    사유 = [s for s in observe_record(_사람(), reg) if "경력_회사" in s]
    assert 사유 == []


# --- 이관 -------------------------------------------------------------------
def test_the_backfill_registers_companies_from_old_records(reg):
    """옛 레코드의 회사가 사전에 없으면 상세를 저장할 때 이름이 지워진다."""
    옛것 = [CVRecord(지원자_ID="A", 경력_회사="한국대학교"),
           CVRecord(지원자_ID="B", 경력_회사="(주)가나다소프트"),
           CVRecord(지원자_ID="C", 경력_회사="")]

    assert reg.backfill_careers(lambda: 옛것) == 2
    assert reg.lookup("소속", "한국대학교") is not None
    assert reg.lookup("소속", "(주)가나다소프트") is not None


def test_the_backfill_runs_only_once(reg):
    불린횟수 = []

    def 주기():
        불린횟수.append(1)
        return [CVRecord(지원자_ID="A", 경력_회사="한국대학교")]

    assert reg.backfill_careers(주기) == 1
    assert reg.backfill_careers(주기) == 0
    # 두 번째는 **레코드를 읽지도 않는다** — 서버가 뜰 때마다 전체를 훑으면 안 된다
    assert len(불린횟수) == 1


def test_the_backfill_does_not_touch_a_name_already_there(reg):
    """이미 이름을 붙여 둔 것을 이관이 되돌리면 안 된다."""
    reg.classify(reg.observe("소속", "한국대학교").id, 표시명="한국대")
    reg.backfill_careers(lambda: [CVRecord(지원자_ID="A", 경력_회사="한국대학교")])
    assert reg.display("소속", "한국대학교") == "한국대"


def test_the_summary_builder_is_shared_with_extraction():
    """추출과 표시가 다른 함수를 쓰면 «사람이 고쳤나» 를 가릴 수 없다."""
    from cvtool import extract

    assert extract.경력_요약_만들기 is 경력_요약_만들기
