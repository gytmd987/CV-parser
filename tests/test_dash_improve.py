"""대시보드 개선 — 프로필 속도·접기, 편집 화면 결과, 전체 거르개, 누구인지 보기,
프로필 = 수식, 블록 설명, 미리보기, 휴지통·만든 사람만 삭제·변경 이력."""

from __future__ import annotations

import json
import re

import pytest

from cvtool import profile_form as P
from cvtool.schemas import CVRecord


@pytest.fixture(scope="module")
def 판(web_client):
    """지원자 넷 · 부서 둘 · 채용 셋."""
    web = web_client
    m = web.module
    for i, (이름, 소속) in enumerate([("가", "KAIST"), ("나", "POSTECH"),
                                      ("다", "KAIST"), ("라", "서울대")]):
        m.store.save(CVRecord(지원자_ID=f"D{i}", 한글_이름=이름, 현재_소속=소속,
                              박사_학교=소속, 박사_시작="202003", 박사_졸업="202402",
                              저널_수=str(i)))
    공정 = m.auth.add_department("공정")
    소재 = m.auth.add_department("소재")
    for cid, 부서 in (("D0", 공정), ("D1", 공정), ("D2", 소재)):
        m.recruit.start(cid, "admin")
        m.recruit.set_assignment(cid, 부서, None, "admin")
    return web


def _대시(web, *블록) -> int:
    m = web.module
    did = m.boards.add(f"테스트{len(m.boards.all()) + len(m.boards.trash())}", "admin")
    for 종류, 제목, 설정 in 블록:
        m.boards.add_block(did, 종류, 제목=제목, 설정=설정)
    return did


# --- 1·2. 프로필: 같은 값, 최대, 접기 --------------------------------------------
def test_프로필은_만든_줄을_쓰고_처음_몇_명만_펼친다(판):
    did = _대시(판, ("프로필", "사람", {"대상": "=LIST(지원자)", "머리": "=한글_이름",
                                   "줄": [["소속", "=현재_소속"]], "펼침": 2}))
    쪽 = 판.get(f"/dash/view?id={did}")
    assert 쪽.count("<details class='pcard' open>") == 2
    assert 쪽.count("<details class='pcard'>") == 2
    assert "모두 펼치기" in 쪽 and "pcardFind" in 쪽


def test_프로필_최대(판):
    did = _대시(판, ("프로필", "사람", {"대상": "=LIST(지원자)", "머리": "=한글_이름",
                                   "줄": [["소속", "=현재_소속"]], "최대": 3}))
    쪽 = 판.get(f"/dash/view?id={did}")
    assert "3명" in 쪽 and "전체 4명" in 쪽
    assert 쪽.count("class='pcard'") == 3


def test_프로필_기본틀은_수식이고_예전과_같게_나온다(판):
    m = 판.module
    assert all(식.startswith("=") for _라벨, 식 in m.기본_프로필틀)
    값들 = {c: "" for c in m.대시보드_열()}
    값들.update({"박사_학교": "서울대", "박사_전공": "기계", "박사_시작": "202203",
               "박사_졸업": "202608", "박사_학위상태": "재학"})
    줄 = dict(P.render_rows([tuple(x) for x in m.기본_프로필틀], 값들))
    assert 줄 == {"학력": "서울대 기계('22.3~'26.8) 재학"}       # 빈 줄은 사라진다


# --- 3. 편집 화면 결과 · 저장 없이 미리보기 --------------------------------------
def test_편집_화면에_블록_결과가_붙는다(판):
    did = _대시(판, ("숫자", "채용 중", {"수식": "=COUNT(채용)", "형식": "명"}))
    쪽 = 판.get(f"/dash/edit?id={did}")
    assert "이렇게 보입니다" in 쪽 and "3명" in 쪽
    assert "href='/dash/who" not in 쪽           # 편집 화면에서는 누르는 곳이 없다
    assert "어떤 블록을 고를까요?" in 쪽          # 블록 설명


def test_저장_없이_미리보기는_저장하지_않는다(판):
    m = 판.module
    did = _대시(판, ("숫자", "n", {"수식": "=COUNT(채용)"}))
    bid = m.boards.blocks(did)[0].id
    code, 몸 = 판.post("/dash/block/preview", id=bid, title="n",
                      formula="=COUNT(지원자)/2", format="그대로")
    j = json.loads(몸)
    assert code == 200 and ">2<" in j["html"].replace(".0<", "<")
    assert m.boards.block(bid).수식 == "=COUNT(채용)"
    code, 몸 = 판.post("/dash/block/preview", id=bid, title="n", formula="=COUNT(없는대상)")
    assert json.loads(몸)["error"]


def test_미리보기는_섞은_식도_계산하고_채용담당자도_쓴다(판):
    m = 판.module
    m.auth.create_user("hr1", "담당", "pw1234", "채용담당자")
    hr = 판.new().login("hr1", "pw1234")
    j = json.loads(hr.get("/dash/preview?kind=agg&line="
                          + "%3DCOUNT(%EC%A7%80%EC%9B%90%EC%9E%90)%2F2"))
    assert j["error"] == "" and j["text"].startswith("2")


# --- 4. 전체 거르개 ------------------------------------------------------------
def test_거르개가_모든_블록에_걸리고_축도_좁힌다(판):
    did = _대시(판, ("숫자", "채용 중", {"수식": "=COUNT(채용)", "형식": "명"}),
                ("축표", "부서별", {"행축": "부서", "열축": "직접 입력", "열": ["인원"],
                                 "칸수식": '=COUNT(채용, 부서="{행}")'}))
    전체 = 판.get(f"/dash/view?id={did}")
    줄머리 = "<th style='text-align:left'>소재</th>"
    assert "3명" in 전체 and 줄머리 in 전체
    공정 = 판.get(f"/dash/view?id={did}&dept=%EA%B3%B5%EC%A0%95")
    assert "2명" in 공정 and 줄머리 not in 공정 and "거름: 부서=공정" in 공정


# --- 5. 누구인지 보기 -----------------------------------------------------------
def test_숫자를_누르면_누구인지(판):
    did = _대시(판, ("숫자", "채용 중", {"수식": "=COUNT(채용)", "형식": "명"}),
                ("숫자", "섞은 식", {"수식": "=COUNT(채용)/2"}))
    쪽 = 판.get(f"/dash/view?id={did}")
    주소들 = re.findall(r"href='(/dash/who\?[^']+)'", 쪽)
    assert len(주소들) == 1                         # 섞은 식은 누를 수 없다
    명단 = 판.get(주소들[0].replace("&amp;", "&"))
    assert "3명" in 명단 and "/candidate?id=D0" in 명단 and "D3" not in 명단


def test_축표_칸을_누르면_그_칸의_사람(판):
    did = _대시(판, ("축표", "부서별", {"행축": "부서", "열축": "직접 입력", "열": ["인원"],
                                    "칸수식": '=COUNT(채용, 부서="{행}")'}))
    m = 판.module
    bid = m.boards.blocks(did)[0].id
    명단 = 판.get(f"/dash/who?id={bid}&r=%EC%86%8C%EC%9E%AC&c=%EC%9D%B8%EC%9B%90")
    assert "1명" in 명단 and "D2" in 명단


# --- 6. 보는 사람에게 필요 없는 것 -------------------------------------------------
def test_숫자_블록은_수식을_글로_안_보인다(판):
    did = _대시(판, ("숫자", "n", {"수식": "=COUNT(채용)"}))
    쪽 = 판.get(f"/dash/view?id={did}")
    assert "<div class='muted'>=COUNT(채용)</div>" not in 쪽


def test_축에_값이_없으면_까닭을_말한다(판):
    did = _대시(판, ("축표", "과제별", {"행축": "과제", "열축": "직접 입력", "열": ["n"],
                                    "칸수식": '=COUNT(채용, 과제="{행}")'}))
    쪽 = 판.get(f"/dash/view?id={did}")
    assert "«과제» 에 값이 아직 없습니다" in 쪽 and "href='/org'" in 쪽


# --- 7. 휴지통 · 만든 사람만 삭제 · 변경 이력 ----------------------------------------
def test_남의_대시보드는_못_지우고_지운_것은_되살린다(판):
    m = 판.module
    m.auth.create_user("hr2", "담당2", "pw1234", "채용담당자")
    hr = 판.new().login("hr2", "pw1234")
    did = _대시(판, ("숫자", "n", {"수식": "=COUNT(채용)"}))
    이름 = m.boards.get(did).이름
    code, 쪽 = hr.post("/dash/delete", id=did)
    assert "만든 사람" in 쪽 and m.boards.get(did) is not None

    판.post("/dash/delete", id=did)
    assert m.boards.get(did) is None and [d.id for d in m.boards.trash()] == [did]
    m.boards.add(이름, "admin")                      # 같은 이름으로 새로 만들 수 있다
    판.post("/dash/restore", id=did)
    되산 = m.boards.get(did)
    assert 되산 is not None and 되산.이름 == f"{이름} (되살림)"
    assert len(m.boards.blocks(did)) == 1           # 블록도 그대로 돌아온다

    판.post("/dash/delete", id=did)
    판.post("/dash/purge", id=did)
    assert m.boards.get(did, 지운것도=True) is None


def test_블록을_지우면_되살릴_수_있다(판):
    m = 판.module
    did = _대시(판, ("숫자", "a", {"수식": "=COUNT(채용)"}), ("글", "b", {"글": "x"}))
    a = m.boards.blocks(did)[0]
    판.post("/dash/block/delete", id=a.id)
    assert [b.제목 for b in m.boards.blocks(did)] == ["b"]
    assert "지운 블록" in 판.get(f"/dash/edit?id={did}")
    판.post("/dash/block/restore", id=a.id)
    assert [b.제목 for b in m.boards.blocks(did)] == ["b", "a"]


def test_블록을_고치면_무엇이_바뀌었는지_남는다(판):
    m = 판.module
    did = _대시(판, ("숫자", "n", {"수식": "=COUNT(채용)"}))
    bid = m.boards.blocks(did)[0].id
    판.post("/dash/block/save", id=bid, title="n", formula="=COUNT(지원자)", format="그대로")
    기록 = [e for e in m.audit.recent(20, 대상종류="대시보드") if e.항목 == f"숫자 블록 #{bid}"]
    assert 기록 and "=COUNT(채용) → =COUNT(지원자)" in 기록[0].summary()
    assert json.loads(기록[0].이전값)["설정"]["수식"] == "=COUNT(채용)"
