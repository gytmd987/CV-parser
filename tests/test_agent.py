"""운영 설정(화면에서 켜고 끄기) · 에이전트(사내 코워크) 토큰 · MCP · 대신 판단하기."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

import pytest

from cvtool.config import settings


def _jobs():
    import sys
    return sys.modules["cvtool.web.jobs"]


@pytest.fixture(autouse=True)
def _설정_되돌리기():
    yield
    settings._덮어씀.clear()


def _mcp(c, 토큰: str, 본문, 경로: str = "/mcp") -> tuple[int, dict | list | None]:
    요청 = urllib.request.Request(
        c.base_url + 경로, data=json.dumps(본문, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {토큰}"})
    try:
        with urllib.request.urlopen(요청, timeout=20) as r:
            글 = r.read().decode()
            return r.status, (json.loads(글) if 글 else None)
    except urllib.error.HTTPError as e:
        글 = e.read().decode()
        return e.code, (json.loads(글) if 글 else None)


def _도구(c, 토큰: str, _도구이름: str, **args) -> dict:
    코드, 답 = _mcp(c, 토큰, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                            "params": {"name": _도구이름, "arguments": args}})
    assert 코드 == 200, 답
    결과 = 답["result"]
    if 결과["isError"]:
        raise AssertionError(결과["content"][0]["text"])
    return 결과["structuredContent"]


def test_운영_설정을_화면에서_바꾸면_바로_반영된다(web_client):
    c = web_client
    m = c.module
    assert "운영 설정" in c.get("/admin/settings")
    c.post("/admin/settings", 이름="mail_dry_run", 값="0")
    assert settings.mail_dry_run is False and settings.화면에서_바꿈("mail_dry_run")
    저장 = json.loads((m.DATA_DIR / "운영설정.json").read_text(encoding="utf-8"))
    assert 저장["mail_dry_run"] is False
    assert any(r.대상 == "mail_dry_run" for r in m.audit.recent(20))
    c.post("/admin/settings", 이름="llm_mode", 값="이상한값")
    assert settings.llm_mode == settings.기본값.llm_mode
    # 다시 켜도 파일에서 읽힌다
    settings._덮어씀.clear()
    settings.파일쓰기(m.DATA_DIR / "운영설정.json")
    assert settings.mail_dry_run is False


def test_토큰_없이는_MCP_가_안_열린다(web_client):
    c = web_client
    토큰 = c.module.auth.create_token("시험1", 만든이="admin")
    코드, 답 = _mcp(c, 토큰, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert 코드 == 401 and "꺼 두었습니다" in 답["error"]["message"]      # 접속 받기가 꺼져 있다
    settings.바꾸기({"agent_api": True})
    assert _mcp(c, "cvt_wrong", {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 401
    assert _mcp(c, 토큰, {"jsonrpc": "2.0", "id": 1, "method": "ping"}) == (200, {"jsonrpc": "2.0", "id": 1, "result": {}})
    tid = next(t["id"] for t in c.module.auth.tokens() if t["이름"] == "시험1")
    c.module.auth.set_token_active(tid, False)
    assert _mcp(c, 토큰, {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 401


def test_관리자_화면에서_토큰을_발급하면_한_번만_보인다(web_client):
    c = web_client
    코드, 화면 = c.post("/admin/agent/token", 이름="채용팀코워크")
    assert 코드 == 200 and "지금 한 번만 보입니다" in 화면
    토큰 = 화면.split("cvt_", 1)[1].split("<", 1)[0]
    assert c.module.auth.user_for_api_token("cvt_" + 토큰).아이디 == "agent:채용팀코워크"
    assert "cvt_" + 토큰 not in c.get("/admin/agent")
    assert "코워크용 설명서" in c.get("/admin/agent") and "cv_submit" in c.get("/admin/agent")


def test_MCP_첫인사와_도구_목록(web_client):
    c = web_client
    settings.바꾸기({"agent_api": True})
    토큰 = c.module.auth.create_token("시험2")
    코드, 답 = _mcp(c, 토큰, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "t", "version": "1"}}})
    assert 코드 == 200 and 답["result"]["protocolVersion"] == "2025-06-18"
    assert "사용 설명서" in 답["result"]["instructions"]
    assert _mcp(c, 토큰, {"jsonrpc": "2.0", "method": "notifications/initialized"}) == (202, None)
    _코드, 답 = _mcp(c, 토큰, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    이름들 = {t["name"] for t in 답["result"]["tools"]}
    assert {"cv_task", "cv_submit", "match_task", "match_submit", "candidate_get"} <= 이름들
    assert _도구(c, 토큰, "status")["AI 분석 모드"] == "서버 LLM"


_CV = """홍길동 (Gil-Dong Hong)
이메일 gildong@example.com
KAIST 신소재공학과 박사 (2015-2020), 지도교수 김교수
현재 삼성전자 반도체연구소 책임연구원
연구분야: 원자층 식각, 플라즈마
"""

_결과 = {
    "basic": {"한글_이름": "홍길동", "영문_이름": "Gil-Dong Hong", "이메일": "gildong@example.com",
              "현재_신분": "재직", "현재_소속": "삼성전자 반도체연구소"},
    "education": {"박사_학교": "KAIST", "박사_전공": "신소재공학과", "박사_시작": "2015", "박사_졸업": "2020",
                  "박사_지도교수": "김교수"},
    "research": {"연구분야_키워드": ["원자층 식각", "플라즈마"]},
    "career": {"경력": [{"회사": "삼성전자", "직책": "책임연구원", "시작": "2020", "종료": "재직중"}]},
}


def test_사내_코워크가_CV_를_분석해_등록하고_과제를_맞춘다(web_client):
    c = web_client
    m = c.module
    jobs = _jobs()
    settings.바꾸기({"agent_api": True})
    토큰 = m.auth.create_token("시험3")
    c.post("/admin/settings", 이름="llm_mode", 값="agent")
    assert settings.llm_mode == "agent"

    올림 = _도구(c, 토큰, "cv_upload", 파일명="홍길동.txt",
              내용_base64=base64.b64encode(_CV.encode()).decode())
    cid = 올림["지원자_ID"]
    assert cid in [x["지원자_ID"] for x in _도구(c, 토큰, "cv_pending")["대기"]]
    일감 = _도구(c, 토큰, "cv_task", 지원자_ID=cid)
    assert "원자층 식각" in 일감["cv_text"] and "basic" in 일감["schema"]["properties"]

    with pytest.raises(AssertionError, match="basic"):
        _도구(c, 토큰, "cv_submit", 지원자_ID=cid, result={"엉뚱": 1})
    답 = _도구(c, 토큰, "cv_submit", 지원자_ID=cid, result=_결과)
    assert 답["이름"] == "홍길동" and 답["남은 대기"] == 0
    rec = m.store.get(cid)
    assert rec.한글_이름 == "홍길동" and rec.to_row()["박사_학교"]
    assert any(r.사용자 == "agent:시험3" and r.대상 == cid for r in m.audit.recent(50))

    # 과제 매칭 — 시스템은 LLM 을 부르지 않는다
    from cvtool import projects as P
    P.save_projects(jobs.가져온과제파일(), [
        {"과제번호": "P-1", "과제명": "원자층 식각 공정", "키워드": "ALE"},
        {"과제번호": "P-2", "과제명": "차세대 패키징", "키워드": "HBM"}])
    assert jobs.매칭실행(rec)[1] == jobs.에이전트모드말
    assert cid in [x["지원자_ID"] for x in _도구(c, 토큰, "match_pending")["대기"]]
    일감 = _도구(c, 토큰, "match_task", 지원자_ID=cid)
    assert 일감["과제수"] == 2 and "P-1" in 일감["system"] + 일감["user"]
    with pytest.raises(AssertionError, match="모르는 등급"):
        _도구(c, 토큰, "match_submit", 지원자_ID=cid, result={"결과": [{"과제키": "P-1", "등급": "최고"}]})
    답 = _도구(c, 토큰, "match_submit", 지원자_ID=cid, result={"결과": [
        {"과제키": "P-2", "등급": "접점 없음", "사유": "패키징 경험 없음", "근거": []},
        {"과제키": "P-1", "등급": "매우 적합", "사유": "원자층 식각 박사", "근거": ["연구분야: 원자층 식각"]}]})
    assert 답["상위"][0] == {"과제": "원자층 식각 공정", "등급": "매우 적합"}
    assert m.store.matches(cid)[0]["과제키"] == "P-1"
    assert cid not in [x["지원자_ID"] for x in _도구(c, 토큰, "match_pending")["대기"]]

    # 조회 · 수정
    assert _도구(c, 토큰, "candidates_search", 검색어="원자층")["지원자"][0]["지원자_ID"] == cid
    상세 = _도구(c, 토큰, "candidate_get", 지원자_ID=cid)
    assert 상세["과제매칭"][0]["등급"] == "매우 적합"
    with pytest.raises(AssertionError):
        _도구(c, 토큰, "candidate_update", 지원자_ID=cid, 항목="이메일", 새값="x@y.com", 이전값="틀린값")
    _도구(c, 토큰, "candidate_update", 지원자_ID=cid, 항목="이메일", 새값="new@example.com",
         이전값=상세["지원자"]["이메일"])
    assert m.store.get(cid).이메일 == "new@example.com"
    jobs.가져온과제파일().unlink(missing_ok=True)


def test_서버_LLM_으로_되돌리면_기다리던_CV_를_이어서_분석한다(web_client, monkeypatch):
    c = web_client
    m = c.module
    jobs = _jobs()
    settings.바꾸기({"llm_mode": "agent"})
    넣은것 = []
    monkeypatch.setattr(jobs._jobs, "put", lambda x: 넣은것.append(x))
    cid = "CV-TEST0001"
    저장명 = m.store.store_file(cid, "a.txt", b"x")
    jobs._enqueue("a.txt", cid, 저장명)
    assert 넣은것 == [] and jobs._status["a.txt"]["state"] == "코워크 대기"
    c.post("/admin/settings", 이름="llm_mode", 값="server")
    assert [x[1] for x in 넣은것] == [cid]
    m.store.finish_job(cid)


def test_JSON_API_로도_같은_도구(web_client):
    c = web_client
    settings.바꾸기({"agent_api": True})
    토큰 = c.module.auth.create_token("시험4")
    코드, 답 = _mcp(c, 토큰, {"tool": "status", "args": {}}, 경로="/api/agent/call")
    assert 코드 == 200 and 답["ok"] and "지원자 수" in 답["result"]
    코드, 답 = _mcp(c, 토큰, {"tool": "없는것"}, 경로="/api/agent/call")
    assert 코드 == 400 and "없는 도구" in 답["error"]


def test_코워크가_검토_항목을_확인함으로(web_client):
    c = web_client
    m = c.module
    settings.바꾸기({"agent_api": True})
    토큰 = m.auth.create_token("검토시험")
    from cvtool.schemas import CVRecord
    rec = CVRecord(지원자_ID="CV-RV00001", 한글_이름="검토대상", 검토_필요="Y",
                   검토_사유="미분류 학회/저널: XYZ / 박사_학교 표기 확인 필요")
    m.store.save(rec)
    목록 = _도구(c, 토큰, "review_list")
    assert any(x["지원자_ID"] == "CV-RV00001" and x["남은_항목"] == 2 for x in 목록["지원자"])
    항목 = _도구(c, 토큰, "review_list", 지원자_ID="CV-RV00001")["항목"]
    assert [x["번호"] for x in 항목] == [1, 2] and not 항목[0]["확인함"]
    답 = _도구(c, 토큰, "review_mark", 지원자_ID="CV-RV00001", 번호들=[1])
    assert 답["남은_항목"] == 1 and 답["검토_필요"]
    with pytest.raises(AssertionError, match="없는 번호"):
        _도구(c, 토큰, "review_mark", 지원자_ID="CV-RV00001", 번호들=[9])
    답 = _도구(c, 토큰, "review_mark", 지원자_ID="CV-RV00001", 전부=True)
    assert 답["남은_항목"] == 0 and not 답["검토_필요"]
    assert m.store.get("CV-RV00001").검토_필요 == ""
    assert any(r.사용자 == "agent:검토시험" and r.항목 == "검토" for r in m.audit.recent(20))
    _도구(c, 토큰, "review_mark", 지원자_ID="CV-RV00001", 번호들=[2], 확인=False)
    assert m.store.get("CV-RV00001").검토_필요 == "Y"


def test_코워크가_명칭을_관리한다(web_client):
    c = web_client
    m = c.module
    reg = m.registry
    settings.바꾸기({"agent_api": True})
    토큰 = m.auth.create_token("명칭시험")
    a = reg.observe("소속", "포항공대")
    b = reg.observe("소속", "POSTECH")
    목록 = _도구(c, 토큰, "names_list", 종류="소속", 안본것만=True)
    assert {x["이름"] for x in 목록["이름"]} >= {"포항공대", "POSTECH"}
    _도구(c, 토큰, "names_rename", 종류="소속", 이름="포항공대", 새이름="POSTECH")
    assert reg.get(a.id).표시명 == "POSTECH" and reg.get(a.id).확인
    _도구(c, 토큰, "names_move", 종류="소속", 표기_id들=[b.id], 새이름="포스텍")
    assert reg.get(b.id).표시명 == "포스텍" and reg.get(a.id).표시명 == "POSTECH"

    v = reg.observe("학회", "Weird Conf")
    with pytest.raises(AssertionError, match="리스트"):
        _도구(c, 토큰, "names_classify", 종류="학회·저널", 이름="Weird Conf", 등급="최우수")
    답 = _도구(c, 토큰, "names_classify", 종류="학회·저널", 이름="Weird Conf", 등급="제외", 국내해외="해외", IF="3.1")
    assert 답["분류"]["등급"] == "제외" and reg.get(v.id).IF == "3.1" and reg.get(v.id).확인
    w = reg.observe("학회", "Another Conf")
    assert _도구(c, 토큰, "names_confirm", 종류="학회·저널", 표기_id들=[w.id])["확인"] == 1
    assert any(r.사용자 == "agent:명칭시험" and r.대상종류 == "명칭" for r in m.audit.recent(30))


def test_코워크가_엑셀로_과제를_올린다(web_client):
    import base64 as _b

    from cvtool.export import build_xlsx

    c = web_client
    m = c.module
    jobs = _jobs()
    settings.바꾸기({"agent_api": True})
    토큰 = m.auth.create_token("과제시험")
    jobs.가져온과제파일().unlink(missing_ok=True)
    엑셀 = build_xlsx([{"과제번호": "P-7", "과제 명": "원자층 식각", "키워드": "ALE"}], ["과제번호", "과제 명", "키워드"])
    미리 = _도구(c, 토큰, "projects_import", 파일명="과제.xlsx", 내용_base64=_b.b64encode(엑셀).decode())
    assert 미리["미리보기"] and 미리["추가"] == ["원자층 식각"]
    assert {"열": "과제명", "쓰임": "과제명"} in 미리["열"]
    assert not jobs.가져온과제파일().exists()                     # 미리보기는 저장 안 함
    답 = _도구(c, 토큰, "projects_import", 파일명="과제.xlsx", 내용_base64=_b.b64encode(엑셀).decode(), 반영=True)
    assert 답["추가"] == 1 and 답["전체"] >= 1
    답 = _도구(c, 토큰, "projects_import", 항목=[{"과제번호": "P-7", "부서": "공정팀"},
                                               {"과제번호": "P-8", "과제명": "패키징"}], 반영=True)
    assert 답["고침"] == 1 and 답["추가"] == 1
    목록 = {p["과제키"]: p for p in _도구(c, 토큰, "projects_list")["과제"]}
    assert 목록["P-7"]["담당"] == "공정팀" and 목록["P-7"]["과제명"] == "원자층 식각"
    with pytest.raises(AssertionError, match="과제명"):
        _도구(c, 토큰, "projects_import", 항목=[{"엉뚱": "x"}])
    assert any(r.사용자 == "agent:과제시험" and r.항목 == "엑셀로 과제 추가·갱신" for r in m.audit.recent(20))
    jobs.가져온과제파일().unlink(missing_ok=True)
