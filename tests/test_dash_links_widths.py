"""대시보드 — 시트 칸 누르면 누구인지 · 사람 이름 링크 · 미리보기에서 열 너비 끌기."""

from __future__ import annotations

import json
import re
import urllib.parse

import pytest

from cvtool.dashboards import 시트_다듬기
from cvtool.schemas import CVRecord


@pytest.fixture(scope="module")
def 판(web_client):
    """지원자 넷(가·나·다·라) · 공정 둘(가·나) · 소재 하나(다)."""
    m = web_client.module
    for i, 이름 in enumerate("가나다라"):
        m.store.save(CVRecord(지원자_ID=f"L{i}", 한글_이름=이름, 저널_수=str(i)))
    공정 = m.auth.add_department("공정L")
    소재 = m.auth.add_department("소재L")
    for cid, 부서 in (("L0", 공정), ("L1", 공정), ("L2", 소재)):
        m.recruit.start(cid, "admin")
        m.recruit.set_assignment(cid, 부서, None, "admin")
    return web_client


def _대시(판, 종류, 설정, 제목="블록"):
    m = 판.module
    did = m.boards.add(f"링크{len(m.boards.all())}", "admin")
    bid = m.boards.add_block(did, 종류, 제목=제목, 설정=설정)
    return did, bid


def _링크들(쪽: str, 종류: str = "drill") -> list[tuple[str, str]]:
    return [(h.replace("&amp;", "&"), t) for h, t in
            re.findall(rf"<a class='{종류}' href='([^']+)'[^>]*>([^<]*)</a>", 쪽)]


# --- 시트 칸도 누르면 누구인지 -------------------------------------------------------
def test_시트의_세는_칸은_누를_수_있다(판):
    칸 = {"A1": {"글": "공정L"},
         "B1": {"글": "=COUNTIFS(부서,A1)"},                  # 엑셀 모양 + 칸 참조
         "B2": {"글": '=COUNT(채용, 부서="공정L")'},           # 예전 모양
         "B3": {"글": '=COUNTIFS(부서,"공정L")+1'},            # 섞은 식 — 못 누른다
         "B4": {"글": "=SUM(1,2)"}}                           # 사람을 안 센다
    did, bid = _대시(판, "시트", 시트_다듬기({"행수": 5, "열수": 3, "칸": 칸}))
    쪽 = 판.get(f"/dash/view?id={did}")
    링크 = _링크들(쪽)
    assert [t for _h, t in 링크] == ["2", "2"]
    for 주소, _t in 링크:
        명단 = 판.get(주소)
        assert "2명" in 명단 and "L0" in 명단 and "L1" in 명단 and "L2" not in 명단


def test_보일_범위로_잘라도_원래_칸을_가리킨다(판):
    칸 = {"A1": {"글": "공정L"}, "C3": {"글": "=COUNTIFS(부서,A1)"}}
    did, _ = _대시(판, "시트", 시트_다듬기({"행수": 5, "열수": 4, "칸": 칸,
                                       "보일범위": "C3:D4"}))
    링크 = _링크들(판.get(f"/dash/view?id={did}"))
    assert len(링크) == 1 and "cell=C3" in 링크[0][0]
    assert "2명" in 판.get(링크[0][0])


def test_거르개가_시트_명단에도_걸린다(판):
    칸 = {"A1": {"글": "=COUNTIFS(채용중,\"Y\")"}}
    did, _ = _대시(판, "시트", 시트_다듬기({"행수": 2, "열수": 2, "칸": 칸}))
    쪽 = 판.get(f"/dash/view?id={did}&dept=" + urllib.parse.quote("소재L"))
    (주소, 수), = _링크들(쪽)
    assert 수 == "1" and "dept=" in 주소
    assert "L2" in 판.get(주소) and "L0" not in 판.get(주소)


# --- 사람 이름은 상세로 ----------------------------------------------------------------
def test_시트와_숫자의_이름에_링크(판):
    칸 = {"A1": {"글": "가"}, "A2": {"글": '=INDEX(한글_이름,MATCH("나",한글_이름,0))'},
         "A3": {"글": "아무개"}}
    did, _ = _대시(판, "시트", 시트_다듬기({"행수": 3, "열수": 1, "칸": 칸}))
    링크 = _링크들(판.get(f"/dash/view?id={did}"), "person")
    assert ("/candidate?id=L0", "가") in 링크 and ("/candidate?id=L1", "나") in 링크
    assert all(t != "아무개" for _h, t in 링크)
    did, _ = _대시(판, "숫자", {"수식": '=LIST(채용, 부서="공정L")'})
    링크 = _링크들(판.get(f"/dash/view?id={did}"), "person")
    assert [t for _h, t in 링크] == ["가", "나"]


def test_목록과_프로필의_이름에_링크(판):
    did, _ = _대시(판, "목록", {"목록대상": "채용",
                              "목록열": [["이름", "=한글_이름", ""], ["저널", "=저널_수", ""]]})
    링크 = _링크들(판.get(f"/dash/view?id={did}"), "person")
    assert sorted(링크) == [("/candidate?id=L0", "가"), ("/candidate?id=L1", "나"),
                           ("/candidate?id=L2", "다")]
    did, _ = _대시(판, "프로필", {"대상": "=LIST(채용)", "머리": "=한글_이름",
                               "줄": [["저널", "=저널_수"]]})
    쪽 = 판.get(f"/dash/view?id={did}")
    assert "href='/candidate?id=L0'" in 쪽 and "상세 ↗" in 쪽


def test_편집_미리보기에는_링크가_없다(판):
    did, _ = _대시(판, "목록", {"목록대상": "채용", "목록열": [["이름", "=한글_이름", ""]]})
    assert "class='person'" not in 판.get(f"/dash/edit?id={did}")


# --- 미리보기에서 열 너비 끌기 -------------------------------------------------------------
def test_목록_열_너비를_저장한다(판):
    m = 판.module
    did, bid = _대시(판, "목록", {"목록대상": "채용",
                                "목록열": [["이름", "=한글_이름", ""], ["빈", "", ""],
                                         ["저널", "=저널_수", "80"]]})
    편집 = 판.get(f"/dash/edit?id={did}")
    assert f"data-wblock='{bid}'" in 편집 and "col_resize" not in 편집  # 스크립트는 본문으로
    assert re.search(r"data-wkey='2'>[^<]*저널", 편집)        # 빈 줄을 건너뛴 원래 번호
    code, 몸 = 판.post("/dash/block/widths", id=str(bid), key=["0", "2"], px=["150", "10"])
    assert code == 200 and json.loads(몸)["ok"]
    assert [w for _h, _f, w in m.boards.block(bid).목록열] == ["150", "", "30"]


def test_축표_열_너비를_저장한다(판):
    m = 판.module
    did, bid = _대시(판, "축표", {"행축": "부서", "열축": "직접 입력", "열": ["인원"],
                                "칸수식": '=COUNT(채용, 부서="{행}")',
                                "열너비": {"인원": "70"}})
    판.post("/dash/block/widths", id=str(bid), key=["", "인원"], px=["120", ""])
    assert m.boards.block(bid).열너비 == {"": "120"}
