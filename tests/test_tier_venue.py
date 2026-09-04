"""등급별 제출처 열 — 1저자 논문을 어디에 냈는지 한 줄로."""

from __future__ import annotations

import pytest

from cvtool.names import NameRegistry
from cvtool.schemas import (
    CVRecord,
    Paper,
    columns,
    is_tier_venue,
    tier_venue_column,
)

최우수 = tier_venue_column("최우수")
우수 = tier_venue_column("우수")


@pytest.fixture
def reg(tmp_path):
    r = NameRegistry(tmp_path / "n.db")
    for 이름, 종류, 등급, IF in (
        ("CVPR", "학회", "최우수", ""),
        ("AAAI", "학회", "최우수", ""),
        ("Nano Energy", "저널", "최우수", "17.9"),
        ("ICML", "학회", "우수", ""),
        ("무명학회", "학회", "미분류", ""),
    ):
        n = r.observe(종류, 이름)
        r.classify(n.id, 등급=등급, 유형=종류, IF=IF, 국내해외="해외")
    return r


def _사람(*논문):
    return CVRecord(지원자_ID="T", 논문=list(논문))


def test_이름은_학회_연도_1저자_꼴이다(reg):
    rec = _사람(Paper(제출처="CVPR", 연도="2024", 유형="학회"))
    assert rec.등급별_제출처(reg)["최우수"] == "CVPR ('24) 1저자"


def test_저널은_IF_를_같이_적는다(reg):
    rec = _사람(Paper(제출처="Nano Energy", 연도="2023", 유형="저널"))
    assert rec.등급별_제출처(reg)["최우수"] == "Nano Energy ('23, IF 17.9) 1저자"


def test_여러_개면_최근_것부터_쉼표로(reg):
    rec = _사람(
        Paper(제출처="AAAI", 연도="2023", 유형="학회"),
        Paper(제출처="CVPR", 연도="2024", 유형="학회"),
        Paper(제출처="Nano Energy", 연도="2022", 유형="저널"),
    )
    assert rec.등급별_제출처(reg)["최우수"] == (
        "CVPR ('24) 1저자, AAAI ('23) 1저자, Nano Energy ('22, IF 17.9) 1저자")


def test_등급마다_따로_모인다(reg):
    rec = _사람(Paper(제출처="CVPR", 연도="2024", 유형="학회"),
              Paper(제출처="ICML", 연도="2022", 유형="학회"))
    것 = rec.등급별_제출처(reg)
    assert 것["최우수"] == "CVPR ('24) 1저자"
    assert 것["우수"] == "ICML ('22) 1저자"


def test_공저자_논문은_안_들어간다(reg):
    rec = _사람(Paper(제출처="CVPR", 연도="2024", 유형="학회", 저자구분="공저자"))
    assert rec.등급별_제출처(reg) == {}


def test_등급을_안_매긴_것은_안_들어간다(reg):
    rec = _사람(Paper(제출처="무명학회", 연도="2024", 유형="학회"))
    assert "미분류" in rec.등급별_제출처(reg)      # 미분류도 등급이긴 하다
    # 다만 표에 나가는 열은 담당자가 켠 등급뿐이다
    assert tier_venue_column("미분류") not in columns(reg)


def test_연도가_없으면_괄호를_안_붙인다(reg):
    rec = _사람(Paper(제출처="CVPR", 연도="", 유형="학회"))
    assert rec.등급별_제출처(reg)["최우수"] == "CVPR 1저자"


def test_연도가_없는_것은_맨_뒤로(reg):
    rec = _사람(Paper(제출처="CVPR", 연도="", 유형="학회"),
              Paper(제출처="AAAI", 연도="2023", 유형="학회"))
    assert rec.등급별_제출처(reg)["최우수"] == "AAAI ('23) 1저자, CVPR 1저자"


def test_IF_를_안_넣었으면_연도만(reg):
    n = reg.lookup("저널", "Nano Energy")
    reg.classify(n.id, 등급="최우수", 유형="저널", IF="", 국내해외="해외")
    rec = _사람(Paper(제출처="Nano Energy", 연도="2023", 유형="저널"))
    assert rec.등급별_제출처(reg)["최우수"] == "Nano Energy ('23) 1저자"


def test_이름과_등급과_IF_는_볼_때마다_사전에서_읽는다(reg):
    """명칭 관리에서 고치면 이미 등록된 표에도 곧바로 반영돼야 한다."""
    rec = _사람(Paper(제출처="Nano Energy", 연도="2023", 유형="저널"))
    n = reg.lookup("저널", "Nano Energy")
    reg.classify(n.id, 표시명="Nano En.", 등급="우수", 유형="저널", IF="20", 국내해외="해외")
    것 = rec.등급별_제출처(reg)
    assert "최우수" not in 것
    assert 것["우수"] == "Nano En. ('23, IF 20) 1저자"


# --- 표에 붙기 ----------------------------------------------------------------
def test_켠_등급마다_열이_생긴다(reg):
    이름들 = columns(reg)
    assert 최우수 in 이름들 and 우수 in 이름들
    # 개수 열 바로 뒤에 온다 (숫자 옆에 어디에 냈는지)
    assert 이름들.index("1저자_해외논문_우수") < 이름들.index(최우수)


def test_to_row_에_값이_담긴다(reg):
    rec = _사람(Paper(제출처="CVPR", 연도="2024", 유형="학회"),
              Paper(제출처="ICML", 연도="2022", 유형="학회"))
    행 = rec.to_row(reg)
    assert 행[최우수] == "CVPR ('24) 1저자"
    assert 행[우수] == "ICML ('22) 1저자"


def test_논문이_없으면_빈칸(reg):
    assert _사람().to_row(reg)[최우수] == ""


def test_사전_없이_부르면_안_터진다():
    """엑셀 양식 만들기처럼 registry 없이 도는 길이 있다."""
    rec = _사람(Paper(제출처="CVPR", 연도="2024", 유형="학회"))
    assert rec.등급별_제출처(None) == {}
    assert 최우수 not in rec.to_row()


# --- 계산 결과라 못 고친다 ------------------------------------------------------
def test_계산_열로_알아본다():
    assert is_tier_venue(최우수) and is_tier_venue(우수)
    assert not is_tier_venue("1저자_해외논문_제출처")   # 옛 열은 다른 규칙이 잡는다
    assert not is_tier_venue("1저자_해외논문_최우수")
    assert not is_tier_venue("경력_요약")


def test_표에서도_상세에서도_못_고친다():
    from cvtool.web import app

    assert not app._editable(최우수)
    assert not app._긴글가능(최우수, None, "지원자 정보")


def test_엑셀_양식에는_안_들어간다(tmp_path):
    """사람이 채우는 칸이 아니다 — 논문 목록에서 계산해 나온다."""
    from cvtool import bulk
    from cvtool.store import CandidateStore

    assert 최우수 not in bulk.양식열(CandidateStore(tmp_path / "c.db"))
