"""시트·대시보드 수식을 엑셀처럼 — `COUNTIFS(부서,"abc",과제,"qw")`, 함수 안의 함수.

예전 모양(`=COUNT(지원자, 부서="A")`)도 그대로 돼야 한다 — 저장된 대시보드가 많다.
"""

from __future__ import annotations

import json
import re

import pytest

from cvtool import expr as E
from cvtool import formula as F
from cvtool import sheet as S
from cvtool.schemas import CVRecord

사람 = [
    {"지원자_ID": "1", "한글_이름": "가", "부서": "abc", "과제": "qw", "저널_수": "3",
     "최종상태": "서류 불합격", "입사월": "202609", "채용중": "Y"},
    {"지원자_ID": "2", "한글_이름": "나", "부서": "abc", "과제": "zz", "저널_수": "1",
     "최종상태": "합격", "입사월": "", "채용중": "Y"},
    {"지원자_ID": "3", "한글_이름": "다", "부서": "xyz", "과제": "qw", "저널_수": "5",
     "최종상태": "", "입사월": "202609", "채용중": ""},
]
줄 = F.Rows(지원자=사람, 채용=사람[:2])
열 = set(사람[0])


def 값(수식: str, 칸: dict | None = None):
    칸 = 칸 or {}
    return S.계산(수식, 줄, 열, (lambda a: 칸.get(a, "")))[0]


@pytest.mark.parametrize("수식, 기대", [
    ('=COUNTIFS(부서,"abc",과제,"qw")', "1"),
    ('=COUNTIF(부서,"abc")', "2"),
    ('=countifs(부서,"abc")', "2"),                          # 소문자도
    ('=COUNTIF(저널_수,">2")', "2"),
    ('=COUNTIF(저널_수,">"&2)', "2"),                         # 조건을 & 로 잇기
    ('=COUNTIF(최종상태,"*불합격")', "1"),                      # 와일드카드
    ('=COUNTIF(최종상태,"<>*불합격")', "2"),
    ('=COUNTIF(최종상태,"")', "1"),                             # 빈칸
    ('=COUNTIF(부서,"ABC")', "2"),                              # 대소문자 안 가림
    ('=SUMIFS(저널_수,부서,"abc")', "4"),
    ('=SUMIF(부서,"abc",저널_수)', "4"),
    ('=AVERAGEIFS(저널_수,과제,"qw")', "4"),
    ('=MAXIFS(저널_수,부서,"abc")', "3"),
    ('=MINIFS(저널_수,부서,"abc")', "1"),
    ('=COUNTIFS(채용중,"Y",부서,"abc")', "2"),
    ('=SUMPRODUCT((부서="abc")*(저널_수>2))', "1"),
    ('=TEXTJOIN(", ",TRUE,FILTER(한글_이름,부서="abc"))', "가, 나"),
    ('=COUNTA(UNIQUE(부서))', "2"),
])
def test_엑셀_모양(수식, 기대):
    assert 값(수식) == 기대


def test_함수_안의_함수():
    assert 값('=IF(COUNTIFS(부서,"abc",과제,"qw")>0,"있음","없음")') == "있음"
    assert 값('=ROUND(AVERAGEIFS(저널_수,부서,"abc"),1)') == "2"
    assert 값('=COUNTIFS(입사월,LEFT("2026091",6),과제,UPPER("qw"))') == "2"
    assert 값('=COUNTIFS(부서,"abc")+COUNT(지원자, 부서="xyz")') == "3"   # 새것+옛것


def test_조건에_칸을_쓴다():
    assert 값("=COUNTIFS(부서,A1)", {"A1": "abc"}) == "2"
    assert 값('=COUNTIF(저널_수,">"&A2)', {"A2": "2"}) == "2"


def test_칸_범위에도_된다():
    칸 = {"A1": "abc", "A2": "abc", "A3": "x", "B1": "1", "B2": "2", "B3": "4"}
    assert 값('=COUNTIF(A1:A3,"abc")', 칸) == "2"
    assert 값('=SUMIF(A1:A3,"abc",B1:B3)', 칸) == "3"
    assert 값("=SUM(B1:B3)", 칸) == "7"


def test_예전_모양도_된다():
    assert 값('=COUNT(지원자, 부서="abc")') == "2"
    assert 값('=COUNTIFS(지원자, 부서="abc", 과제="qw")') == "1"
    assert 값("=COUNT(채용)") == "2"


def test_열_하나만_쓰면_묶으라고_알려준다():
    with pytest.raises(E.ExprError, match="COUNTA"):
        S.계산("=부서", 줄, 열)


def test_조건줄은_센_사람():
    assert [r["지원자_ID"] for r in S.조건줄('=COUNTIFS(부서,"abc",과제,"qw")', 줄, 열)] == ["1"]
    assert [r["지원자_ID"] for r in S.조건줄('=SUMIFS(저널_수,부서,"abc")', 줄, 열)] == ["1", "2"]
    # 감싸도 센 사람은 분명하다 — 조건 집계가 하나뿐이면 그 사람들
    assert [r["지원자_ID"] for r in S.조건줄('=COUNTIFS(부서,"abc")/2', 줄, 열)] == ["1", "2"]
    assert S.조건줄('=COUNTIF(부서,"abc")/COUNTIF(부서,"xyz")', 줄, 열) is None  # 둘
    assert S.조건줄('=COUNTIF(A1:A3,"abc")', 줄, 열) is None       # 사람이 아니다


# --- 대시보드에서 숫자 누르기 ---------------------------------------------------
@pytest.fixture(scope="module")
def 판(web_client):
    m = web_client.module
    for i, 이름 in enumerate("가나다"):
        m.store.save(CVRecord(지원자_ID=f"X{i}", 한글_이름=이름, 저널_수=str(i)))
    공정 = m.auth.add_department("공정X")
    for cid in ("X0", "X1"):
        m.recruit.start(cid, "admin")
        m.recruit.set_assignment(cid, 공정, None, "admin")
    return web_client


def test_엑셀_모양_숫자도_누르면_누구인지(판):
    m = 판.module
    did = m.boards.add("엑셀판", "admin")
    m.boards.add_block(did, "숫자", 제목="공정", 설정={"수식": '=COUNTIFS(부서,"공정X",채용중,"Y")'})
    쪽 = 판.get(f"/dash/view?id={did}")
    주소들 = re.findall(r"href='(/dash/who\?[^']+)'", 쪽)
    assert len(주소들) == 1
    명단 = 판.get(주소들[0].replace("&amp;", "&"))
    assert "2명" in 명단 and "X0" in 명단 and "X1" in 명단 and "X2" not in 명단


def test_시트에_저장하고_보면_계산된다(판):
    m = 판.module
    did = m.boards.add("엑셀시트판", "admin")
    bid = m.boards.add_block(did, "시트", 제목="시트")
    모델 = {"행수": 3, "열수": 3,
          "칸": {"A1": {"글": "공정X"},
                "B1": {"글": '=IF(COUNTIFS(부서,A1,채용중,"Y")>1,"여럿","하나")'},
                "C1": {"글": '=COUNTIFS(부서,A1)'}}}
    판.post("/dash/sheet/save", id=str(bid), title="시트", sheet=json.dumps(모델))
    assert m.boards.block(bid).설정["시트칸"]["C1"]["글"] == "=COUNTIFS(부서,A1)"
    쪽 = 판.get(f"/dash/view?id={did}")
    assert "여럿" in 쪽 and ">2<" in re.sub(r"<a [^>]*>", "", 쪽).replace("</a>", "")


def test_숫자_블록_저장도_된다(판):
    m = 판.module
    did = m.boards.add("엑셀숫자판", "admin")
    bid = m.boards.add_block(did, "숫자", 제목="n", 설정={"수식": "=COUNT(지원자)"})
    판.post("/dash/block/save", id=str(bid), title="n",
            formula='=COUNTIFS(부서,"공정X")', format="그대로")
    assert m.boards.block(bid).수식 == '=COUNTIFS(부서,"공정X")'
