"""오늘 할 일 · 과제로 사람 찾기 · 빈 항목 채우기 · 새 블록 종류."""

from __future__ import annotations

import re
import urllib.parse

import pytest

from cvtool.matching import Match
from cvtool.schemas import Award, CVRecord


@pytest.fixture(scope="module")
def 판(web_client):
    m = web_client.module
    m.store.save(CVRecord(지원자_ID="T0", 한글_이름="검토씨", 검토_필요="Y",
                          검토_사유="박사 학교를 확신하지 못함"))
    m.store.save(CVRecord(지원자_ID="T1", 한글_이름="멈춘씨", 보유기술="리소그래피, Python"))
    m.store.save(CVRecord(지원자_ID="T2", 한글_이름="새사람", 보유기술="식각, python"))
    m.recruit.start("T1", "admin")
    m.recruit.set_stage("T1", "서류 검토", "합격", "admin")
    # 20일 전에 움직였다고 적어 둔다
    m.recruit._conn.execute("UPDATE stages SET 갱신일시='2000-01-01 00:00:00'"
                            " WHERE 지원자_ID='T1'")
    m.recruit._conn.execute("UPDATE recruit SET 채용시작일시='2000-01-01 00:00:00'"
                            " WHERE 지원자_ID='T1'")
    m.recruit._conn.commit()
    m.mailing.add_template("서류합격안내T", "제목", "본문", 발송조건="서류 검토 합격")
    return web_client


# --- 오늘 할 일 ------------------------------------------------------------------
def test_오늘_할_일에_네_가지가_모인다(판):
    쪽 = 판.get("/todo")
    assert "오늘 할 일" in 쪽
    검토 = 쪽[쪽.index("id='review'"):쪽.index("id='mail'")]
    assert "검토씨" in 검토 and "박사 학교" in 검토
    메일 = 쪽[쪽.index("id='mail'"):쪽.index("id='stuck'")]
    assert "서류합격안내T" in 메일 and "멈춘씨" in 메일 and "/mail/log?tpl=" in 메일
    멈춤 = 쪽[쪽.index("id='stuck'"):쪽.index("id='nodept'")]
    assert "멈춘씨" in 멈춤 and "서류 검토 합격" in 멈춤
    assert "멈춘씨" in 쪽[쪽.index("id='nodept'"):]


def test_확인하면_검토에서_빠진다(판):
    m = 판.module
    m.store.mark_reviewed("T0", "박사 학교를 확신하지 못함", "admin")
    try:
        쪽 = 판.get("/todo")
        assert "검토씨" not in 쪽[쪽.index("id='review'"):쪽.index("id='mail'")]
    finally:
        m.store.unmark_reviewed("T0", "박사 학교를 확신하지 못함")


def test_탭_맨_앞에_있다(판):
    머리 = 판.get("/").split("<header>")[1].split("</header>")[0]
    assert 머리.index("/todo") < 머리.index("인재 Pool")


# --- 과제로 사람 찾기 --------------------------------------------------------------
def test_과제를_고르면_점수_순(판):
    m = 판.module
    m.store.save_matches("T2", [Match(과제키="PX", 과제명="식각과제", 점수=92, 사유="식각 경험")])
    m.store.save_matches("T1", [Match(과제키="PX", 과제명="식각과제", 점수=40, 사유="인접")])
    from cvtool.web.pages import find

    결과 = find.찾기("PX", "")
    ids = [x["rec"].지원자_ID for x in 결과]
    assert ids.index("T2") < ids.index("T1") < ids.index("T0")   # 안 맞춰 본 사람은 맨 뒤
    assert 결과[0]["매칭"]["점수"] == 92


def test_키워드는_모두_들어야(판):
    from cvtool.web.pages import find

    assert {x["rec"].지원자_ID for x in find.찾기("", "python")} == {"T1", "T2"}
    assert [x["rec"].지원자_ID for x in find.찾기("", "PYTHON 리소")] == ["T1"]
    쪽 = 판.get("/find?q=" + urllib.parse.quote("리소그래피"))
    assert "<mark>리소그래피</mark>" in 쪽 and "멈춘씨" in 쪽 and "새사람" not in 쪽
    assert "action='/candidates/start' class='selform'" in 쪽


def test_인재_Pool_에서_간다(판):
    assert "href='/find'" in 판.get("/")


# --- 빈 항목 채우기 ----------------------------------------------------------------
def test_빈_항목만_채우고_나머지는_그대로(판):
    m = 판.module
    from cvtool.web import jobs

    m.store.save(CVRecord(지원자_ID="F0", 한글_이름="채울씨", 박사_학교="손으로 고친 학교"),
                 저장_파일명="F0.pdf")
    m.store.save(CVRecord(지원자_ID="F1", 한글_이름="꽉찬씨", 보유기술="A",
                          수상=[Award(상명="상")]), 저장_파일명="F1.pdf")
    대상 = [cid for cid, *_ in m.store.fill_targets(jobs.채울항목)]
    assert "F0" in 대상 and "F1" not in 대상
    assert "빈 항목만 채우기" in 판.get("/upload")

    새것 = CVRecord(지원자_ID="F0", 한글_이름="다른이름", 박사_학교="LLM 이 읽은 학교",
                  보유기술="식각", 수상=[Award(상명="최우수상", 수여처="학회", 연월="202405")])
    jobs._빈칸채우기("F0", 새것, "원문", "F0.pdf")
    rec = m.store.get("F0")
    assert rec.보유기술 == "식각" and rec.수상[0].상명 == "최우수상"
    assert rec.박사_학교 == "손으로 고친 학교" and rec.한글_이름 == "채울씨"
    assert "F0" not in [cid for cid, *_ in m.store.fill_targets(jobs.채울항목)]


def test_채우기_단추는_대기열에_넣는다(판, monkeypatch):
    m = 판.module
    from cvtool.web.pages import candidates

    넣은것 = []
    monkeypatch.setattr(candidates, "_enqueue", lambda *a: 넣은것.append(a))
    m.store.save(CVRecord(지원자_ID="F2", 한글_이름="또채울씨"), 저장_파일명="F2.pdf")
    code, _ = 판.post("/candidates/fill")
    assert code == 200
    assert any(a[1] == "F2" and a[3] == "빈 항목 채우기" for a in 넣은것)


# --- 대시보드: 새 블록은 다섯 종류, 안내는 엑셀 모양 -------------------------------------
def test_새_블록_고르기에는_축표와_표가_없다(판):
    m = 판.module
    did = m.boards.add("새종류판", "admin")
    쪽 = 판.get(f"/dash/edit?id={did}")
    종류 = re.findall(r"<button name='kind' value='([^']+)' class='kcard'>", 쪽)
    assert 종류 == ["숫자", "목록", "시트", "프로필", "글"]
    판.post("/dash/block/add", dash=str(did), kind="축표")
    assert not [b for b in m.boards.blocks(did) if b.종류 == "축표"]


def test_예전_축표는_그대로_보인다(판):
    m = 판.module
    did = m.boards.add("옛축표판", "admin")
    m.boards.add_block(did, "축표", 제목="옛축표", 설정={
        "행축": "부서", "열축": "직접 입력", "열": ["인원"], "칸수식": "=COUNT(채용)"})
    assert "옛축표" in 판.get(f"/dash/view?id={did}")
    assert "옛축표" in 판.get(f"/dash/edit?id={did}")


def test_안내는_엑셀_모양만(판):
    m = 판.module
    did = m.boards.add("안내모양판", "admin")
    쪽 = 판.get(f"/dash/edit?id={did}")
    도움 = 쪽[쪽.index("여러 사람 세기"):]
    도움 = 도움[:도움.index("</details>")]
    assert "COUNTIFS" in 도움
    assert "COUNT(지원자" not in 도움 and "COUNT(채용" not in 도움 and "PCT(" not in 도움
    목록 = re.search(r"window\.수식목록 = (\{.*?\});</script>", 쪽).group(1)
    assert '"PCT"' not in 목록.split('"설명"')[0] and '"COUNTIFS"' in 목록


def test_새_대시보드_예시도_엑셀_모양(판):
    m = 판.module
    판.post("/dash/add", name="예시모양판", sample="1")
    d = next(d for d in m.boards.all() if d.이름 == "예시모양판")
    블록 = m.boards.blocks(d.id)
    assert [b.종류 for b in 블록] == ["숫자", "시트", "프로필"]
    assert 블록[0].수식.startswith("=COUNTIFS(")


def test_매칭은_등급만_보이고_숫자는_안_보인다():
    from cvtool.matching import 등급점수
    from cvtool.web.pages.match import _등급칸

    칸 = _등급칸({"점수": 등급점수["매우 적합"], "평가됨": True})
    assert "매우 적합" in 칸 and "95" not in 칸
    assert "미평가" in _등급칸({"점수": 0, "평가됨": False})
    assert "적합" in _등급칸({"점수": 85, "평가됨": True})          # 옛 점수도 등급으로
