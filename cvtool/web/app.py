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
import uuid
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import settings
from ..dotenv import LOADED_FROM, candidate_paths
from ..export import records_to_tsv, records_to_xlsx
from ..fsutil import is_world_readable, mode_of, safe_filename, secure_dir, secure_file
from ..extract import extract_cv_from_text
from ..ingestion.parsers import UnsupportedFormat, extract_text
from ..schemas import columns as table_columns
from ..store import SUPPORTED_SUFFIXES, CandidateStore
from ..timeutil import now_kst
from ..dedup import fingerprint, find_duplicates
from ..names import GRADED_KINDS, KINDS, NameRegistry, observe_record
from .multipart import parse_multipart

DATA_DIR = Path(os.environ.get("CVTOOL_DATA_DIR", Path.home() / ".cvtool"))
WEB_PASSWORD = os.environ.get("CVTOOL_WEB_PASSWORD", "")
HOST = os.environ.get("CVTOOL_WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("CVTOOL_WEB_PORT", "8600"))

CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
}

store = CandidateStore(DATA_DIR / "candidates.db", DATA_DIR / "files")
_names_db = DATA_DIR / "names.db"
_old_venues = DATA_DIR / "venues.db"
if _old_venues.is_file() and not _names_db.is_file():
    # 예전 학회 목록을 그대로 이어받는다 (분류해둔 등급이 날아가면 안 된다)
    import shutil

    shutil.copy2(_old_venues, _names_db)
registry = NameRegistry(_names_db)

_sessions: set[str] = set()
_jobs: "queue.Queue[tuple[str, str, str | None]]" = queue.Queue()
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
        filename, 지원자_ID, 저장_파일명 = _jobs.get()
        try:
            _set_status(filename, "처리중")
            path = store.files_dir / 저장_파일명
            # 보관된 원본에서 매번 새로 뽑는다. 그래야 PDF 파서를 개선하면
            # 재분석만으로 반영된다 (텍스트를 캐시하면 옛 추출에 갇힌다).
            text = extract_text(path)
            if not text.strip():
                _set_status(filename, "실패", "텍스트를 추출하지 못했습니다(스캔 PDF?)")
                continue
            rec = extract_cv_from_text(text, 원본_파일명=filename, 지원자_ID=지원자_ID)

            # 이름들을 사전에 등록만 한다. 레코드 값은 건드리지 않는다.
            미분류 = observe_record(rec, registry)
            if 미분류:
                사유 = "미분류 학회/저널: " + ", ".join(미분류)
                rec.검토_사유 = f"{rec.검토_사유} / {사유}" if rec.검토_사유 else 사유
                rec.검토_필요 = "Y"

            # 중복 검토
            fp = fingerprint(text)
            후보 = find_duplicates(rec, fp, store.fingerprints())
            메모 = " / ".join(str(m) for m in 후보)
            if 후보:
                확실 = [m for m in 후보 if m.수준 == "확실"]
                말머리 = "중복 확실" if 확실 else "중복 의심"
                rec.검토_사유 = f"{rec.검토_사유} / {말머리}: {메모}" if rec.검토_사유 else f"{말머리}: {메모}"
                rec.검토_필요 = "Y"

            store.save(rec, 원문_텍스트=text, 저장_파일명=저장_파일명, 지문=fp, 중복_메모=메모)
            state = "중복의심" if 후보 else ("검토필요" if rec.검토_필요 == "Y" else "완료")
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
button.danger,.btn.danger{background:#b91c1c}
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
.p-중복의심{background:#ffe4e6;color:#9f1239}
.p-대기중{background:#e5e7eb;color:#374151}
.dup{background:#fff1f2}
"""


def _page(title: str, body: str, nav: bool = True) -> bytes:
    미분류 = registry.unclassified_count() if nav else 0
    badge = f' <span class="pill p-미분류">{미분류}</span>' if 미분류 else ""
    header = (
        "<header><a href='/'>CV 분석</a>"
        "<a href='/'>지원자</a>"
        f"<a href='/names?kind=학회'>명칭 관리{badge}</a>"
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
        + "<p><form method='post' action='/status/clear' style='display:inline'>"
        "<button type='submit' class='sec'>현황 지우기</button></form>"
        "<span class='muted'> 이 목록만 비웁니다. 지원자는 지워지지 않습니다.</span></p>"
        + "</div>"
    )


def _dashboard(q: str = "", review_only: bool = False) -> bytes:
    records = store.list_filtered(q, review_only)
    전체 = store.count()
    만료 = store.expired_count()
    expiry = store.expiry_map()
    미분류 = registry.unclassified_count()

    warns = []
    if 미분류:
        warns.append(
            f"<div class='warn'>분류되지 않은 학회·저널이 <b>{미분류}건</b> 있습니다. "
            f"판별 전까지 해외 논문 열이 부정확할 수 있습니다. "
            f"<a href='/venues'>지금 분류하기 →</a></div>"
        )
    if 만료:
        warns.append(
            f"<div class='warn'>보관 기간이 지난 지원자가 <b>{만료}명</b> 있습니다. "
            f"<form method='post' action='/candidates/purge' style='display:inline'>"
            f"<button class='danger' onclick=\"return confirm('만료된 {만료}명을 삭제합니다. "
            f"되돌릴 수 없습니다. 진행할까요?')\">만료분 {만료}명 삭제</button></form></div>"
        )

    COLS = table_columns(registry)
    head = "".join(f"<th>{html.escape(c)}</th>" for c in COLS)
    body_rows = []
    for rec in records:
        row = rec.to_row(registry)
        cells = [
            f"<td><input type='checkbox' name='ids' value='{html.escape(rec.지원자_ID)}'></td>",
            f"<td><a href='/candidate?id={urllib.parse.quote(rec.지원자_ID)}'>상세</a></td>",
            f"<td class='muted'>{html.escape(expiry.get(rec.지원자_ID, ''))}</td>",
        ]
        for c in COLS:
            v = html.escape(str(row.get(c, "") or ""))
            cls = " class='flag'" if c == "검토_필요" and v == "Y" else ""
            cells.append(f"<td{cls} title='{v}'>{v}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    if records:
        table = f"""
        <form method='post' action='/candidates/delete'
              onsubmit="return confirm('선택한 지원자를 삭제합니다. 되돌릴 수 없습니다.')">
          <p><button type='submit' class='danger'>선택 삭제</button>
             <span class='muted'>체크한 지원자를 지웁니다.</span></p>
          <div class='scroll'><table>
            <tr><th><input type='checkbox' onclick="for(const c of
                this.closest('table').querySelectorAll('input[name=ids]'))c.checked=this.checked">
            </th><th></th><th>보관만료</th>{head}</tr>
            {''.join(body_rows)}
          </table></div>
        </form>"""
    elif 전체:
        table = "<p class='muted'>검색 조건에 맞는 지원자가 없습니다.</p>"
    else:
        table = "<p class='muted'>아직 등록된 지원자가 없습니다. CV를 업로드하세요.</p>"

    checked = " checked" if review_only else ""
    보관 = "켜짐 (재분석 가능)" if settings.store_cv_text else "꺼짐 (재분석하려면 재업로드 필요)"

    return _page(
        "지원자",
        f"""{''.join(warns)}
        <div class='card'><h2>CV 업로드</h2>
          <form method='post' action='/upload' enctype='multipart/form-data'>
            <p><input type='file' name='files' multiple accept='.pdf,.docx,.txt,.md'></p>
            <button type='submit'>업로드 후 분석</button>
            <span class='muted'>여러 개를 한 번에 선택할 수 있습니다 (PDF/docx/txt).</span>
          </form>
          <p class='muted'>원문 텍스트 보관: <b>{보관}</b> · 보관 기간 {settings.retention_months}개월</p>
        </div>
        {_status_table()}
        <div class='card'>
          <h2>지원자 {len(records)}명{f' / 전체 {전체}명' if len(records) != 전체 else ''}</h2>
          <form method='get' action='/' style='margin-bottom:12px'>
            <input type='text' name='q' value='{html.escape(q)}' placeholder='이름·소속·학교·파일명 검색'>
            <label class='muted'><input type='checkbox' name='review' value='1'{checked}>
              검토 필요만</label>
            <button type='submit'>검색</button>
            <a class='btn sec' href='/'>초기화</a>
          </form>
          <p><a class='btn' href='/export.xlsx'>엑셀(.xlsx) 다운로드</a>
             <a class='btn sec' href='/export.tsv'>TSV 보기(복사용)</a></p>
          {table}
        </div>""",
    )


def _candidate_page(지원자_ID: str) -> bytes:
    rec = store.get(지원자_ID)
    if rec is None:
        return _page("없음", "<div class='card'>해당 지원자를 찾을 수 없습니다.</div>")
    meta = store.meta(지원자_ID) or {}
    row = rec.to_row(registry)

    항목 = "".join(
        f"<tr><th style='width:180px'>{html.escape(c)}</th>"
        f"<td style='white-space:normal;max-width:none'>{html.escape(str(row.get(c,'') or '')) or '<span class=muted>-</span>'}</td></tr>"
        for c in table_columns(registry)
    )
    관리 = "".join(
        f"<tr><th style='width:180px'>{k}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in (
            ("원본 파일명", meta.get("원본_파일명") or "-"),
            ("등록 일시", meta.get("등록일시") or "-"),
            ("보관 만료일", meta.get("보관_만료일") or "-"),
            ("원본 파일 보관", "예" if meta.get("원본보유") else "아니오 (삭제됨)"),
            ("원문 텍스트 보관", "예" if meta.get("원문보유") else "아니오"),
        )
    )

    원본있음 = meta.get("원본보유")
    원본버튼 = (
        f"<a class='btn' href='/candidate/file?id={urllib.parse.quote(지원자_ID)}'>"
        f"원본 다운로드 ({html.escape(meta.get('원본_파일명') or '파일')})</a> "
        if 원본있음
        else ""
    )
    재분석 = (
        "<form method='post' action='/candidate/reanalyze' style='display:inline'>"
        f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
        "<button type='submit'>다시 분석</button></form> "
        if 원본있음
        else "<span class='muted'>보관된 원본이 없어 다시 분석하려면 재업로드해야 합니다.</span><br><br>"
    )

    return _page(
        f"지원자 {rec.한글_이름 or rec.지원자_ID}",
        f"""<div class='card'>
          <h2>{html.escape(rec.한글_이름 or '(이름 미상)')}
              <span class='muted'>{html.escape(rec.지원자_ID)}</span></h2>
          <p>{원본버튼}{재분석}
             <form method='post' action='/candidate/delete' style='display:inline'
                   onsubmit="return confirm('이 지원자를 삭제합니다. 되돌릴 수 없습니다.')">
               <input type='hidden' name='id' value='{html.escape(지원자_ID)}'>
               <button type='submit' class='danger'>삭제</button>
             </form>
             <a class='btn sec' href='/'>목록으로</a></p>
        </div>
        <div class='card'><h2>관리 정보</h2><table>{관리}</table></div>
        <div class='card'><h2>추출 결과</h2><table>{항목}</table></div>""",
    )


def _names_page(종류: str) -> bytes:
    """학교·학회·저널·전공을 같은 화면에서 관리한다.

    같은 대상을 다르게 적은 표기들을 하나로 묶고, 대표명을 정한다.
    여기서 고치면 이미 등록된 지원자 표에도 곧바로 반영된다.
    """
    if 종류 not in KINDS:
        종류 = "학회"
    items = registry.list_all(종류)
    등급목록 = registry.tier_names()

    탭 = " ".join(
        f"<a class='btn {'' if k == 종류 else 'sec'}' href='/names?kind={urllib.parse.quote(k)}'>"
        f"{k}"
        + (f" <b>{registry.unclassified_count(k)}</b>" if k in GRADED_KINDS
           and registry.unclassified_count(k) else "")
        + "</a>"
        for k in KINDS
    )

    등급열 = ""
    if 종류 in GRADED_KINDS:
        체크 = "".join(
            f"<label style='margin-right:14px'><input type='checkbox' name='tier'"
            f" value='{html.escape(t['이름'])}'{' checked' if t['표에_표시'] else ''}>"
            f"{html.escape(t['이름'])}</label>"
            for t in registry.tiers()
            if t["이름"] != "미분류"
        )
        등급열 = (
            "<div class='card'><h2>표에 개수 열로 낼 등급</h2>"
            "<form method='post' action='/names/tiers'>"
            f"<input type='hidden' name='kind' value='{html.escape(종류)}'>"
            f"{체크}<button type='submit'>저장</button></form>"
            "<p class='muted'>켠 등급마다 <code>1저자_해외논문_(등급)</code> 열이 표에 생깁니다.</p>"
            "</div>"
        )

    선택지 = "".join(
        f"<option value='{i.id}'>{html.escape(i.표시명)}</option>" for i in items
    )
    rows = []
    for i in items:
        별칭 = registry.aliases_of(i.id)
        별칭표시 = f"<span class='muted'> +별칭 {len(별칭)}</span>" if 별칭 else ""
        등급칸 = ""
        if 종류 in GRADED_KINDS:
            등급opt = "".join(
                f"<option{' selected' if t == i.등급 else ''}>{html.escape(t)}</option>"
                for t in 등급목록
            )
            해외opt = "".join(
                f"<option{' selected' if v == i.국내해외 else ''}>{v}</option>"
                for v in ("불명", "해외", "국내")
            )
            등급칸 = f"<select name='등급'>{등급opt}</select><select name='국내해외'>{해외opt}</select>"
        rows.append(
            f"<tr><td>{html.escape(i.표시명)}{별칭표시}</td><td>{i.발견횟수}</td>"
            f"<td><form method='post' action='/names/save' style='display:flex;gap:6px'>"
            f"<input type='hidden' name='id' value='{i.id}'>"
            f"<input type='hidden' name='kind' value='{html.escape(종류)}'>"
            f"<input type='text' name='표시명' value='{html.escape(i.표시명)}' style='width:200px'>"
            f"{등급칸}<button type='submit'>저장</button></form></td>"
            f"<td><form method='post' action='/names/merge' style='display:flex;gap:6px'"
            f" onsubmit=\"return confirm('이 표기를 선택한 항목으로 묶습니다. 되돌리려면 다시 등록해야 합니다.')\">"
            f"<input type='hidden' name='id' value='{i.id}'>"
            f"<input type='hidden' name='kind' value='{html.escape(종류)}'>"
            f"<select name='into'>{선택지}</select>"
            f"<button type='submit' class='sec'>여기로 묶기</button></form></td></tr>"
        )

    표 = (
        f"<table><tr><th>대표명</th><th>발견</th><th>수정</th><th>다른 표기와 묶기</th></tr>"
        f"{''.join(rows)}</table>"
        if rows
        else "<p class='muted'>아직 등록된 항목이 없습니다. CV를 업로드하면 자동으로 등록됩니다.</p>"
    )
    return _page(
        f"{종류} 관리",
        f"""<div class='card'><h2>명칭 관리</h2><p>{탭}</p>
        <p class='muted'>같은 대상을 다르게 적은 표기(예: 포항공대 / POSTECH / 포항공과대학교)를
        <b>여기로 묶기</b>로 하나로 만들고 대표명을 정하세요.
        <b>고치면 이미 등록된 지원자 표에도 바로 반영됩니다.</b></p></div>
        {등급열}
        <div class='card'><h2>{html.escape(종류)} {len(items)}건</h2>
        <div class='scroll'>{표}</div></div>""",
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
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(
                _dashboard(
                    q=(params.get("q") or [""])[0],
                    review_only=bool(params.get("review")),
                )
            )
        if path == "/candidate":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cid = (params.get("id") or [""])[0]
            return self._send(_candidate_page(cid))
        if path == "/candidate/file":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cid = (params.get("id") or [""])[0]
            fpath = store.file_path(cid) if cid else None
            if fpath is None:
                return self._send(
                    _page("없음", "<div class='card'>보관된 원본이 없습니다.</div>"), code=404
                )
            meta = store.meta(cid) or {}
            download_name = meta.get("원본_파일명") or fpath.name
            ctype = CONTENT_TYPES.get(fpath.suffix.lower(), "application/octet-stream")
            quoted = urllib.parse.quote(download_name)
            return self._send(
                fpath.read_bytes(), ctype,
                extra={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
            )
        if path == "/names":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(_names_page((params.get("kind") or ["학회"])[0]))
        if path == "/export.xlsx":
            data = records_to_xlsx(store.list_all(), registry)
            stamp = now_kst().strftime("%Y%m%d_%H%M")
            return self._send(
                data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                extra={"Content-Disposition": f'attachment; filename="cv_{stamp}.xlsx"'},
            )
        if path == "/export.tsv":
            tsv = records_to_tsv(store.list_all(), registry)
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
                where = f".env 를 {LOADED_FROM} 에서 읽었지만" if LOADED_FROM else ".env 를 찾지 못했고"
                return self._send(
                    _login_page(
                        f"CVTOOL_WEB_PASSWORD 가 비어 있습니다. {where} 그 안에 "
                        "CVTOOL_WEB_PASSWORD 값이 없습니다. 서버 콘솔 메시지를 확인하세요."
                    )
                )
            # compare_digest 는 비ASCII str 을 거부한다. 한글 비밀번호도 되도록 바이트로 비교.
            if secrets.compare_digest(pw.encode("utf-8"), WEB_PASSWORD.encode("utf-8")):
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
            for f in form.files:
                safe_name = safe_filename(f.filename)
                suffix = Path(safe_name).suffix.lower()
                if suffix not in SUPPORTED_SUFFIXES:
                    _set_status(safe_name, "실패",
                                f"지원하지 않는 형식: {suffix or '(확장자 없음)'}")
                    continue
                try:
                    cid = f"CV-{uuid.uuid4().hex[:8].upper()}"
                    저장명 = store.store_file(cid, safe_name, f.content)
                    _set_status(safe_name, "대기중")
                    _jobs.put((safe_name, cid, 저장명))
                except Exception as exc:  # noqa: BLE001
                    _set_status(safe_name, "실패", f"{type(exc).__name__}: {exc}")
            return self._redirect("/")

        if path == "/candidate/delete":
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            cid = (data.get("id") or [""])[0]
            if cid:
                store.delete(cid)
            return self._redirect("/")

        if path == "/candidates/delete":
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            ids = data.get("ids") or []
            if ids:
                store.delete_many(ids)
            return self._redirect("/")

        if path == "/candidates/purge":
            store.purge_expired()
            return self._redirect("/")

        if path == "/candidate/reanalyze":
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            cid = (data.get("id") or [""])[0]
            meta = store.meta(cid) if cid else None
            if not meta or not meta.get("저장_파일명"):
                return self._redirect(f"/candidate?id={urllib.parse.quote(cid)}")
            name = meta.get("원본_파일명") or cid
            _set_status(name, "대기중", "재분석")
            _jobs.put((name, cid, meta["저장_파일명"]))
            return self._redirect("/")

        if path == "/status/clear":
            with _status_lock:
                _status.clear()
            return self._redirect("/")

        if path == "/names/save":
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            kind = (data.get("kind") or ["학회"])[0]
            try:
                nid = int((data.get("id") or ["0"])[0])
            except ValueError:
                return self._redirect(f"/names?kind={urllib.parse.quote(kind)}")
            registry.classify(
                nid,
                표시명=(data.get("표시명") or [None])[0],
                등급=(data.get("등급") or [None])[0],
                국내해외=(data.get("국내해외") or [None])[0],
            )
            return self._redirect(f"/names?kind={urllib.parse.quote(kind)}")

        if path == "/names/merge":
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            kind = (data.get("kind") or ["학회"])[0]
            try:
                registry.merge(
                    int((data.get("id") or ["0"])[0]), int((data.get("into") or ["0"])[0])
                )
            except (ValueError, TypeError):
                pass
            return self._redirect(f"/names?kind={urllib.parse.quote(kind)}")

        if path == "/names/tiers":
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            kind = (data.get("kind") or ["학회"])[0]
            켠것 = set(data.get("tier") or [])
            for t in registry.tiers():
                if t["이름"] != "미분류":
                    registry.set_tier_column(t["이름"], t["이름"] in 켠것)
            return self._redirect(f"/names?kind={urllib.parse.quote(kind)}")

        return self._send(_page("없음", "<div class='card'>없는 경로입니다.</div>"), code=404)


def _startup_cleanup() -> list[str]:
    """시작할 때 데이터 디렉터리 권한을 조이고, 크래시로 남은 원본을 지운다.

    추출 도중 프로세스가 강제 종료되면 incoming/ 에 CV 원본이 남는다.
    다음 기동 때 반드시 치운다.
    """
    secure_dir(DATA_DIR)
    for name in ("candidates.db", "venues.db"):
        for suffix in ("", "-wal", "-shm"):
            secure_file(DATA_DIR / (name + suffix))

    secure_dir(store.files_dir)
    for f in store.files_dir.iterdir():
        if f.is_file():
            secure_file(f)

    leftovers = []
    # 예전 버전이 쓰던 임시 폴더에 원본이 남아 있으면 지운다
    incoming = DATA_DIR / "incoming"
    if incoming.is_dir():
        for f in incoming.iterdir():
            if f.is_file():
                leftovers.append(f.name)
                f.unlink(missing_ok=True)
    # DB 에 행이 없는 원본(추출 실패·크래시)도 개인정보이므로 지운다
    for f in store.orphan_files():
        leftovers.append(f.name)
        f.unlink(missing_ok=True)
    return leftovers


def main() -> int:
    leftovers = _startup_cleanup()
    if leftovers:
        print(f"⚠️  이전 실행에서 남은 CV 원본 {len(leftovers)}건을 삭제했습니다: "
              f"{', '.join(leftovers[:5])}{' ...' if len(leftovers) > 5 else ''}")
    if LOADED_FROM:
        print(f".env 읽음        : {LOADED_FROM}")
    else:
        print("⚠️  .env 파일을 찾지 못했습니다. 아래 위치를 확인했습니다:")
        for p in candidate_paths():
            print(f"      - {p}")

    if not WEB_PASSWORD:
        print("⚠️  CVTOOL_WEB_PASSWORD 가 비어 있어 로그인할 수 없습니다.")
        if LOADED_FROM:
            print(f"    {LOADED_FROM} 안에 아래 줄이 있는지 확인하세요 (앞의 # 제거):")
        print("      CVTOOL_WEB_PASSWORD=원하는비밀번호")
        print("    또는: export CVTOOL_WEB_PASSWORD='원하는비밀번호'")
    else:
        print(f"로그인 비밀번호  : 설정됨 ({len(WEB_PASSWORD)}자)")

    print(f"데이터 저장 위치 : {DATA_DIR} (권한 {mode_of(DATA_DIR):o})")
    db = DATA_DIR / "candidates.db"
    if db.exists() and is_world_readable(db):
        print("⚠️  candidates.db 를 다른 계정이 읽을 수 있습니다. 권한을 확인하세요.")
    print(f"지원자 {store.count()}명 / 학회·저널 미분류 {registry.unclassified_count()}건")
    print(f"http://{HOST}:{PORT}/ 에서 실행합니다. (Ctrl+C 로 종료)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
