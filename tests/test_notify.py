"""새 지원자 알림 메일 — 마지막 등록 10분 뒤 한 통 · 새 사람 없으면 안 보냄 · 매칭 3순위."""

from __future__ import annotations

import sys
from datetime import datetime

import pytest

from cvtool.schemas import CVRecord
from cvtool.timeutil import KST


def _n():
    return sys.modules["cvtool.web.pages.notify"]


def _때(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


@pytest.fixture
def 보낸것(web_client, monkeypatch):
    n = _n()
    n._파일().unlink(missing_ok=True)
    받음 = []

    class 결과:
        보냄 = True

    def 가짜(받는사람, 제목, 본문, **kw):
        받음.append((받는사람, kw.get("참조"), 제목, 본문))
        return 결과()
    monkeypatch.setattr(n.mailapi, "send", 가짜)
    yield 받음
    n._파일().unlink(missing_ok=True)


def _켜기(n, 기준="2026-12-01 00:00:00", **값):
    설 = n.설정()
    설.update({"켜짐": True, "받는사람": "a@corp.com, b@corp.com", "마지막기준": 기준,
              "시스템주소": "http://10.0.0.3:8600", **값})
    n._저장(설)


def _지원자(web_client, cid, 이름, 때):
    store = web_client.module.store
    rec = CVRecord(지원자_ID=cid, 한글_이름=이름, 현재_소속="KAIST")
    store.save(rec)
    store._conn.execute("UPDATE candidates SET 등록일시=? WHERE 지원자_ID=?", (때, cid))
    store._conn.commit()


def test_마지막_등록_10분_뒤에_한_통(web_client, 보낸것):
    n = _n()
    _켜기(n, 기준="2026-12-02 08:00:00")
    assert n.한번_보기(_때("2026-12-02 10:00")) == ""            # 새 사람 없음 — 안 보냄
    _지원자(web_client, "CV-DB00001", "첫째", "2026-12-02 10:00:00")
    _지원자(web_client, "CV-DB00002", "둘째", "2026-12-02 10:06:00")   # 올리는 중
    assert n.한번_보기(_때("2026-12-02 10:12")) == ""            # 마지막 등록 뒤 6분 — 기다린다
    assert n.한번_보기(_때("2026-12-02 10:16")).startswith("2명 알림을 보냈습니다")
    assert len(보낸것) == 1 and "첫째" in 보낸것[0][3] and "둘째" in 보낸것[0][3]
    assert n.한번_보기(_때("2026-12-02 10:30")) == ""            # 이미 보낸 사람은 다시 안 보낸다
    assert len(보낸것) == 1


def test_보내지_못하면_30분_뒤에_다시(web_client, 보낸것, monkeypatch):
    n = _n()
    _켜기(n, 기준="2026-12-03 08:00:00")
    _지원자(web_client, "CV-DB00003", "셋째", "2026-12-03 09:00:00")

    def 실패(*a, **k):
        raise n.mailapi.MailError("연결 안 됨")
    monkeypatch.setattr(n.mailapi, "send", 실패)
    assert "30분 뒤 다시" in n.한번_보기(_때("2026-12-03 09:20"))
    assert n.한번_보기(_때("2026-12-03 09:30")) == ""            # 바로 다시 두드리지 않는다
    assert n.한번_보기(_때("2026-12-03 09:51")) != ""


def test_새로_등록된_사람만_모아서_한_통(web_client, 보낸것):
    n = _n()
    m = web_client.module
    _켜기(n, 기준="2026-12-01 09:00:00")
    _지원자(web_client, "CV-NT00001", "옛사람", "2026-12-01 08:00:00")    # 기준 전 — 안 실림
    _지원자(web_client, "CV-NT00002", "새사람", "2026-12-01 15:00:00")
    m.store.save_matches("CV-NT00002", [
        __import__("cvtool.matching", fromlist=["Match"]).Match(과제키=f"P{i}", 과제명=f"과제{i}", 점수=95 - i * 10, 사유="")
        for i in range(1, 5)])
    assert n.한번_보기(_때("2026-12-02 09:01")).startswith("1명 알림을 보냈습니다")
    (받는사람, 참조, 제목, 본문), = 보낸것
    assert 받는사람 == "a@corp.com" and 참조 == ["b@corp.com"]
    assert "새 지원자 1명" in 제목 and "새사람" in 본문 and "옛사람" not in 본문
    assert "1. 과제1" in 본문 and "3. 과제3" in 본문 and "과제4" not in 본문     # 3순위까지
    assert "http://10.0.0.3:8600/candidate?id=CV-NT00002" in 본문
    assert n.한번_보기(_때("2026-12-02 10:00")) == ""
    assert len(보낸것) == 1
    assert not m.mailing.history("CV-NT00002")                   # 지원자 메일 이력에는 안 남긴다


def test_꺼져_있거나_받는_사람이_없으면_안_보낸다(web_client, 보낸것):
    n = _n()
    _지원자(web_client, "CV-DB00009", "넷째", "2026-12-04 09:00:00")
    assert n.한번_보기(_때("2026-12-04 10:00")) == ""             # 꺼짐
    _켜기(n, 기준="2026-12-04 08:00:00", 받는사람="")
    assert "받는 사람이 없어" in n.한번_보기(_때("2026-12-04 10:00"))
    assert 보낸것 == []


def test_관리자_화면(web_client, 보낸것):
    c = web_client
    n = _n()
    assert "새 지원자 알림 메일" in c.get("/admin/notify")
    c.post("/admin/notify/toggle")
    assert n.설정()["켜짐"] and n.설정()["마지막기준"]
    _코드, 화면 = c.post("/admin/notify/save", 받는사람="잘못된주소", 시스템주소="", 제목="t", 본문="b", 대기분="10")
    assert "메일 주소가 아닙니다" in 화면
    _코드, 화면 = c.post("/admin/notify/save", 받는사람="hr@corp.com", 시스템주소="", 제목="t", 본문="b", 대기분="0")
    assert "1~1440" in 화면
    c.post("/admin/notify/save", 받는사람="hr@corp.com", 시스템주소="http://x:1", 제목="제목 {건수}",
           본문="{지원자표}", 대기분="15")
    assert n.설정()["받는사람"] == "hr@corp.com" and n.대기분(n.설정()) == 15
    c.post("/admin/notify/reset")
    assert n.설정()["제목"] == n.기본제목


def test_메일_탭과_같은_발송_길을_쓴다():
    """서버마다 고친 mail_local.py 가 있으면 그걸 써야 한다 — 알림 메일만 기본 mail.py 를 써서
    «메일 API 에 연결하지 못했습니다» 가 났었다. 화면 코드는 mailer 로만 보낸다."""
    import pathlib
    import re

    assert _n().mailapi.__name__ == "cvtool.clients.mailer"
    뿌리 = pathlib.Path(__file__).resolve().parent.parent / "cvtool"
    직접 = [str(p.relative_to(뿌리)) for p in 뿌리.rglob("*.py")
          if p.name not in ("mailer.py", "mail.py")
          and re.search(r"clients import mail\b(?!er)|clients\.mail import|clients import \(?[^)\n]*\bmail\b(?!er)",
                        p.read_text(encoding="utf-8"))]
    assert 직접 == []
