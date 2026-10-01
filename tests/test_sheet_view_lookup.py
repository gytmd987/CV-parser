"""시트 — 보일 범위 · 지원자 DB 에서 찾기 (INDEX·MATCH·VLOOKUP·XLOOKUP)."""

from __future__ import annotations

import re

import pytest

from cvtool import expr as E
from cvtool import sheet as S
from cvtool.dashboards import Block, render_sheet, 시트_다듬기, 시트_행열
from cvtool.formula import Rows

사람 = [
    {"지원자_ID": "1", "한글_이름": "홍길동", "박사_학교": "서울대", "저널_수": "3", "부서": "A"},
    {"지원자_ID": "2", "한글_이름": "김철수", "박사_학교": "KAIST", "저널_수": "5", "부서": "B"},
    {"지원자_ID": "3", "한글_이름": "이영희", "박사_학교": "", "저널_수": "1", "부서": "A"},
]
줄 = Rows(지원자=사람, 채용=[])
열 = set(사람[0])


def 값(수식: str, 칸: dict | None = None) -> str:
    칸 = 칸 or {}
    return S.계산(수식, 줄, 열, lambda a: 칸.get(a, ""))[0]


# --- 지원자 DB 에서 찾기 ------------------------------------------------------------
@pytest.mark.parametrize("수식, 기대", [
    ('=INDEX(박사_학교, MATCH("김철수", 한글_이름, 0))', "KAIST"),
    ('=XLOOKUP("홍길동", 한글_이름, 박사_학교)', "서울대"),
    ('=XLOOKUP("없는사람", 한글_이름, 박사_학교, "없음")', "없음"),
    ('=VLOOKUP("이영희", HSTACK(한글_이름, 박사_학교, 저널_수), 3, FALSE)', "1"),
    ('=MATCH("김*", 한글_이름, 0)', "2"),                       # 와일드카드
    ('=MATCH("KAIST", 박사_학교, 0)', "2"),
    ('=MATCH("kaist", 박사_학교, 0)', "2"),                      # 대소문자 안 가림
    ('=INDEX(한글_이름, 3)', "이영희"),
    ('=ROWS(한글_이름)', "3"),
    ('=COLUMNS(HSTACK(한글_이름, 부서))', "2"),
    ('=TEXTJOIN(",", TRUE, INDEX(HSTACK(한글_이름, 부서), 0, 2))', "A,B,A"),
    ('=XLOOKUP("김철수", 한글_이름, 저널_수) * 2', "10"),
    ('=IFERROR(XLOOKUP("없는사람", 한글_이름, 박사_학교), "-")', "-"),
])
def test_지원자_DB_에서_찾기(수식, 기대):
    assert 값(수식) == 기대


def test_찾을_값에_칸을_쓴다():
    assert 값("=XLOOKUP(A1, 한글_이름, 박사_학교)", {"A1": "김철수"}) == "KAIST"
    assert 값("=INDEX(저널_수, MATCH(A1, 한글_이름, 0)) + 1", {"A1": "홍길동"}) == "4"


def test_칸_범위에서도_예전처럼():
    칸 = {"A1": "사과", "A2": "배", "B1": "1", "B2": "2"}
    assert 값("=VLOOKUP(\"배\", A1:B2, 2, FALSE)", 칸) == "2"
    assert 값("=INDEX(A1:B2, 2, 1)", 칸) == "배"
    assert 값("=MATCH(\"배\", A1:A2, 0)", 칸) == "2"
    assert 값("=ROWS(A1:B2) * COLUMNS(A1:B2)", 칸) == "4"
    assert 값("=XLOOKUP(\"사과\", A1:A2, B1:B2)", 칸) == "1"


def test_못_찾으면_말해_준다():
    with pytest.raises(E.ExprError, match="찾는 값이 없습니다"):
        값('=XLOOKUP("없는사람", 한글_이름, 박사_학교)')


# --- 보일 범위 ---------------------------------------------------------------------
def _시트(설정: dict) -> Block:
    return Block(id=1, dashboard_id=1, 순서=0, 종류="시트", 제목="S", 설정=시트_다듬기(설정))


def test_보일_범위는_다듬어_저장된다():
    assert 시트_다듬기({"보일범위": " b2:d5 "})["보일범위"] == "B2:D5"
    assert 시트_다듬기({"보일범위": "$B$2:$D$5"})["보일범위"] == "B2:D5"
    assert 시트_다듬기({"보일범위": "아무거나"})["보일범위"] == ""


def test_보기는_범위만_계산은_전체로():
    b = _시트({"행수": 6, "열수": 5, "보일범위": "B2:C3",
              "칸": {"A1": {"글": "10"}, "B2": {"글": "=A1*2", "굵게": 1},
                    "C3": {"글": "끝"}, "E5": {"글": "밖"}}})
    전체 = render_sheet(b, 줄, 열)
    assert (전체.행수, 전체.열수) == (6, 5)
    잘림 = render_sheet(b, 줄, 열, 잘라보기=True)
    assert (잘림.행수, 잘림.열수, 잘림.시작행, 잘림.시작열) == (2, 2, 1, 1)
    칸 = {주소: v for 줄_ in 잘림.행 for 주소, v, *_ in 줄_}
    assert 칸 == {"A1": "20", "B1": "", "A2": "", "B2": "끝"}     # A1 은 범위 밖이지만 계산된다
    assert 잘림.칸서식["A1"].get("굵게")


def test_범위에_걸친_병합은_잘린다():
    b = _시트({"행수": 4, "열수": 4, "보일범위": "B1:D2",
              "칸": {"A1": {"글": "제목", "가로병합": 3}}})
    잘림 = render_sheet(b, 줄, 열, 잘라보기=True)
    첫 = 잘림.행[0][0]
    assert 첫[0] == "A1" and 첫[1] == "제목" and 첫[3] == 2      # B1:C1 만큼


def test_행을_끼우면_범위도_밀린다():
    설정 = 시트_다듬기({"행수": 6, "열수": 4, "보일범위": "A2:C4"})
    assert 시트_행열(설정, "행삽입", 0, 2)["보일범위"] == "A4:C6"
    assert 시트_행열(설정, "행삭제", 2, 1)["보일범위"] == "A2:C3"
    assert 시트_행열(설정, "열삭제", 0, 1)["보일범위"] == "A2:B4"


def test_보기_화면은_잘라서_머리글은_원래_자리(web_client):
    m = web_client.module
    did = m.boards.add("범위판", "admin")
    bid = m.boards.add_block(did, "시트", 제목="S", 설정=시트_다듬기(
        {"행수": 8, "열수": 6, "보일범위": "C3:D4",
         "칸": {"C3": {"글": "보임"}, "A1": {"글": "안보임"}}}))
    쪽 = web_client.get(f"/dash/view?id={did}")
    assert "보임" in 쪽 and "안보임" not in 쪽
    assert re.search(r"<th data-col='A'>C</th><th data-col='B'>D</th>", 쪽)
    편집 = web_client.get(f"/dash/edit?id={did}")
    assert "안보임" in 편집 and "data-sheet='보일범위'" in 편집 and "C3:D4" in 편집
    assert m.boards.block(bid).설정["보일범위"] == "C3:D4"
