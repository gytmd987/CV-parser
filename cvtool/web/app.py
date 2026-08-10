"""사내 CV 분석 웹 앱 (표준 라이브러리 http.server).

폐쇄망이라 FastAPI/uvicorn 이 없을 수 있어 표준 라이브러리만 쓴다.

기능
  - 간단 로그인 (환경변수 비밀번호 + 세션 쿠키)
  - CV 여러 개 동시 업로드 -> 백그라운드 추출 -> 결과 여러 줄
  - 결과 표 화면 / 엑셀(.xlsx) 다운로드 / TSV 복사
  - 학회·저널 등급 관리 (미등록은 자동으로 '미분류' 등록)

실행:  python3 -m cvtool.web.app
"""

from __future__ import annotations

import html
import os
import queue
import secrets
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import settings
from ..export import records_to_tsv, records_to_xlsx
from ..extract import extract_cv_from_text
from ..ingestion.parsers import UnsupportedFormat, extract_text
from ..schemas import COLUMNS
from ..store import CandidateStore
from ..timeutil import now_kst
from ..venues import DEFAULT_TIERS, VenueRegistry, apply_registry
from .multipart import parse_multipart

DATA_DIR = Path(os.environ.get("CVTOOL_DATA_DIR", Path.home() / ".cvtool"))
WEB_PASSWORD = os.environ.get("CVTOOL_WEB_PASSWORD", "")
HOST = os.environ.get("CVTOOL_WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("CVTOOL_WEB_PORT", "8600"))

store = CandidateStore(DATA_DIR / "candidates.db")
registry = VenueRegistry(DATA_DIR / "venues.db")

_sessions: set[str] = set()
_jobs: "queue.Queue[tuple[str, str]]" = queue.Queue()
_status_lock = threading.Lock()
_status: dict[str, dict] = {}  # filename -> {state, message}


# ---------------------------------------------------------------------------
# 백그라운드 추출 워커
# ---------------------------------------------------------------------------
def _set_status(name: str, state: str, message: str = "") -> None:
    with _status_lock:
        _status[name] = {"state": state, "message": message, "시각": now_kst().strftime("%H:%M:%S")}


def _worker() -> None:
    while True:
        filename, text = _jobs.get()
        try:
            _set_status(filename, "처리중")
            rec = extract_cv_from_text(text, 원본_파일명=filename)
            apply_registry(rec, registry)
            store.save(rec)
            state = "검토필요" if rec.검토_필요 == "Y" else "완료"
            _set_status(filename, state, rec.검토_사유)
        except Exception as exc:  # noqa: BLE001 - 워커가 죽으면 안 된다
            _set_status(filename, "실패", f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
        finally:
            _jobs.task_done()


threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--line:#dfe3e8;--txt:#1b1f24;--muted:#666;--accent:#2563eb}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.55 "맑은 고딕",system-ui,sans-serif}
header{background:#1b1f24;color:#fff;padding:12px 20px;display:flex;gap:18px;align-items:center}
header a{color:#cbd5e1;text-decoration:none;font-weight:600}
header a:hover{color:#fff}
header .sp{flex:1}
main{padding:20px;max-width:1600px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:18px;margin-bottom:18px}
h2{margin:0 0 12px;font-size:16px}
button,.btn{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:8px 14px;
 font-size:14px;cursor:pointer;text-decoration:none;display:inline-block}
button:hover,.btn:hover{opacity:.9}
.btn.sec{background:#4b5563}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{border:1px solid var(--line);padding:5px 7px;text-align:left;white-space:nowrap;
 max-width:260px;overflow:hidden;text-overflow:ellipsis}
th{background:#eef2f7;position:sticky;top:0}
tr:nth-child(even) td{background:#fafbfc}
.scroll{overflow:auto;max-height:70vh;border:1px solid var(--line);border-radius:6px}
.flag{color:#b91c1c;font-weight:700}
.ok{color:#15803d}
.muted{color:var(--muted);font-size:12.5px}
.warn{background:#fef3c7;border:1px solid #fcd34d;padding:10px 14px;border-radius:6px;margin-bottom:14px}
input[type=password],input[type=text],select{padding:7px 9px;border:1px solid var(--line);
 border-radius:6px;font-size:14px}
.login{max-width:340px;margin:14vh auto}
.pill{padding:2px 8px;border-radius:99px;font-size:11.5px;font-weight:700}
.p-미분류{background:#fee2e2;color:#b91c1c}
.p-처리중{background:#dbeafe;color:#1d4ed8}
.p-완료{background:#dcfce7;color:#15803d}
.p-검토필요{background:#fef3c7;color:#92400e}
.p-실패{background:#fee2e2;color:#b91c1c}
"""


def _page(title: str, body: str, nav: bool = True) -> bytes:
    미분류 = registry.unclassified_count() if nav else 0
    badge = f' <span class="pill p-미분류">{미분류}</span>' if 미분류 else ""
    header = (
        "<header><a href='/'>CV 분석</a>"
        "<a href='/'>지원자</a>"
        f"<a href='/venues'>학회·저널 관리{badge}</a>"
        "<span class='sp'></span><a href='/logout'>로그아웃</a></header>"
        if nav
        else ""
    )
    return (
        f"<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{header}<main>{body}</main></body></html>"
    ).encode("utf-8")


def _login_page(error: str = "") -> bytes:
    msg = f"<p class='flag'>{html.escape(error)}</p>" if error else ""
    return _page(
        "로그인",
        f"""<div class='card login'><h2>CV 분석 툴</h2>{msg}
        <form method='post' action='/login'>
        <p><input type='password' name='password' placeholder='비밀번호' autofocus
           style='width:100%'></p>
        <button type='submit' style='width:100%'>로그인</button></form>
        <p class='muted'>채용 담당자 전용입니다.</p></div>""",
        nav=False,
    )


def _status_table() -> str:
    with _status_lock:
        items = list(_status.items())
    if not items:
        return ""
    rows = "".join(
        f"<tr><td>{html.escape(n)}</td>"
        f"<td><span class='pill p-{s['state']}'>{s['state']}</span></td>"
        f"<td>{html.escape(s.get('message',''))}</td><td>{s['시각']}</td></tr>"
        for n, s in items
    )
    처리중 = any(s["state"] == "처리중" for _, s in items)
    refresh = "<meta http-equiv='refresh' content='5'>" if 처리중 else ""
    return (
        f"{refresh}<div class='card'><h2>업로드 처리 현황</h2>"
        f"<table><tr><th>파일</th><th>상태</th><th>메모</th><th>시각</th></tr>"
        f"{rows}</table>"
        + ("<p class='muted'>처리 중입니다. 5초마다 자동 새로고침됩니다.</p>" if 처리중 else "")
        + "</div>"
    )


def _dashboard() -> bytes:
    records = store.list_all()
    미분류 = registry.unclassified_count()
    warn = (
        f"<div class='warn'>분류되지 않은 학회·저널이 <b>{미분류}건</b> 있습니다. "
        f"판별 전까지 해외 논문 열이 부정확할 수 있습니다. "
        f"<a href='/venues'>지금 분류하기 →</a></div>"
        if 미분류
        else ""
    )

    head = "".join(f"<th>{html.escape(c)}</th>" for c in COLUMNS)
    body_rows = []
    for rec in records:
        row = rec.to_row()
        cells = []
        for c in COLUMNS:
            v = html.escape(str(row.get(c, "") or ""))
            cls = " class='flag'" if c == "검토_필요" and v == "Y" else ""
            cells.append(f"<td{cls} title='{v}'>{v}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    table = (
        f"<div class='scroll'><table><tr>{head}</tr>{''.join(body_rows)}</table></div>"
        if records
        else "<p class='muted'>아직 등록된 지원자가 없습니다. CV를 업로드하세요.</p>"
    )

    return _page(
        "지원자",
        f"""{warn}
        <div class='card'><h2>CV 업로드</h2>
          <form method='post' action='/upload' enctype='multipart/form-data'>
            <p><input type='file' name='files' multiple accept='.pdf,.docx,.txt,.md'></p>
            <button type='submit'>업로드 후 분석</button>
            <span class='muted'>여러 개를 한 번에 선택할 수 있습니다 (PDF/docx/txt).</span>
          </form>
        </div>
        {_status_table()}
        <div class='card'>
          <h2>지원자 {len(records)}명</h2>
          <p><a class='btn' href='/export.xlsx'>엑셀(.xlsx) 다운로드</a>
             <a class='btn sec' href='/export.tsv'>TSV 보기(복사용)</a></p>
          {table}
        </div>""",
    )


def _venues_page() -> bytes:
    venues = registry.list_all()
    tier_opts = lambda cur: "".join(  # noqa: E731
        f"<option value='{t}'{' selected' if t == cur else ''}>{t}</option>"
        for t in DEFAULT_TIERS
    )
    sel = lambda name, cur, opts: (  # noqa: E731
        f"<select name='{name}'>"
        + "".join(
            f"<option value='{o}'{' selected' if o == cur else ''}>{o or '-'}</option>"
            for o in opts
        )
        + "</select>"
    )

    rows = []
    for v in venues:
        rows.append(
            f"<tr><td>{html.escape(v.표시명)}</td>"
            f"<td>{v.발견횟수}</td>"
            f"<td><form method='post' action='/venues' style='display:flex;gap:6px'>"
            f"<input type='hidden' name='id' value='{v.id}'>"
            f"<select name='등급'>{tier_opts(v.등급)}</select>"
            f"{sel('유형', v.유형, ['', '학회', '저널', '기타'])}"
            f"{sel('국내해외', v.국내해외, ['불명', '해외', '국내'])}"
            f"<button type='submit'>저장</button></form></td></tr>"
        )

    미분류 = registry.unclassified_count()
    note = (
        f"<div class='warn'>미분류 <b>{미분류}건</b> — 위쪽에 먼저 표시됩니다.</div>"
        if 미분류
        else "<p class='ok'>모두 분류되었습니다.</p>"
    )
    table = (
        f"<table><tr><th>학회/저널</th><th>발견</th><th>분류</th></tr>{''.join(rows)}</table>"
        if rows
        else "<p class='muted'>아직 등록된 학회·저널이 없습니다. CV를 업로드하면 자동으로 등록됩니다.</p>"
    )
    return _page(
        "학회·저널 관리",
        f"""<div class='card'><h2>학회·저널 등급 관리</h2>{note}
        <p class='muted'>CV에서 발견된 제출처 중 목록에 없는 것은 자동으로
        '미분류'로 추가됩니다. 등급·유형·국내해외를 지정하면 이후 추출과
        엑셀 출력에 반영됩니다.</p>
        <div class='scroll'>{table}</div></div>""",
    )


# ---------------------------------------------------------------------------
# HTTP 핸들러
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "cvtool"

    def log_message(self, fmt: str, *args) -> None:  # 접근 로그 간소화
        print(f"[{now_kst().strftime('%H:%M:%S')}] {fmt % args}")

    # -- 유틸 ---------------------------------------------------------------
    def _session_ok(self) -> bool:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "cvsession" and v in _sessions:
                return True
        return False

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", code: int = 200,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # -- GET ----------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path

        if path == "/login":
            return self._send(_login_page())
        if path == "/logout":
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "cvsession":
                    _sessions.discard(v)
            return self._redirect("/login")

        if not self._session_ok():
            return self._redirect("/login")

        if path == "/":
            return self._send(_dashboard())
        if path == "/venues":
            return self._send(_venues_page())
        if path == "/export.xlsx":
            data = records_to_xlsx(store.list_all())
            stamp = now_kst().strftime("%Y%m%d_%H%M")
            return self._send(
                data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                extra={"Content-Disposition": f'attachment; filename="cv_{stamp}.xlsx"'},
            )
        if path == "/export.tsv":
            tsv = records_to_tsv(store.list_all())
            body = _page(
                "TSV",
                "<div class='card'><h2>엑셀 붙여넣기용 TSV</h2>"
                "<p class='muted'>전체 선택 후 복사해서 엑셀에 붙여넣으세요.</p>"
                f"<textarea style='width:100%;height:60vh'>{html.escape(tsv)}</textarea></div>",
            )
            return self._send(body)
        return self._send(_page("없음", "<div class='card'>페이지가 없습니다.</div>"), code=404)

    # -- POST ---------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path

        if path == "/login":
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            pw = (data.get("password") or [""])[0]
            if not WEB_PASSWORD:
                return self._send(_login_page("서버에 CVTOOL_WEB_PASSWORD 가 설정되지 않았습니다."))
            if secrets.compare_digest(pw, WEB_PASSWORD):
                token = secrets.token_urlsafe(32)
                _sessions.add(token)
                return self._redirect(
                    "/", {"Set-Cookie": f"cvsession={token}; HttpOnly; Path=/; SameSite=Strict"}
                )
            return self._send(_login_page("비밀번호가 틀렸습니다."))

        if not self._session_ok():
            return self._redirect("/login")

        if path == "/upload":
            form = parse_multipart(self._read_body(), self.headers.get("Content-Type", ""))
            if not form.files:
                return self._redirect("/")
            tmp = DATA_DIR / "incoming"
            tmp.mkdir(parents=True, exist_ok=True)
            for f in form.files:
                dest = tmp / f.filename
                try:
                    dest.write_bytes(f.content)
                    text = extract_text(dest)
                    if not text.strip():
                        _set_status(f.filename, "실패", "텍스트를 추출하지 못했습니다(스캔 PDF?)")
                        continue
                    _set_status(f.filename, "대기중")
                    _jobs.put((f.filename, text))
                except UnsupportedFormat as exc:
                    _set_status(f.filename, "실패", str(exc))
                except Exception as exc:  # noqa: BLE001
                    _set_status(f.filename, "실패", f"{type(exc).__name__}: {exc}")
                finally:
                    dest.unlink(missing_ok=True)  # 원본 CV 는 디스크에 남기지 않는다
            return self._redirect("/")

        if path == "/venues":
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            try:
                vid = int((data.get("id") or ["0"])[0])
            except ValueError:
                return self._redirect("/venues")
            registry.classify(
                vid,
                등급=(data.get("등급") or [None])[0],
                유형=(data.get("유형") or [None])[0],
                국내해외=(data.get("국내해외") or [None])[0],
            )
            return self._redirect("/venues")

        return self._send(_page("없음", "<div class='card'>없는 경로입니다.</div>"), code=404)


def main() -> int:
    if not WEB_PASSWORD:
        print("⚠️  CVTOOL_WEB_PASSWORD 가 설정되지 않았습니다. 로그인할 수 없습니다.")
        print("    예: export CVTOOL_WEB_PASSWORD='사내에서정한비밀번호'")
    print(f"데이터 저장 위치 : {DATA_DIR}")
    print(f"지원자 {store.count()}명 / 학회·저널 미분류 {registry.unclassified_count()}건")
    print(f"http://{HOST}:{PORT}/ 에서 실행합니다. (Ctrl+C 로 종료)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
