"""엑셀(.xlsx · .csv)로 과제 추가 · 갱신."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
import uuid

import pytest

from cvtool import projects as P
from cvtool.export import build_xlsx


def _엑셀(줄들: list[dict], 머리: list[str]) -> bytes:
    return build_xlsx(줄들, 머리)


def test_엑셀_첫줄이_항목_이름이다():
    표 = P.표_읽기("과제.xlsx", _엑셀(
        [{"과제 번호": "P-1", "과제 명": "식각 공정", "키워드": "ALE, 플라즈마", "핵심기술": "원자층 식각"},
         {"과제 번호": "P-2", "과제 명": "패키징", "키워드": "", "핵심기술": ""}],
        ["과제 번호", "과제 명", "키워드", "핵심기술"]))
    과제들, 열 = P.표를_과제로(표)
    assert 열 == ["과제번호", "과제명", "키워드", "핵심기술"]          # 띄어 쓴 이름도 알아본다
    assert [P.열역할(h) for h in 열] == ["과제번호", "과제명", "키워드", "설명에 붙음"]
    assert 과제들[1] == {"과제번호": "P-2", "과제명": "패키징"}       # 빈 칸은 안 넣는다
    p = P.to_project(과제들[0])
    assert p.키 == "P-1" and p.키워드 == ["ALE", "플라즈마"] and "핵심기술: 원자층 식각" in p.설명


def test_csv_도_읽고_과제명_열이_없으면_알려준다():
    표 = P.표_읽기("a.csv", "과제명,부서\n식각,공정팀\n".encode("cp949"))
    assert P.표를_과제로(표)[0] == [{"과제명": "식각", "부서": "공정팀"}]
    with pytest.raises(P.ProjectsError, match="과제명"):
        P.표를_과제로([["이름아님", "부서"], ["x", "y"]])
    with pytest.raises(P.ProjectsError, match="xlsx"):
        P.표_읽기("a.xls", b"x")


def test_번호가_같으면_값_있는_칸만_고치고_없으면_추가():
    기존 = P.raw_items({"P-1": {"project_name": "식각", "core_tech": "ALE", "budget": "10억"},
                      "P-2": {"project_name": "패키징"}})
    r = P.추가갱신(기존, [{"과제번호": "P-1", "core_tech": "ALE · 플라즈마"},      # 고침 (budget 은 둔다)
                       {"과제번호": "P-2", "project_name": "패키징"},          # 그대로
                       {"과제번호": "P-3", "과제명": "신소재"}])                  # 추가
    assert r.추가 == ["신소재"] and r.같음 == ["패키징"]
    assert r.갱신 == [("식각", ["핵심 기술"])]
    첫 = r.과제들[0]
    assert 첫["core_tech"] == "ALE · 플라즈마" and 첫["budget"] == "10억" and 첫["과제번호"] == "P-1"
    assert [p.키 for p in P.parse({"projects": r.과제들})] == ["P-1", "P-2", "P-3"]


def test_번호가_없으면_과제명으로_맞춘다():
    r = P.추가갱신(P.raw_items([{"과제명": "식각", "설명": "옛"}]), [{"과제명": "식각", "설명": "새"}])
    assert r.갱신 == [("식각", ["설명"])] and len(r.과제들) == 1


def test_다듬은_파일에는_고른_항목과_새_항목만():
    r = P.추가갱신(P.raw_items([{"과제번호": "P-1", "과제명": "식각"}]),
                [{"과제번호": "P-1", "budget": "5억", "산출물": "장비"}],
                남길필드={"과제명", "산출물"})
    assert r.과제들[0] == {"과제번호": "P-1", "과제명": "식각", "산출물": "장비"}


# --- 웹 -----------------------------------------------------------------------------
def _올리기(c, 경로: str, 파일명: str, 내용: bytes) -> tuple[int, str]:
    경계 = uuid.uuid4().hex
    몸 = (f"--{경계}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{파일명}\"\r\n"
         "Content-Type: application/octet-stream\r\n\r\n").encode() + 내용 + f"\r\n--{경계}--\r\n".encode()
    요청 = urllib.request.Request(c.base_url + 경로, data=몸,
                                headers={"Content-Type": f"multipart/form-data; boundary={경계}"})
    try:
        with c.opener.open(요청, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def test_웹에서_엑셀로_추가_갱신(web_client, tmp_path, monkeypatch):
    import dataclasses

    from cvtool.config import settings
    from cvtool.web import jobs
    from cvtool.web.pages import match

    c = web_client
    원본 = tmp_path / "과제.json"
    원본.write_text(json.dumps({"projects": [{"과제번호": "P-1", "과제명": "식각", "부서": "공정팀"}]},
                              ensure_ascii=False), encoding="utf-8")
    바꾼 = dataclasses.replace(settings, projects_json=str(원본))
    monkeypatch.setattr(jobs, "settings", 바꾼)
    monkeypatch.setattr(match, "settings", 바꾼)
    jobs.가져온과제파일().unlink(missing_ok=True)
    jobs.다듬은파일().unlink(missing_ok=True)

    assert "엑셀로 과제 추가·갱신" in c.get("/match")
    엑셀 = _엑셀([{"과제번호": "P-1", "과제명": "식각", "부서": "식각팀"},
                 {"과제번호": "P-2", "과제명": "패키징", "부서": ""}], ["과제번호", "과제명", "부서"])
    코드, 화면 = _올리기(c, "/match/import", "새과제.xlsx", 엑셀)
    assert 코드 == 200 and "새로 추가" in 화면 and "패키징" in 화면 and "부서" in 화면
    assert not jobs.가져온과제파일().exists()                     # 미리보기만 — 아직 안 바뀜

    rows = urllib.parse.unquote(화면.split("name='rows' value='")[1].split("'")[0])
    import html as _h
    c.post("/match/import/apply", rows=_h.unescape(rows), name="새과제.xlsx")
    assert jobs.원본과제경로() == str(jobs.가져온과제파일())
    목록, 오류 = jobs.과제목록(다시=True)
    assert not 오류 and [(p.키, p.담당) for p in 목록] == [("P-1", "식각팀"), ("P-2", "")]
    assert json.loads(원본.read_text(encoding="utf-8"))["projects"][0]["부서"] == "공정팀"   # .env 파일은 그대로

    c.post("/match/import/reset")
    assert not jobs.가져온과제파일().exists() and jobs.원본과제경로() == str(원본)


def test_웹_엑셀이_이상하면_알려준다(web_client):
    코드, 화면 = _올리기(web_client, "/match/import", "x.xls", b"abc")
    assert 코드 == 200 and "xlsx 로 다시 저장" in 화면


def test_권한이_없으면_어느_파일인지_알려준다(tmp_path, monkeypatch):
    f = tmp_path / "과제.json"
    f.write_text("[]", encoding="utf-8")

    def 막힘(*a, **k):
        raise PermissionError(13, "Permission denied", str(f))
    monkeypatch.setattr(type(f), "read_text", 막힘)
    with pytest.raises(P.ProjectsError) as e:
        P.read_json(str(f))
    assert "권한이 없습니다 (오류 13)" in str(e.value) and str(f) in str(e.value)


def test_기존_파일을_못_읽어도_엑셀_업로드는_된다(web_client, tmp_path, monkeypatch):
    """권한 문제(오류 13) 등으로 기존 과제 · 다듬은 파일을 못 읽어도 엑셀 내용으로 반영된다."""
    import dataclasses
    import html as _h

    from cvtool.config import settings
    from cvtool.web import jobs
    from cvtool.web.pages import match

    c = web_client
    원본 = tmp_path / "과제.json"
    원본.write_text('{"projects": [{"과제명": "옛 과제"}]}', encoding="utf-8")
    바꾼 = dataclasses.replace(settings, projects_json=str(원본))
    monkeypatch.setattr(jobs, "settings", 바꾼)
    monkeypatch.setattr(match, "settings", 바꾼)
    jobs.가져온과제파일().unlink(missing_ok=True)
    P.save_curated(jobs.다듬은파일(), [{"과제명": "옛 과제"}])

    진짜 = P.read_json

    def 막힘(path):
        if str(path) in (str(원본), str(jobs.다듬은파일())):
            raise P.ProjectsError(f"과제 파일을 읽을 권한이 없습니다 (오류 13) — {path}")
        return 진짜(path)
    monkeypatch.setattr(P, "read_json", 막힘)

    코드, 화면 = _올리기(c, "/match/import", "새.xlsx",
                       _엑셀([{"과제번호": "P-9", "과제명": "새 과제"}], ["과제번호", "과제명"]))
    assert 코드 == 200 and "엑셀 내용만으로 새로 만듭니다" in 화면 and "다듬은 과제 파일을 읽지 못해" in 화면
    rows = 화면.split("name='rows' value='")[1].split("'")[0]
    c.post("/match/import/apply", rows=_h.unescape(rows), name="새.xlsx")
    assert not jobs.다듬은파일().exists()
    monkeypatch.setattr(P, "read_json", 진짜)
    목록, 오류 = jobs.과제목록(다시=True)
    assert not 오류 and [p.이름 for p in 목록] == ["새 과제"]
    jobs.가져온과제파일().unlink(missing_ok=True)


def test_다른_계정이_만든_파일이면_지우고_새로_쓴다(tmp_path, monkeypatch):
    f = tmp_path / "과제_원본.json"
    f.write_text("옛것", encoding="utf-8")
    진짜, 번 = type(f).write_text, []

    def 처음엔_막힘(self, *a, **k):
        번.append(self.exists())
        if len(번) == 1:
            raise PermissionError(13, "Permission denied", str(self))
        return 진짜(self, *a, **k)
    monkeypatch.setattr(type(f), "write_text", 처음엔_막힘)
    P.save_projects(f, [{"과제명": "새"}])
    assert 번 == [True, False]                     # 지운 뒤 새로 썼다
    monkeypatch.setattr(type(f), "write_text", 진짜)
    assert [p.이름 for p in P.load(f)] == ["새"]
