"""표 항목 수식 · TEXT 숫자 서식 · 보유기술.

열 수식의 약속: **사람이 칸에 적은 값이 있으면 그 값, 비어 있으면 수식 값.**
"""

from __future__ import annotations

import json
import re

import pytest

from cvtool import colformula as C
from cvtool.expr import text_format
from cvtool.schemas import CVRecord


# --- TEXT 숫자 서식 ---------------------------------------------------------------
@pytest.mark.parametrize("값, 서식, 기대", [
    (1234.5, "#,##0.0", "1,234.5"),
    (1234567, "#,##0", "1,234,567"),
    (0.355, "0.0%", "35.5%"),
    (0.35, "0%", "35%"),
    (42, "00000", "00042"),
    (3.14159, "0.00", "3.14"),
    (2.5, "0", "3"),                       # 엑셀처럼 0.5 는 올린다
    (1.005, "0.00", "1.01"),
    (-2.5, "0", "-3"),
    (0.5, "#.0", ".5"),
    (1.5, "0.##", "1.5"),
    (1234567, "#,##0,", "1,235"),          # 뒤 쉼표는 1000 으로 나누기
    (1234567, '0.0,,"백만"', "1.2백만"),
    (12345, "0.00E+00", "1.23E+04"),
    (0.00012, "0.0E+00", "1.2E-04"),
    (-5, "0;(0)", "(5)"),
    (0, '0;-0;"없음"', "없음"),
    (1234567, "000-0000", "123-4567"),
    (3, '0"명"', "3명"),
    (3, "#,##0명", "3명"),
    ("1,234", "#,##0.00", "1,234.00"),
    ("abc", "0", "abc"),                   # 숫자가 아니면 그대로
    ("abc", '0;0;0;"["@"]"', "[abc]"),
    ("", "0", ""),
    (12.0, "General", "12"),
    (202602, "'yy.m", "'26.2"),            # 날짜 서식은 예전 그대로
    (202602, "yyyy년 m월", "2026년 2월"),
])
def test_TEXT_숫자_서식(값, 서식, 기대):
    assert text_format(값, 서식) == 기대


# --- 열 수식 (순수) ---------------------------------------------------------------
채용중식 = '=IF(과제<>"","Y","N")'


def test_빈칸은_수식_적은값은_그대로():
    행 = {"과제": "A", "채용중": ""}
    assert C.채우기(행, {"채용중": 채용중식}, {}) == {"채용중"}
    assert 행["채용중"] == "Y"
    행 = {"과제": "A", "채용중": "보류"}
    assert C.채우기(행, {"채용중": 채용중식}, {"채용중": "보류"}) == set()
    assert 행["채용중"] == "보류"


def test_수식_열끼리_차례대로():
    행 = {"과제": ""}
    C.채우기(행, {"표시": '=채용중&"!"', "채용중": 채용중식}, {})
    assert 행["표시"] == "N!"


def test_틀린_수식은_오류값():
    행 = {"과제": "A"}
    C.채우기(행, {"x": "=VALUE(과제)"}, {})
    assert 행["x"] == C.오류값


def test_저장_전_검사():
    with pytest.raises(ValueError, match="자기 자신"):
        C.검사("a", "=a", {"b"}, {})
    with pytest.raises(ValueError, match="모르는 열"):
        C.검사("a", "=없는열", {"b"}, {})
    with pytest.raises(ValueError, match="서로를"):
        C.검사("a", "=b", {"x"}, {"b": "=a"})
    C.검사("a", 채용중식, {"과제"}, {})


# --- 보유기술 ---------------------------------------------------------------------
def test_보유기술은_열로_나오고_10개까지():
    from cvtool.extract import _assemble
    from cvtool.schemas import COLUMNS, SECTION_RESEARCH

    assert "보유기술" in COLUMNS and "보유기술" in SECTION_RESEARCH["required"]
    기술 = [f"기술{i}" for i in range(14)] + ["기술1", "연구"]
    rec = _assemble({"research": {"연구분야_키워드": ["광학"], "보유기술": 기술}}, [],
                    지원자_ID="CV-1", 원본_파일명="")
    assert rec.보유기술.split(" | ") == [f"기술{i}" for i in range(10)]
    assert rec.to_row()["보유기술"] == rec.보유기술


def test_보유기술_안내문():
    from cvtool.extract import _ALL_HINT, _RESEARCH_HINT

    assert "[보유기술]" in _RESEARCH_HINT and "최대 10개" in _RESEARCH_HINT
    _RESEARCH_HINT.format(이름="x")         # 중괄호가 안 깨졌다
    assert "보유기술" in _ALL_HINT


# --- 웹 -----------------------------------------------------------------------------
@pytest.fixture(scope="module")
def 판(web_client):
    m = web_client.module
    for i, 이름 in enumerate("가나"):
        m.store.save(CVRecord(지원자_ID=f"F{i}", 한글_이름=이름))
    부서 = m.auth.add_department("수식부")
    과제 = m.auth.add_project(부서, "수식과제")
    m.recruit.start("F0", "admin")
    m.recruit.set_assignment("F0", 부서, 과제, "admin")
    code, _ = web_client.post("/fields/add", name="채용중", scope="지원자 정보",
                              type="텍스트", choices="", formula='IF(과제<>"","Y","N")')
    assert code == 200
    return web_client


def test_수식은_저장되고_표에_보인다(판):
    m = 판.module
    assert m.store.field("채용중")["수식"] == 채용중식        # = 를 붙여 저장
    쪽 = 판.get("/")
    칸 = re.findall(r"<td class='edit[^']*fxcol[^']*'[^>]*data-id='(F\d)'[^>]*>([^<]*)</td>", 쪽)
    assert ("F0", "Y") in 칸 and ("F1", "N") in 칸


def test_손으로_적으면_그_값_지우면_다시_수식(판):
    m = 판.module
    code, 몸 = 판.post("/api/cell", id="F1", 항목="채용중", 새값="보류", 이전값="", scope="사용자")
    j = json.loads(몸)
    assert code == 200 and j["표시"] == "보류" and not j["수식"]
    assert m._표값맵()["F1"].get("채용중", "") in ("", "보류")
    assert "보류" in 판.get("/")
    code, 몸 = 판.post("/api/cell", id="F1", 항목="채용중", 새값="", 이전값="보류", scope="사용자")
    j = json.loads(몸)
    assert j["표시"] == "N" and j["수식"]


def test_대시보드에서도_수식_값(판):
    m = 판.module
    줄 = {r["지원자_ID"]: r for r in m.대시보드_행().지원자}
    assert 줄["F0"]["채용중"] == "Y" and 줄["F1"]["채용중"] == "N"


def test_틀린_수식은_저장을_막는다(판):
    m = 판.module
    판.post("/fields/formula", col="채용중", formula="=없는열+1")
    assert m.store.field("채용중")["수식"] == 채용중식
    판.post("/fields/formula", col="채용중", formula="")
    assert m.store.field("채용중")["수식"] == ""
    판.post("/fields/formula", col="채용중", formula=채용중식)
    assert m.store.field("채용중")["수식"] == 채용중식


def test_다른_수식이_쓰는_열은_이름을_못_바꾼다(판):
    m = 판.module
    m.store.add_field("표시", 수식='=채용중&"!"')
    with pytest.raises(ValueError, match="표시"):
        m.store.update_field("채용중", 새이름="채용")
    m.store.delete_field("표시")
