"""검증 자료 정리 — 웹 화면 (표준 라이브러리만).

  python -m verifysum            → http://127.0.0.1:8795

  VERIFYSUM_PORT       포트 (기본 8795)
  VERIFYSUM_HOST       다른 PC 에서 열려면 0.0.0.0 (그때는 비밀번호를 꼭 거세요)
  VERIFYSUM_PASSWORD   있으면 브라우저가 묻는다 (아이디는 아무거나)
  VERIFYSUM_WORKERS    동시에 LLM 에 보낼 사람 수 (기본 2)
  VERIFYSUM_DATA_DIR   학습 결과·결과 파일 둘 곳 (기본 ~/.cvtool/verifysum)

LLM 서버 설정은 지원자 관리와 같은 .env (CVTOOL_LLM_*) 를 그대로 읽는다.
올린 후보자 원본 파일은 디스크에 남기지 않는다 (결과 파일만 남는다).
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from email.parser import BytesParser
from email.policy import default as 기본정책
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import docxio as D
from . import fit, generate, learn

폴더 = Path(os.environ.get("VERIFYSUM_DATA_DIR")
          or Path(os.environ.get("CVTOOL_DATA_DIR", Path.home() / ".cvtool")) / "verifysum")
결과_1장 = "검증의견_1장_통합.docx"
결과_요약 = "개인별_검증결과_요약.docx"
결과_목록 = "결과.json"


def _클라이언트():
    """LLM 클라이언트. 지원자 관리와 같은 설정(.env)을 쓴다. 테스트가 바꿔 끼운다."""
    from cvtool.clients.llm import LLMClient

    return LLMClient()


# --- 작업 상태 -----------------------------------------------------------------------
_잠금 = threading.Lock()
작업: dict = {"돌고있음": False, "줄": [], "시작": 0.0, "끝": 0.0, "오류": ""}


def _줄바꾸기(i: int, **값) -> None:
    with _잠금:
        작업["줄"][i].update(값)


def _자연정렬(이름: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", 이름)]


def 작업시작(파일들: list[tuple[str, bytes]], 학습: dict) -> None:
    파일들 = sorted(파일들, key=lambda f: _자연정렬(f[0]))
    자료들: list = []
    with _잠금:
        작업.update(돌고있음=True, 줄=[], 시작=time.time(), 끝=0.0, 오류="")
        for 이름, 내용 in 파일들:
            try:
                자료 = D.검증자료_읽기(내용, 이름)
                작업["줄"].append({"파일": 이름, "후보자": 자료.제목, "상태": "대기", "메모": ""})
            except D.형식오류 as exc:
                자료 = None
                작업["줄"].append({"파일": 이름, "후보자": "", "상태": "실패", "메모": str(exc)})
            자료들.append(자료)
    threading.Thread(target=_돌리기, args=(자료들, 학습), daemon=True).start()


def _돌리기(자료들: list, 학습: dict) -> None:
    결과들: list = [None] * len(자료들)
    로컬 = threading.local()

    def 하나(i: int) -> None:
        자료 = 자료들[i]
        if 자료 is None:
            return
        if not hasattr(로컬, "c"):
            로컬.c = _클라이언트()
        _줄바꾸기(i, 상태="처리중")
        try:
            r = generate.한사람(자료, 학습, 로컬.c)
            결과들[i] = r
            _줄바꾸기(i, 상태="확인필요" if r.경고 else "완료", 메모=" / ".join(r.경고),
                    전문성=r.전문성, 역량=r.역량, 작성자=" / ".join(r.작성자))
        except Exception as exc:  # noqa: BLE001 - 한 사람 실패가 전체를 멈추면 안 된다
            _줄바꾸기(i, 상태="실패", 메모=f"{type(exc).__name__}: {str(exc)[:200]}")

    try:
        일꾼 = max(1, int(os.environ.get("VERIFYSUM_WORKERS", "2")))
        with ThreadPoolExecutor(일꾼) as 풀:
            list(풀.map(하나, range(len(자료들))))
        된것 = [r for r in 결과들 if r is not None]
        if 된것:
            폴더.mkdir(parents=True, exist_ok=True)
            (폴더 / 결과_1장).write_bytes(
                generate.한장파일(된것, (폴더 / learn.틀_1장).read_bytes()))
            (폴더 / 결과_요약).write_bytes(
                generate.요약파일(된것, (폴더 / learn.틀_요약).read_bytes(), 학습))
    except Exception as exc:  # noqa: BLE001
        with _잠금:
            작업["오류"] = f"결과 파일을 만들지 못했습니다: {type(exc).__name__}: {exc}"
    finally:
        with _잠금:
            작업.update(돌고있음=False, 끝=time.time())
            (폴더 / 결과_목록).parent.mkdir(parents=True, exist_ok=True)
            (폴더 / 결과_목록).write_text(json.dumps(작업, ensure_ascii=False), encoding="utf-8")


# --- 화면 ----------------------------------------------------------------------------
CSS = """
:root{--bg:#f4f6fa;--card:#fff;--line:#e3e8ef;--txt:#1b2b4b;--muted:#5b6b85;--acc:#2563eb;--acc2:#e3edfe;
--ok:#15803d;--warn:#b45309;--bad:#b91c1c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:15px/1.6 'Malgun Gothic','Apple SD Gothic Neo',sans-serif}
header{background:#fff;border-bottom:1px solid var(--line);padding:14px 28px;font-weight:700;font-size:17px}
main{max-width:1100px;margin:24px auto;padding:0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px 26px;margin-bottom:18px}
h2{margin:0 0 4px;font-size:18px}h2 .n{display:inline-block;width:26px;height:26px;border-radius:50%;
background:var(--acc);color:#fff;text-align:center;font-size:14px;line-height:26px;margin-right:8px}
.muted{color:var(--muted);font-size:13.5px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:14px 0}
.drop{border:1.5px dashed #c9d6ea;border-radius:10px;padding:14px;background:#fafcff}
.drop b{display:block;margin-bottom:6px}button,.btn{background:var(--acc);color:#fff;border:0;border-radius:8px;
padding:9px 18px;font-size:14.5px;cursor:pointer;text-decoration:none;display:inline-block}
button.sec,.btn.sec{background:#fff;color:var(--acc);border:1px solid #c9d6ea}button:disabled{opacity:.45;cursor:default}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:14px}th,td{border-bottom:1px solid var(--line);
padding:7px 8px;text-align:left;vertical-align:top}th{background:#f7f9fc;font-weight:600;color:var(--muted)}
.pill{display:inline-block;border-radius:999px;padding:1px 10px;font-size:12.5px;font-weight:600;background:#eef2f7}
.s-완료{background:#dcfce7;color:var(--ok)}.s-확인필요{background:#fef3c7;color:var(--warn)}
.s-실패{background:#fee2e2;color:var(--bad)}.s-처리중{background:var(--acc2);color:var(--acc)}
textarea{width:100%;min-height:260px;font:13.5px/1.6 'Malgun Gothic',sans-serif;border:1px solid var(--line);
border-radius:8px;padding:10px}.kv td:first-child{width:180px;color:var(--muted)}
.msg{padding:10px 14px;border-radius:8px;margin-bottom:14px}.msg.ok{background:#dcfce7}.msg.bad{background:#fee2e2}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}
"""


def _쪽(본문: str, 알림: str = "", 나쁨: bool = False) -> bytes:
    알 = f"<div class='msg {'bad' if 나쁨 else 'ok'}'>{html.escape(알림)}</div>" if 알림 else ""
    return ("<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>검증 자료 정리</title><style>{CSS}</style></head><body>"
            "<header>검증 자료 정리 <span class='muted' style='font-weight:400'>· 1장 통합본 + 요약표</span></header>"
            f"<main>{알}{본문}</main>{_JS}</body></html>").encode("utf-8")


def _학습칸(학습: dict | None) -> str:
    파일칸 = "".join(
        f"<div class='drop'><b>{제목}</b><input type='file' name='{이름}' accept='.docx' required>"
        f"<div class='muted'>{설명}</div></div>"
        for 이름, 제목, 설명 in (
            ("원본", "예시 검증 파일", "한 사람의 원본 (2장짜리)"),
            ("한장", "예시 1장 결과", "그 사람을 1장으로 줄인 것"),
            ("요약", "예시 요약 결과", "개인별 검증결과 요약 표")))
    양식 = ("<form method='post' action='/learn' enctype='multipart/form-data'>"
          f"<div class='grid'>{파일칸}</div>"
          "<div class='row'><button>분석하기</button><span class='muted'>"
          "예시를 보고 문체·분량·칸 너비를 파악해 작성 지침을 만듭니다 (1~2분).</span></div></form>")
    if not 학습:
        return ("<div class='card'><h2><span class='n'>1</span>예시로 방식 익히기</h2>"
                "<p class='muted'>세 파일을 넣으면 어떻게 줄이고 어떤 말투로 쓰는지 정리합니다.</p>"
                f"{양식}</div>")
    칸 = 학습["칸"]
    전 = 칸["전문성"]
    한줄 = 전["폭_pt"] / 전["크기_pt"]
    항목 = " · ".join(a for a, _b in 학습["원본"]["항목"])
    정보 = (
        "<table class='kv'>"
        f"<tr><td>예시 후보자</td><td>{html.escape(학습['원본']['제목'])} "
        f"<span class='muted'>({html.escape(학습['원본']['소속'])})</span></td></tr>"
        f"<tr><td>항목 {len(학습['원본']['항목'])}개</td><td>{html.escape(항목)}</td></tr>"
        f"<tr><td>분량</td><td>원본 {sum(학습['분량']['원본글자'])}자 → 1장 "
        f"{sum(학습['분량']['1장글자'])}자 (이 분량을 한 쪽 기준으로 씁니다)</td></tr>"
        f"<tr><td>요약 칸</td><td>너비 {전['폭_pt'] / 72 * 2.54 + 0.38:.1f}cm · "
        f"{html.escape(전.get('글꼴') or '')} {전['크기_pt']:g}pt → 한 줄에 한글 약 {int(한줄)}자, "
        f"두 줄 약 {fit.줄에_맞는_글자수(한줄)}자로 씁니다</td></tr>"
        f"<tr><td>분석한 때</td><td>{html.escape(학습['만든때'])}</td></tr></table>")
    return (
        "<div class='card'><h2><span class='n'>1</span>예시로 익힌 방식</h2>" + 정보 +
        "<h3 style='margin:18px 0 6px;font-size:15px'>작성 지침 <span class='muted'>"
        "(결과를 만들 때 LLM 에 그대로 넘깁니다. 고쳐서 저장할 수 있어요)</span></h3>"
        "<form method='post' action='/guide'><textarea name='지침'>"
        f"{html.escape(학습.get('지침') or '')}</textarea>"
        "<div class='row'><button class='sec'>지침 저장</button></div></form>"
        "<details style='margin-top:14px'><summary class='muted'>다른 예시로 다시 분석하기</summary>"
        f"{양식}</details></div>")


def _실행칸(학습: dict | None) -> str:
    with _잠금:
        돈다 = 작업["돌고있음"]
    막음 = " disabled" if (not 학습 or 돈다) else ""
    return ("<div class='card'><h2><span class='n'>2</span>후보자 검증 파일 넣기</h2>"
            "<p class='muted'>30~40개를 한 번에 골라 넣으세요. 파일 이름 순서대로 결과에 들어갑니다. "
            "원본 파일은 저장하지 않습니다.</p>"
            "<form method='post' action='/run' enctype='multipart/form-data'>"
            "<div class='drop'><input type='file' name='files' accept='.docx' multiple required></div>"
            f"<div class='row'><button{막음}>결과 만들기</button>"
            + ("" if 학습 else "<span class='muted'>먼저 1단계를 해 주세요.</span>")
            + "</div></form></div>")


def _결과칸() -> str:
    return ("<div class='card' id='진행'><h2><span class='n'>3</span>진행 · 결과</h2>"
            "<div id='진행몸'><p class='muted'>아직 돌린 작업이 없습니다.</p></div></div>")


_JS = """<script>
function esc(s){return String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function poll(){
  let r=await fetch('/status'); let s=await r.json(); let el=document.getElementById('진행몸'); if(!el) return;
  if(!s.줄.length){ setTimeout(poll, 3000); return; }
  let n=s.줄.length, done=s.줄.filter(x=>['완료','확인필요','실패'].includes(x.상태)).length;
  let h='<p>'+(s.돌고있음?'<b>처리 중</b> ':'<b>끝남</b> ')+done+' / '+n+'명</p>';
  if(s.오류) h+='<div class="msg bad">'+esc(s.오류)+'</div>';
  if(!s.돌고있음 && s.파일있음) h+='<div class="row"><a class="btn" href="/download/1">1장 통합본 받기</a>'+
     '<a class="btn" href="/download/2">요약표 받기</a><span class="muted">확인필요 표시된 사람은 워드에서 한 번 봐 주세요.</span></div>';
  h+='<table><tr><th>파일</th><th>후보자</th><th>상태</th><th>전문성/성과</th><th>연구전문가 역량</th><th>작성자</th><th>메모</th></tr>';
  for(const x of s.줄){ h+='<tr><td>'+esc(x.파일)+'</td><td>'+esc(x.후보자)+'</td><td><span class="pill s-'+esc(x.상태)+'">'+esc(x.상태)+
     '</span></td><td>'+esc(x.전문성)+'</td><td>'+esc(x.역량)+'</td><td>'+esc(x.작성자)+'</td><td class="muted">'+esc(x.메모)+'</td></tr>'; }
  el.innerHTML=h+'</table>';
  if(s.돌고있음) setTimeout(poll, 2000);
}
poll();
</script>"""


def _폼(본문: bytes, 형식: str) -> tuple[dict[str, str], dict[str, list[tuple[str, bytes]]]]:
    """multipart 를 (글 필드, {이름: [(파일명, 내용)]}) 로."""
    msg = BytesParser(policy=기본정책).parsebytes(
        b"Content-Type: " + 형식.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + 본문)
    글, 파일 = {}, {}
    for part in msg.iter_parts():
        이름 = part.get_param("name", header="content-disposition") or ""
        파일명 = part.get_filename()
        if 파일명 is not None:
            내용 = part.get_payload(decode=True) or b""
            if 내용:
                파일.setdefault(이름, []).append((Path(파일명).name, 내용))
        else:
            글[이름] = part.get_content()
    return 글, 파일


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 조용히
        pass

    def _보냄(self, 본문: bytes, 형식: str = "text/html; charset=utf-8", 코드: int = 200, 머리=None):
        self.send_response(코드)
        self.send_header("Content-Type", 형식)
        self.send_header("Content-Length", str(len(본문)))
        for k, v in (머리 or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(본문)

    def _문지기(self) -> bool:
        비번 = os.environ.get("VERIFYSUM_PASSWORD", "")
        if not 비번:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                if base64.b64decode(h[6:]).decode().split(":", 1)[1] == 비번:
                    return True
            except Exception:  # noqa: BLE001
                pass
        self._보냄(b"", 코드=401, 머리={"WWW-Authenticate": 'Basic realm="verifysum"'})
        return False

    def _본문(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _첫화면(self, 알림: str = "", 나쁨: bool = False):
        학습 = learn.불러오기(폴더)
        self._보냄(_쪽(_학습칸(학습) + _실행칸(학습) + _결과칸(), 알림, 나쁨))

    def do_GET(self):
        if not self._문지기():
            return
        경로 = urllib.parse.urlparse(self.path).path
        if 경로 == "/":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._첫화면((q.get("msg") or [""])[0], bool(q.get("bad")))
        if 경로 == "/status":
            with _잠금:
                s = dict(작업)
            s["파일있음"] = (폴더 / 결과_1장).exists() and (폴더 / 결과_요약).exists() and bool(s["끝"])
            return self._보냄(json.dumps(s, ensure_ascii=False).encode(), "application/json")
        if 경로 in ("/download/1", "/download/2"):
            이름 = 결과_1장 if 경로.endswith("1") else 결과_요약
            p = 폴더 / 이름
            if not p.exists():
                return self._보냄("아직 결과가 없습니다".encode(), 코드=404)
            return self._보냄(p.read_bytes(),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            머리={"Content-Disposition":
                                  "attachment; filename*=UTF-8''" + urllib.parse.quote(이름)})
        self._보냄(b"not found", 코드=404)

    def _돌아가기(self, 알림: str, 나쁨: bool = False):
        self._보냄(b"", 코드=303, 머리={"Location": "/?msg=" + urllib.parse.quote(알림)
                                         + ("&bad=1" if 나쁨 else "")})

    def do_POST(self):
        if not self._문지기():
            return
        경로 = urllib.parse.urlparse(self.path).path
        형식 = self.headers.get("Content-Type", "")
        본문 = self._본문()
        if 경로 == "/learn":
            _글, 파일 = _폼(본문, 형식)
            try:
                원본, 한장, 요약 = (파일[k][0][1] for k in ("원본", "한장", "요약"))
            except (KeyError, IndexError):
                return self._돌아가기("세 파일을 모두 넣어 주세요.", True)
            try:
                학습 = learn.분석(원본, 한장, 요약)
            except D.형식오류 as exc:
                return self._돌아가기(str(exc), True)
            c = _클라이언트()
            try:
                학습["지침"] = learn.지침_만들기(학습, c)
            finally:
                getattr(c, "close", lambda: None)()
            learn.저장(폴더, 학습, 한장, 요약)
            return self._돌아가기("예시를 분석했습니다. 지침을 확인하고 2단계로 넘어가세요.")
        if 경로 == "/guide":
            학습 = learn.불러오기(폴더)
            if not 학습:
                return self._돌아가기("먼저 예시를 분석해 주세요.", True)
            글 = urllib.parse.parse_qs(본문.decode("utf-8", "replace")).get("지침", [""])[0]
            학습["지침"] = 글.strip()
            learn.저장(폴더, 학습)
            return self._돌아가기("지침을 저장했습니다.")
        if 경로 == "/run":
            학습 = learn.불러오기(폴더)
            if not 학습:
                return self._돌아가기("먼저 예시를 분석해 주세요.", True)
            with _잠금:
                if 작업["돌고있음"]:
                    return self._돌아가기("이미 돌고 있는 작업이 있습니다.", True)
            _글, 파일 = _폼(본문, 형식)
            목록 = 파일.get("files", [])
            if not 목록:
                return self._돌아가기("파일을 골라 주세요.", True)
            for f in (폴더 / 결과_1장, 폴더 / 결과_요약):
                f.unlink(missing_ok=True)
            작업시작(목록, 학습)
            return self._돌아가기(f"{len(목록)}개 파일을 정리하기 시작했습니다.")
        self._보냄(b"not found", 코드=404)


def main() -> None:
    호스트 = os.environ.get("VERIFYSUM_HOST", "127.0.0.1")
    포트 = int(os.environ.get("VERIFYSUM_PORT", "8795"))
    if 호스트 not in ("127.0.0.1", "localhost") and not os.environ.get("VERIFYSUM_PASSWORD"):
        print("경고: 다른 PC 에서 열 수 있는데 비밀번호(VERIFYSUM_PASSWORD)가 없습니다.")
    print(f"검증 자료 정리: http://{호스트}:{포트}  (데이터: {폴더})")
    ThreadingHTTPServer((호스트, 포트), Handler).serve_forever()
