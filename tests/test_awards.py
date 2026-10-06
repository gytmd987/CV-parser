"""수상실적 — LLM 이 뽑고, 상세에서 고치고, 표에는 `무슨 상, 대회명 ('yy.m)` 으로."""

from __future__ import annotations

import html

import pytest

from cvtool.edit import ValidationError, validate_award
from cvtool.extract import _ALL_HINT, _RESEARCH_HINT, _assemble
from cvtool.schemas import COLUMNS, SECTION_RESEARCH, Award, CVRecord


@pytest.mark.parametrize("상, 기대", [
    (Award(상명="최우수 논문상", 수여처="대한기계학회", 연월="202305"), "최우수 논문상, 대한기계학회 ('23.5)"),
    (Award(상명="금상", 수여처="캡스톤 경진대회", 연월="202100"), "금상, 캡스톤 경진대회 ('21)"),
    (Award(상명="장학금"), "장학금"),
    (Award(상명="Best Paper Award", 연월="201911"), "Best Paper Award ('19.11)"),
])
def test_한_줄_모양(상, 기대):
    assert 상.한줄() == 기대


def test_표에는_최근_것부터():
    rec = CVRecord(수상=[Award(상명="가", 연월="201905"), Award(상명="나"),
                       Award(상명="다", 수여처="재단", 연월="202301")])
    assert rec.to_row()["수상실적"] == "다, 재단 ('23.1) | 가 ('19.5) | 나"
    assert "수상실적" in COLUMNS


def test_뽑기_스키마와_안내문():
    assert "수상" in SECTION_RESEARCH["required"]
    assert "[수상]" in _RESEARCH_HINT and "수여처" in _RESEARCH_HINT
    _RESEARCH_HINT.format(이름="x")                     # 중괄호가 안 깨졌다
    assert "수상" in _ALL_HINT


def test_뽑은_수상을_다듬는다():
    rec = _assemble({"research": {"연구분야_키워드": ["광학"], "수상": [
        {"상명": " 최우수상 ", "수여처": "학회", "연월": "2023년 5월"},
        {"상명": "최우수상", "수여처": "학회", "연월": "2023.05"},     # 같은 상
        {"상명": "", "수여처": "빈 이름"},                            # 버린다
        {"상명": "장려상", "연월": "2021"},
    ]}}, [], 지원자_ID="CV-1", 원본_파일명="")
    assert [(a.상명, a.수여처, a.연월) for a in rec.수상] == [
        ("최우수상", "학회", "202305"), ("장려상", "", "202100")]


def test_한_줄_검사():
    assert validate_award({"상명": ""}) is None
    assert validate_award({"상명": "금상", "연월": "2022.7"}).연월 == "202207"
    with pytest.raises(ValidationError):
        validate_award({"상명": "금상", "연월": "언젠가"})


def test_상세에서_고치면_표에_나온다(web_client):
    m = web_client.module
    m.store.save(CVRecord(지원자_ID="AW1", 한글_이름="수상자"))
    쪽 = web_client.get("/candidate?id=AW1")
    assert "수상 목록 저장" in 쪽 and "awardform" in 쪽
    web_client.post("/candidate/awards", id="AW1", 끝="1",
                    수상상명_1="최우수 논문상", 수상수여처_1="대한기계학회", 수상연월_1="2023.05")
    assert m.store.get("AW1").수상[0].연월 == "202305"
    assert html.escape("최우수 논문상, 대한기계학회 ('23.5)") in web_client.get("/candidate?id=AW1")
    web_client.post("/candidate/awards", id="AW1", 끝="2", 수상del_1="1")
    assert m.store.get("AW1").수상 == []
