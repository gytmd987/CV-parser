"""우수 학회·저널 리스트 — 최우수 · 우수는 리스트에 있는 곳만."""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request

import pytest

from cvtool.names import NameRegistry, 리스트등급오류
from cvtool.schemas import CVRecord, Paper


@pytest.fixture
def reg(tmp_path):
    return NameRegistry(tmp_path / "n.db")


def test_리스트에_있으면_표기가_달라도_그_등급(reg):
    reg.우수_저장("ICML", "최우수", 별칭="International Conference on Machine Learning")
    a = reg.observe("학회", "Proc. of ICML 2023")
    b = reg.observe("학회", "International Conference on Machine Learning")
    c = reg.observe("학회", "무명학회")
    assert reg.get(a.id).등급 == "최우수" and reg.get(b.id).등급 == "최우수"
    assert reg.get(c.id).등급 == "미분류"


def test_리스트_밖은_일반_제외만(reg):
    n = reg.observe("학회", "무명학회")
    with pytest.raises(리스트등급오류):
        reg.classify(n.id, 등급="우수")
    with pytest.raises(리스트등급오류):
        reg.set_class("학회·저널", "무명학회", 등급="최우수")
    reg.classify(n.id, 등급="제외")
    assert reg.get(n.id).등급 == "제외"


def test_리스트에서_빼면_미분류(reg):
    n = reg.observe("저널", "Nano Energy")
    x = reg.우수_저장("Nano Energy", "우수")
    assert reg.get(n.id).등급 == "우수"
    reg.우수_지우기(x["id"])
    assert reg.get(n.id).등급 == "미분류"


def test_이름에_묶인_표기가_리스트에_맞아도_된다(reg):
    """표에 보일 이름을 한글로 바꿔도, 그 이름에 묶인 원래 표기가 리스트에 맞으면 걸린다."""
    n = reg.observe("학회", "CVPR")
    reg.classify(n.id, 표시명="컴퓨터비전학회")
    reg.우수_저장("CVPR", "최우수")
    assert reg.get(n.id).등급 == "최우수"
    assert reg.우수목록()[0]["사전이름"] == ["컴퓨터비전학회"]


def test_같은_곳으로_읽히는_항목은_둘_못_넣는다(reg):
    reg.우수_저장("ICML", "최우수")
    with pytest.raises(ValueError, match="ICML"):
        reg.우수_저장("Proc. ICML", "우수")
    with pytest.raises(ValueError):
        reg.우수_저장("NeurIPS", "보통")


def test_등급별_개수_열도_리스트를_따른다(reg):
    rec = CVRecord(지원자_ID="T", 논문=[Paper(제출처="CVPR 2024", 연도="2024", 유형="학회", 국내해외="해외",
                                         주저자=True)])
    reg.observe("학회", "CVPR 2024")
    assert rec.to_row(reg).get("1저자_해외논문_최우수") in ("0", "")
    reg.우수_저장("CVPR", "최우수")
    assert rec.to_row(reg)["1저자_해외논문_최우수"] == "1"


def test_원래_최우수_우수는_처음_켤_때_리스트로_옮겨진다(tmp_path):
    path = tmp_path / "n.db"
    r = NameRegistry(path)
    n = r.observe("학회", "AAAI")
    r.close()
    # 리스트가 없던 버전에서 손으로 매긴 최우수
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE name_classes SET 등급='최우수' WHERE 표시명='AAAI'")
    conn.execute("DELETE FROM registry_meta")
    conn.commit()
    conn.close()
    r = NameRegistry(path)
    assert [x["이름"] for x in r.우수목록()] == ["AAAI"]
    assert r.get(n.id).등급 == "최우수"


# --- 웹 ------------------------------------------------------------------------------
def test_명칭_관리에서_리스트를_고친다(web_client):
    c = web_client
    reg = c.module.registry
    n = reg.observe("학회", "SIGGRAPH Asia 2024")
    화면 = c.get("/names?kind=" + urllib.parse.quote("학회·저널"))
    assert "우수 학회·저널 리스트" in 화면
    assert f"name='등급_{n.id}'" in 화면 and "<option>최우수</option>" not in 화면   # 손으로는 못 고름

    c.post("/names/excellent/save", 이름="SIGGRAPH Asia", 등급="최우수", 별칭="", 학회저널="학회", 비고="")
    assert reg.get(n.id).등급 == "최우수"
    화면 = c.get("/names?kind=" + urllib.parse.quote("학회·저널"))
    assert f"name='등급_{n.id}'" not in 화면                                     # 리스트 등급은 잠김
    assert "SIGGRAPH Asia 2024" in c.get("/names/excellent")
    x = next(x for x in reg.우수목록() if x["이름"] == "SIGGRAPH Asia")
    c.post("/names/excellent/delete", id=str(x["id"]))
    assert reg.get(n.id).등급 == "미분류"
    _코드, 화면 = c.post("/names/save", kind="학회·저널", id=str(n.id), **{f"등급_{n.id}": "우수"})
    assert "리스트" in 화면 and reg.get(n.id).등급 == "미분류"


def test_사내_코워크도_리스트를_고친다(web_client):
    from cvtool.config import settings

    c = web_client
    reg = c.module.registry
    settings.바꾸기({"agent_api": True})
    토큰 = c.module.auth.create_token("리스트시험")

    def 도구(_도구이름, **args):
        요청 = urllib.request.Request(
            c.base_url + "/api/agent/call",
            data=json.dumps({"tool": _도구이름, "args": args}, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {토큰}"})
        try:
            with urllib.request.urlopen(요청) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    try:
        n = reg.observe("저널", "Advanced Materials")
        답 = 도구("venue_list_save", 항목=[
            {"이름": "Advanced Materials", "등급": "최우수", "별칭": ["Adv. Mater."], "학회저널": "저널"},
            {"이름": "이상한", "등급": "보통"}])
        assert 답["ok"] and 답["result"]["저장"] == [{"이름": "Advanced Materials", "등급": "최우수", "처리": "넣음"}]
        assert 답["result"]["실패"][0]["이름"] == "이상한"
        assert reg.get(n.id).등급 == "최우수"
        목록 = 도구("venue_list")["result"]["리스트"]
        assert any(x["이름"] == "Advanced Materials" and x["별칭"] == ["Adv. Mater."] for x in 목록)
        이름들 = 도구("venue_names", 검색어="advanced")["result"]["학회저널"]
        assert 이름들[0]["리스트"] and 이름들[0]["등급"] == "최우수"
        assert 도구("venue_list_remove", 이름="Advanced Materials")["ok"]
        assert reg.get(n.id).등급 == "미분류"
        assert any(r.사용자 == "agent:리스트시험" and r.대상 == "우수리스트:Advanced Materials"
                   for r in c.module.audit.recent(30))
    finally:
        settings._덮어씀.clear()
