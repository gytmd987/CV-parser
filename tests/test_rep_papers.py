"""대표 논문 — 상세에서 논문 · 특허는 보기만, 대표 논문만 고른다."""

from __future__ import annotations

import pytest

from cvtool.names import NameRegistry
from cvtool.schemas import CVRecord, Paper, Patent, REP_VENUE_COLUMN


@pytest.fixture
def reg(tmp_path):
    r = NameRegistry(tmp_path / "n.db")
    r.우수_저장("CVPR", "최우수")
    return r


def _사람(**kw):
    return CVRecord(지원자_ID="T", 논문=[
        Paper(제출처="CVPR", 연도="2024", 유형="학회", 대표=True),             # 최우수 — 대표 열에서 빠짐
        Paper(제출처="Some Workshop", 연도="2023", 유형="학회", 대표=True),
        Paper(제출처="Old Journal", 연도="2021", 유형="저널", 대표=True),
        Paper(제출처="Co Conf", 연도="2025", 유형="학회", 저자구분="공저자", 대표=True),  # 공저자 — 빠짐
        Paper(제출처="Not Rep", 연도="2025", 유형="학회"),
    ], **kw)


def test_대표_제출처는_최우수_우수_공저자를_빼고_최근부터(reg):
    for p in _사람().논문:
        reg.observe("학회", p.제출처)
    assert _사람().대표_제출처(reg) == "Some Workshop ('23) 1저자, Old Journal ('21) 1저자"
    row = _사람().to_row(reg)
    assert row[REP_VENUE_COLUMN] == "Some Workshop ('23) 1저자, Old Journal ('21) 1저자"
    assert "CVPR" in row["1저자_최우수_제출처"]


def test_대표_열은_최우수_열_바로_뒤에(reg):
    from cvtool.schemas import columns
    열 = columns(reg)
    assert 열.index(REP_VENUE_COLUMN) == 열.index("1저자_우수_제출처") + 1


# --- 웹 -------------------------------------------------------------------------------
@pytest.fixture
def 사람(web_client):
    m = web_client.module
    m.registry.우수_저장("NeurIPS", "최우수")
    rec = CVRecord(지원자_ID="CV-REP0001", 한글_이름="대표시험", 논문=[
        Paper(제출처="NeurIPS", 연도="2024", 유형="학회"),
        Paper(제출처="KCC", 연도="2023", 유형="학회"),
        Paper(제출처="Coauthored", 연도="2022", 유형="학회", 저자구분="공저자"),
    ], 특허=[Patent(제목="식각 장치", 상태="등록", 국내해외="국내")])
    for p in rec.논문:
        m.registry.observe("학회", p.제출처)
    m.store.save(rec)
    return "CV-REP0001"


def test_상세에서는_논문_특허를_못_고치고_대표만_고른다(web_client, 사람):
    c = web_client
    화면 = c.get(f"/candidate?id={사람}")
    assert "paperform" not in 화면 and "patentform" not in 화면 and "식각 장치" in 화면
    assert "name='대표' value='2'" in 화면                  # KCC 만 고를 수 있다
    assert "name='대표' value='1'" not in 화면 and "name='대표' value='3'" not in 화면
    c.post("/candidate/rep-papers", id=사람, 대표=["1", "2", "3"])  # 못 고르는 것은 무시
    rec = c.module.store.get(사람)
    assert [p.대표 for p in rec.논문] == [False, True, False]
    assert rec.to_row(c.module.registry)[REP_VENUE_COLUMN] == "KCC ('23) 1저자"
    assert any(r.항목 == "대표 논문" for r in c.module.audit.recent(10))
    c.post("/candidate/rep-papers", id=사람)                       # 다 풀기
    assert not any(p.대표 for p in c.module.store.get(사람).논문)


def test_재분석해도_대표_논문은_이어받는다(web_client, 사람):
    import sys

    jobs = sys.modules["cvtool.web.jobs"]
    m = web_client.module
    rec = m.store.get(사람)
    rec.논문[1].대표 = True
    m.store.save(rec)
    새것 = CVRecord(지원자_ID=사람, 한글_이름="대표시험", 논문=[
        Paper(제출처="KCC", 연도="2023", 유형="학회"), Paper(제출처="New Conf", 연도="2025", 유형="학회")])
    jobs.등록마무리(새것, "원문", "a.txt", 사람, "")
    assert [p.대표 for p in m.store.get(사람).논문] == [True, False]
