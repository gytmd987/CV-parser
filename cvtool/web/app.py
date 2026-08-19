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

from ..audit import AuditLog
from ..auth import ROLES, AuthStore, User, can
from ..config import settings
from ..edit import CHOICE_FIELDS, READONLY_FIELDS, ConflictError, ValidationError, apply_edit, field_spec
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
from ..recruit import RECRUIT_COLUMNS, STAGES, STATUSES, RecruitStore
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
auth = AuthStore(DATA_DIR / "admin.db")
recruit = RecruitStore(DATA_DIR / "recruit.db")
audit = AuditLog(DATA_DIR / "audit.db")


def bootstrap_admin() -> str | None:
    """계정이 하나도 없으면 관리자를 만든다.

    예전처럼 CVTOOL_WEB_PASSWORD 만 설정해 두었어도 그대로 쓸 수 있게,
    그 값을 admin 계정의 비밀번호로 삼는다.
    """
    if auth.count():
        return None
    pw = os.environ.get("CVTOOL_ADMIN_PASSWORD") or WEB_PASSWORD
    if not pw:
        return None
    아이디 = os.environ.get("CVTOOL_ADMIN_ID", "admin")
    auth.create_user(아이디, "관리자", pw, "관리자", 생성자="(최초 설정)")
    audit.record(아이디, "계정", 아이디, 비고="최초 관리자 계정 생성")
    return 아이디

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


def _page(title: str, body: str, nav: bool = True, me: User | None = None) -> bytes:
    미분류 = registry.unclassified_count() if nav else 0
    badge = f' <span class="pill p-미분류">{미분류}</span>' if 미분류 else ""
    링크 = ["<a href='/'>지원자</a>", "<a href='/recruit'>채용 현황</a>"]
    if can(me, "명칭_관리"):
        링크.append(f"<a href='/names?kind=학회'>명칭 관리{badge}</a>")
    if can(me, "부서과제_관리"):
        링크.append("<a href='/org'>부서·과제</a>")
    if can(me, "계정_현업추가"):
        링크.append("<a href='/users'>계정</a>")
    if me is not None:
        링크.append("<a href='/history'>변경 이력</a>")
    누구 = (
        f"<span class='muted' style='color:#94a3b8'>{html.escape(me.이름)} ({me.역할})</span> "
        if me else ""
    )
    header = (
        "<header><a href='/'>CV 분석</a>" + "".join(링크)
        + f"<span class='sp'></span>{누구}<a href='/logout'>로그아웃</a></header>"
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
        <p><input type='text' name='userid' placeholder='아이디' autofocus style='width:100%'></p>
        <p><input type='password' name='password' placeholder='비밀번호' style='width:100%'></p>
        <button type='submit' style='width:100%'>로그인</button></form>
        <p class='muted'>사내 채용 담당자 전용입니다.</p></div>""",
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


def _dashboard(me: User, q: str = "", review_only: bool = False, 년도: str = "") -> bytes:
    records = store.list_filtered(q, review_only, 년도)
    전체 = store.count()
    만료 = store.expired_count()
    연도맵 = store.year_map()
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
    연도목록 = store.years()
    연도선택 = "".join(
        f"<option value='{y}'{' selected' if y == 년도 else ''}>{y}년</option>"
        for y in 연도목록
    )

    COLS = table_columns(registry)
    head = "".join(f"<th>{html.escape(c)}</th>" for c in COLS)
    body_rows = []
    for rec in records:
        row = rec.to_row(registry)
        cells = [
            f"<td><input type='checkbox' name='ids' value='{html.escape(rec.지원자_ID)}'></td>",
            f"<td><a href='/candidate?id={urllib.parse.quote(rec.지원자_ID)}'>상세</a></td>",
            f"<td class='muted'>{html.escape(연도맵.get(rec.지원자_ID, ''))}</td>",
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
            </th><th></th><th>등록년도</th>{head}</tr>
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
          <form method='post' action='/candidate/new' style='margin-top:10px'>
            <button type='submit' class='sec'>CV 없이 지원자 추가</button>
            <span class='muted'>다른 지원서로 지원한 경우. 빈 칸을 직접 채웁니다.</span>
          </form>
          <p class='muted'>원문 텍스트 보관: <b>{보관}</b> · 보관 기간 {settings.retention_months}개월</p>
        </div>
        {_status_table()}
        <div class='card'>
          <h2>지원자 {len(records)}명{f' / 전체 {전체}명' if len(records) != 전체 else ''}</h2>
          <form method='get' action='/' style='margin-bottom:12px'>
            <input type='text' name='q' value='{html.escape(q)}' placeholder='이름·소속·학교·파일명 검색'>
            <select name='year'><option value=''>전체 년도</option>{연도선택}</select>
            <label class='muted'><input type='checkbox' name='review' value='1'{checked}>
              검토 필요만</label>
            <button type='submit'>검색</button>
            <a class='btn sec' href='/'>초기화</a>
          </form>
          <p><a class='btn' href='/export.xlsx'>엑셀(.xlsx) 다운로드</a>
             <a class='btn sec' href='/export.tsv'>TSV 보기(복사용)</a></p>
          {table}
        </div>""",
        me=me,
    )


def _candidate_page(지원자_ID: str, me: User, error: str = "") -> bytes:
    rec = store.get(지원자_ID)
    if rec is None:
        return _page("없음", "<div class='card'>해당 지원자를 찾을 수 없습니다.</div>")
    meta = store.meta(지원자_ID) or {}
    row = rec.to_row(registry)
    수정가능 = can(me, "지원자_수정")

    def 입력칸(항목: str, 값: str) -> str:
        spec = field_spec(항목)
        if spec.입력 == "select":
            opts = "".join(
                f"<option value='{html.escape(o)}'{' selected' if o == 값 else ''}>"
                f"{html.escape(o) or '(빈칸)'}</option>"
                for o in spec.선택지
            )
            return f"<select name='새값'>{opts}</select>"
        도움 = f" placeholder='{html.escape(spec.도움말)}'" if spec.도움말 else ""
        return f"<input type='text' name='새값' value='{html.escape(값)}' style='width:260px'{도움}>"

    항목행 = []
    for c in table_columns(registry):
        값 = str(row.get(c, "") or "")
        보기 = html.escape(값) or "<span class='muted'>-</span>"
        if not 수정가능 or c in READONLY_FIELDS or c.startswith("1저자_해외논문_"):
            항목행.append(
                f"<tr><th style='width:170px'>{html.escape(c)}</th>"
                f"<td style='white-space:normal;max-width:none'>{보기}</td></tr>"
            )
            continue
        원본값 = str(getattr(rec, c, "") or "")
        항목행.append(
            f"<tr><th style='width:170px'>{html.escape(c)}</th>"
            f"<td style='white-space:normal;max-width:none'>"
            f"<form method='post' action='/candidate/edit' style='display:flex;gap:6px'>"
            f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
            f"<input type='hidden' name='항목' value='{html.escape(c)}'>"
            f"<input type='hidden' name='이전값' value='{html.escape(원본값)}'>"
            f"{입력칸(c, 원본값)}<button type='submit'>저장</button></form></td></tr>"
        )

    년도 = store.year_of(지원자_ID)
    년도폼 = (
        f"<form method='post' action='/candidate/year' style='display:flex;gap:6px'>"
        f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
        f"<input type='text' name='년도' value='{html.escape(년도)}' style='width:90px'"
        f" placeholder='YYYY'><button type='submit'>저장</button></form>"
        if 수정가능
        else html.escape(년도)
    )

    관리 = (
        f"<tr><th style='width:170px'>등록 년도</th><td>{년도폼}</td></tr>"
        f"<tr><th>원본 파일명</th><td>{html.escape(meta.get('원본_파일명') or '-')}</td></tr>"
        f"<tr><th>등록 일시</th><td>{html.escape(meta.get('등록일시') or '-')}</td></tr>"
        f"<tr><th>원본 파일 보관</th><td>{'예' if meta.get('원본보유') else '아니오'}</td></tr>"
    )
    중복 = store.duplicate_note(지원자_ID)
    if 중복:
        관리 += f"<tr><th>중복 후보</th><td class='flag' style='white-space:normal'>{html.escape(중복)}</td></tr>"

    이력 = audit.for_target("지원자", 지원자_ID)
    이력행 = "".join(
        f"<tr><td>{html.escape(e.일시)}</td><td>{html.escape(e.사용자)}</td>"
        f"<td style='white-space:normal'>{html.escape(e.summary())}</td></tr>"
        for e in 이력
    ) or "<tr><td colspan=3 class='muted'>아직 수정 내역이 없습니다.</td></tr>"

    원본있음 = meta.get("원본보유")
    원본버튼 = (
        f"<a class='btn' href='/candidate/file?id={urllib.parse.quote(지원자_ID)}'>원본 다운로드</a> "
        if 원본있음 else ""
    )
    재분석 = (
        "<form method='post' action='/candidate/reanalyze' style='display:inline'>"
        f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
        "<button type='submit'>다시 분석</button></form> "
        if 원본있음 and 수정가능 else ""
    )
    삭제 = (
        "<form method='post' action='/candidate/delete' style='display:inline'"
        " onsubmit=\"return confirm('이 지원자를 삭제합니다. 되돌릴 수 없습니다.')\">"
        f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
        "<button type='submit' class='danger'>삭제</button></form> "
        if can(me, "지원자_삭제") else ""
    )
    오류 = f"<div class='warn'>{html.escape(error)}</div>" if error else ""

    첨부목록 = "".join(
        f"<li><a href='/attachment?id={a['id']}'>{html.escape(a['파일명'])}</a>"
        f" <span class='muted'>{html.escape(a['올린일시'])} · {html.escape(a['올린이'] or '-')}</span>"
        + (
            " <form method='post' action='/attachment/delete' style='display:inline'"
            " onsubmit=\"return confirm('첨부파일을 삭제합니다.')\">"
            f"<input type='hidden' name='id' value='{a['id']}'>"
            f"<input type='hidden' name='cid' value='{html.escape(지원자_ID)}'>"
            "<button class='danger'>삭제</button></form>"
            if 수정가능 else ""
        )
        + "</li>"
        for a in store.attachments(지원자_ID)
    ) or "<li class='muted'>첨부파일 없음</li>"
    올리기 = (
        "<form method='post' action='/attachment/add' enctype='multipart/form-data'"
        " style='display:flex;gap:8px;margin-top:10px'>"
        f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
        "<input type='file' name='files' multiple>"
        "<button type='submit'>첨부 추가</button></form>"
        if 수정가능 else ""
    )
    첨부카드 = (
        f"<div class='card'><h2>첨부파일</h2><ul>{첨부목록}</ul>{올리기}"
        "<p class='muted'>CV 원본과 별개로 자기소개서·포트폴리오 등을 붙일 수 있습니다. "
        "지원자를 삭제하면 함께 지워집니다.</p></div>"
    )

    return _page(
        f"지원자 {rec.한글_이름 or rec.지원자_ID}",
        f"""{오류}
        <div class='card'>
          <h2>{html.escape(rec.한글_이름 or '(이름 미상)')}
              <span class='muted'>{html.escape(rec.지원자_ID)}</span></h2>
          <p>{원본버튼}{재분석}{삭제}<a class='btn sec' href='/'>목록으로</a></p>
          {'<p class=muted>수정 권한이 없어 읽기 전용입니다.</p>' if not 수정가능 else ''}
        </div>
        <div class='card'><h2>관리 정보</h2><table>{관리}</table></div>
        <div class='card'><h2>추출 결과 {'(칸을 고치고 저장을 누르세요)' if 수정가능 else ''}</h2>
          <table>{''.join(항목행)}</table></div>
        {첨부카드}
        <div class='card'><h2>변경 이력</h2><div class='scroll'>
          <table><tr><th>일시</th><th>사용자</th><th>내용</th></tr>{이력행}</table>
        </div></div>""",
        me=me,
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



def _users_page(me: User, error: str = "") -> bytes:
    """계정 관리. 관리자는 전원, 채용담당자는 현업만 추가할 수 있다."""
    users = auth.list_users()
    projects = auth.projects()
    추가가능 = ROLES if me.is_admin else ("현업",)

    rows = []
    for u in users:
        배정 = auth.project_ids_of(u.아이디) if u.역할 == "현업" else set()
        과제표시 = ", ".join(
            f"{p['부서명']}/{p['이름']}" for p in projects if p["id"] in 배정
        ) or ("-" if u.역할 == "현업" else "")
        수정가능 = me.is_admin or u.역할 == "현업"
        조작 = ""
        if 수정가능 and u.아이디 != me.아이디:
            라벨 = "비활성화" if u.활성 else "활성화"
            조작 = (
                "<form method='post' action='/users/toggle' style='display:inline'>"
                f"<input type='hidden' name='id' value='{html.escape(u.아이디)}'>"
                f"<button class='sec'>{라벨}</button></form> "
                "<form method='post' action='/users/delete' style='display:inline'"
                " onsubmit=\"return confirm('계정을 삭제합니다.')\">"
                f"<input type='hidden' name='id' value='{html.escape(u.아이디)}'>"
                "<button class='danger'>삭제</button></form>"
            )
        상태 = "활성" if u.활성 else "<span class='flag'>비활성</span>"
        rows.append(
            f"<tr><td>{html.escape(u.아이디)}</td><td>{html.escape(u.이름)}</td>"
            f"<td>{u.역할}</td><td>{상태}</td><td>{html.escape(과제표시)}</td>"
            f"<td class='muted'>{html.escape(u.생성일시)} ({html.escape(u.생성자 or '-')})</td>"
            f"<td>{조작}</td></tr>"
        )

    역할옵션 = "".join(f"<option>{r}</option>" for r in 추가가능)
    과제옵션 = "".join(
        f"<option value='{p['id']}'>{html.escape(p['부서명'])} / {html.escape(p['이름'])}</option>"
        for p in projects
    )
    오류 = f"<p class='flag'>{html.escape(error)}</p>" if error else ""
    안내 = (
        "관리자는 모든 역할을 만들 수 있습니다."
        if me.is_admin
        else "채용담당자는 <b>현업 계정만</b> 만들 수 있습니다."
    )
    return _page(
        "계정 관리",
        "<div class='card'><h2>계정 추가</h2>" + 오류
        + "<form method='post' action='/users/add' style='display:flex;gap:8px;flex-wrap:wrap'>"
        "<input type='text' name='userid' placeholder='아이디' required>"
        "<input type='text' name='name' placeholder='이름'>"
        "<input type='password' name='password' placeholder='비밀번호(4자 이상)' required>"
        f"<select name='role'>{역할옵션}</select>"
        f"<select name='project'><option value=''>과제 배정(현업만)</option>{과제옵션}</select>"
        "<button type='submit'>추가</button></form>"
        f"<p class='muted'>{안내} 현업은 배정된 과제의 지원자만 볼 수 있습니다.</p></div>"
        f"<div class='card'><h2>계정 {len(users)}개</h2><div class='scroll'>"
        "<table><tr><th>아이디</th><th>이름</th><th>역할</th><th>상태</th>"
        "<th>배정 과제</th><th>생성</th><th></th></tr>"
        + "".join(rows) + "</table></div></div>",
        me=me,
    )


def _org_page(me: User, error: str = "") -> bytes:
    """부서 · 과제 관리. 과제는 부서에 속한다."""
    depts = auth.departments()
    projects = auth.projects()

    카드 = []
    for d in depts:
        소속 = [p for p in projects if p["부서_id"] == d["id"]]
        항목 = "".join(
            f"<li>{html.escape(p['이름'])}"
            + (" <span class='muted'>(초대암호 있음)</span>" if p["초대암호"] else "")
            + " <form method='post' action='/org/project/delete' style='display:inline'"
            " onsubmit=\"return confirm('과제를 삭제합니다. 배정도 함께 지워집니다.')\">"
            f"<input type='hidden' name='id' value='{p['id']}'>"
            "<button class='danger'>삭제</button></form></li>"
            for p in 소속
        ) or "<li class='muted'>과제 없음</li>"
        카드.append(
            f"<div class='card'><h2>{html.escape(d['이름'])}"
            f" <span class='muted'>과제 {len(소속)}개</span></h2><ul>{항목}</ul>"
            "<form method='post' action='/org/project/add' style='display:flex;gap:8px'>"
            f"<input type='hidden' name='dept' value='{d['id']}'>"
            "<input type='text' name='name' placeholder='과제 이름' required>"
            "<input type='password' name='invite' placeholder='초대암호(선택)'>"
            "<button type='submit'>과제 추가</button></form></div>"
        )

    오류 = f"<p class='flag'>{html.escape(error)}</p>" if error else ""
    본문 = (
        "<div class='card'><h2>부서 추가</h2>" + 오류
        + "<form method='post' action='/org/dept/add' style='display:flex;gap:8px'>"
        "<input type='text' name='name' placeholder='부서 이름' required>"
        "<button type='submit'>추가</button></form>"
        "<p class='muted'>과제는 부서에 속합니다. 표에서 부서를 고르면 그 부서의 과제만 나옵니다.</p>"
        "</div>"
        + ("".join(카드) or "<div class='card muted'>부서를 먼저 추가하세요.</div>")
    )
    return _page("부서·과제 관리", 본문, me=me)


def _history_page(me: User, 대상종류: str = "", limit: int = 300) -> bytes:
    entries = audit.recent(limit, 대상종류=대상종류)
    rows = "".join(
        f"<tr><td>{html.escape(e.일시)}</td><td>{html.escape(e.사용자)}</td>"
        f"<td>{html.escape(e.대상종류)}</td><td>{html.escape(e.대상)}</td>"
        f"<td style='white-space:normal'>{html.escape(e.summary())}</td></tr>"
        for e in entries
    ) or "<tr><td colspan='5' class='muted'>이력이 없습니다.</td></tr>"
    탭 = " ".join(
        f"<a class='btn {'' if k == 대상종류 else 'sec'}'"
        f" href='/history?kind={urllib.parse.quote(k)}'>{k or '전체'}</a>"
        for k in ("", "지원자", "계정", "명칭", "과제", "로그인")
    )
    return _page(
        "변경 이력",
        f"<div class='card'><h2>변경 이력 <span class='muted'>총 {audit.count()}건</span></h2>"
        f"<p>{탭}</p><div class='scroll'><table>"
        "<tr><th>일시</th><th>사용자</th><th>종류</th><th>대상</th><th>내용</th></tr>"
        f"{rows}</table></div></div>",
        me=me,
    )



def _recruit_page(me: User, sort: str = "", error: str = "") -> bytes:
    """채용 현황 관리.

    지원자마다 단계별 상태를 드롭다운으로 바꾼다. 현업은 자기 과제만 보인다.
    기본 정렬은 불합격을 맨 아래로 내린다.
    """
    보이는과제 = auth.visible_project_ids(me)      # None 이면 전부
    진행맵 = recruit.all()
    depts = auth.departments()
    projects = auth.projects()
    부서명 = {d["id"]: d["이름"] for d in depts}
    과제명 = {p["id"]: p["이름"] for p in projects}

    records = store.list_all()
    if 보이는과제 is not None:
        records = [
            r for r in records
            if (진행맵.get(r.지원자_ID) and 진행맵[r.지원자_ID].project_id in 보이는과제)
        ]

    표열 = recruit.columns()
    수정가능 = can(me, "채용현황_수정")
    담당자 = can(me, "지원자_수정")

    def 값(rec, col: str) -> str:
        p = 진행맵.get(rec.지원자_ID)
        if col == "부서":
            return 부서명.get(p.부서_id, "") if p else ""
        if col == "과제":
            return 과제명.get(p.project_id, "") if p else ""
        if col == "최종상태":
            return p.최종상태 if p else "미시작"
        if col == "비고":
            return p.비고 if p else ""
        if col in STAGES:
            return (p.단계상태.get(col, "") if p else "")
        return str(rec.to_row(registry).get(col, "") or "")

    # 정렬: 기본은 불합격 아래로. 열 제목을 누르면 그 열 기준
    def 정렬키(rec):
        p = 진행맵.get(rec.지원자_ID)
        기본 = p.정렬키() if p else (0, 0, 0)
        if sort:
            return (기본[0], 값(rec, sort).lower())   # 불합격은 어떤 정렬에서도 아래로
        return 기본
    records.sort(key=정렬키)

    부서옵션 = "".join(f"<option value='{d['id']}'>{html.escape(d['이름'])}</option>" for d in depts)
    과제_by_부서 = {}
    for pr in projects:
        과제_by_부서.setdefault(pr["부서_id"], []).append(pr)

    rows = []
    for rec in records:
        p = 진행맵.get(rec.지원자_ID)
        cells = []
        for col in 표열:
            v = html.escape(값(rec, col))
            if col in STAGES and 수정가능:
                opts = "".join(
                    f"<option value='{html.escape(st)}'"
                    f"{' selected' if st == 값(rec, col) else ''}>{html.escape(st) or '-'}</option>"
                    for st in STATUSES
                )
                cells.append(
                    f"<td><form method='post' action='/recruit/stage' style='display:inline'>"
                    f"<input type='hidden' name='id' value='{html.escape(rec.지원자_ID)}'>"
                    f"<input type='hidden' name='단계' value='{html.escape(col)}'>"
                    f"<select name='상태' onchange='this.form.submit()'>{opts}</select>"
                    f"</form></td>"
                )
            elif col == "부서" and 담당자:
                현재부서 = p.부서_id if p else None
                현재과제 = p.project_id if p else None
                옵션 = "".join(
                    f"<option value='{d['id']}'{' selected' if d['id'] == 현재부서 else ''}>"
                    f"{html.escape(d['이름'])}</option>" for d in depts
                )
                과제옵션 = "".join(
                    f"<option value='{pr['id']}'{' selected' if pr['id'] == 현재과제 else ''}>"
                    f"{html.escape(pr['이름'])}</option>"
                    for pr in 과제_by_부서.get(현재부서, [])
                )
                cells.append(
                    f"<td><form method='post' action='/recruit/assign' style='display:flex;gap:4px'>"
                    f"<input type='hidden' name='id' value='{html.escape(rec.지원자_ID)}'>"
                    f"<select name='dept' onchange='this.form.submit()'>"
                    f"<option value=''>-</option>{옵션}</select>"
                    f"<select name='project' onchange='this.form.submit()'>"
                    f"<option value=''>-</option>{과제옵션}</select></form></td>"
                )
            elif col == "과제" and 담당자:
                continue   # 부서 칸에서 함께 고른다
            elif col == "비고" and 수정가능:
                cells.append(
                    f"<td><form method='post' action='/recruit/note' style='display:flex;gap:4px'>"
                    f"<input type='hidden' name='id' value='{html.escape(rec.지원자_ID)}'>"
                    f"<input type='text' name='비고' value='{v}' style='width:160px'>"
                    f"<button type='submit'>저장</button></form></td>"
                )
            elif col == "최종상태":
                cls = " class='flag'" if p and p.탈락 else ""
                cells.append(f"<td{cls}>{v}</td>")
            else:
                cells.append(f"<td title='{v}'>{v}</td>")
        링크 = f"<td><a href='/candidate?id={urllib.parse.quote(rec.지원자_ID)}'>상세</a></td>"
        묶음 = " class='dup'" if p and p.탈락 else ""
        rows.append(f"<tr{묶음}>{링크}{''.join(cells)}</tr>")

    머리 = "<th></th>" + "".join(
        f"<th><a href='/recruit?sort={urllib.parse.quote(c)}' style='color:inherit'>"
        f"{html.escape(c)}</a></th>"
        for c in 표열
        if not (c == "과제" and 담당자)
    )
    오류 = f"<div class='warn'>{html.escape(error)}</div>" if error else ""
    안내 = (
        "배정된 과제의 지원자만 보입니다."
        if 보이는과제 is not None
        else "열 제목을 누르면 그 열로 정렬됩니다. 불합격자는 항상 아래로 갑니다."
    )
    열구성 = (
        "<p><a class='btn sec' href='/recruit/columns'>표 열 구성</a></p>"
        if can(me, "열_구성") else ""
    )
    표 = (
        f"<div class='scroll'><table><tr>{머리}</tr>{''.join(rows)}</table></div>"
        if rows else "<p class='muted'>표시할 지원자가 없습니다.</p>"
    )
    return _page(
        "채용 현황",
        f"""{오류}<div class='card'><h2>채용 현황 <span class='muted'>{len(records)}명</span></h2>
        <p class='muted'>{안내}</p>{열구성}{표}</div>""",
        me=me,
    )


def _recruit_columns_page(me: User) -> bytes:
    """관리자가 채용 현황 표에 보일 열과 순서를 정한다."""
    전체 = list(table_columns(registry)) + list(RECRUIT_COLUMNS)
    현재 = recruit.columns()
    항목 = "".join(
        f"<label style='display:block;padding:3px 0'>"
        f"<input type='checkbox' name='col' value='{html.escape(c)}'"
        f"{' checked' if c in 현재 else ''}> {html.escape(c)}</label>"
        for c in 전체
    )
    순서 = ", ".join(현재)
    return _page(
        "표 열 구성",
        f"""<div class='card'><h2>채용 현황 표에 보일 열</h2>
        <p class='muted'>체크한 열만 보입니다. 순서는 아래 칸에 쉼표로 적은 순서를 따릅니다.</p>
        <form method='post' action='/recruit/columns'>
          <div style='columns:3'>{항목}</div>
          <p>순서(쉼표 구분, 비우면 체크 순서대로):<br>
          <input type='text' name='order' value='{html.escape(순서)}' style='width:100%'></p>
          <button type='submit'>저장</button>
          <a class='btn sec' href='/recruit'>취소</a>
        </form></div>""",
        me=me,
    )


# ---------------------------------------------------------------------------
# HTTP 핸들러
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "cvtool"

    def log_message(self, fmt: str, *args) -> None:  # 접근 로그 간소화
        print(f"[{now_kst().strftime('%H:%M:%S')}] {fmt % args}")

    # -- 유틸 ---------------------------------------------------------------
    def _token(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "cvsession":
                return v
        return ""

    def _user(self) -> User | None:
        return auth.user_for_session(self._token())

    def _session_ok(self) -> bool:
        return self._user() is not None

    def _deny(self, 이유: str = "권한이 없습니다.") -> None:
        self._send(
            _page("권한 없음", f"<div class='card'><h2>권한 없음</h2><p>{html.escape(이유)}</p>"
                  "<p><a class='btn sec' href='/'>돌아가기</a></p></div>"),
            code=403,
        )

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
            auth.end_session(self._token())
            return self._redirect("/login")

        me = self._user()
        if me is None:
            return self._redirect("/login")

        if path == "/":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(
                _dashboard(
                    me,
                    q=(params.get("q") or [""])[0],
                    review_only=bool(params.get("review")),
                    년도=(params.get("year") or [""])[0],
                )
            )
        if path == "/candidate":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cid = (params.get("id") or [""])[0]
            return self._send(_candidate_page(cid, me, (params.get("err") or [""])[0]))
        if path == "/users":
            if not can(me, "계정_현업추가"):
                return self._deny()
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(_users_page(me, (params.get("err") or [""])[0]))
        if path == "/org":
            if not can(me, "부서과제_관리"):
                return self._deny()
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(_org_page(me, (params.get("err") or [""])[0]))
        if path == "/history":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(_history_page(me, (params.get("kind") or [""])[0]))
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
        if path == "/recruit":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(
                _recruit_page(me, (params.get("sort") or [""])[0],
                              (params.get("err") or [""])[0])
            )
        if path == "/recruit/columns":
            if not can(me, "열_구성"):
                return self._deny("표 열 구성은 관리자만 바꿀 수 있습니다.")
            return self._send(_recruit_columns_page(me))
        if path == "/attachment":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                att = store.attachment(int((params.get("id") or ["0"])[0]))
            except ValueError:
                att = None
            if not att:
                return self._send(_page("없음", "<div class='card'>첨부파일이 없습니다.</div>"),
                                  code=404)
            fpath = store.files_dir / att["저장명"]
            if not fpath.is_file():
                return self._send(_page("없음", "<div class='card'>파일이 사라졌습니다.</div>"),
                                  code=404)
            ctype = CONTENT_TYPES.get(fpath.suffix.lower(), "application/octet-stream")
            quoted = urllib.parse.quote(att["파일명"])
            return self._send(
                fpath.read_bytes(), ctype,
                extra={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
            )
        if path == "/names":
            if not can(me, "명칭_관리"):
                return self._deny()
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
            아이디 = (data.get("userid") or [""])[0].strip()
            pw = (data.get("password") or [""])[0]
            if not auth.count():
                return self._send(
                    _login_page("계정이 하나도 없습니다. 서버 콘솔 안내를 확인하세요.")
                )
            user = auth.authenticate(아이디, pw)
            if user is None:
                audit.record(아이디 or "(빈칸)", "로그인", 아이디 or "-", 비고="로그인 실패")
                return self._send(_login_page("아이디 또는 비밀번호가 틀렸습니다."))
            token = auth.start_session(user.아이디)
            audit.record(user.아이디, "로그인", user.아이디, 비고="로그인")
            return self._redirect(
                "/", {"Set-Cookie": f"cvsession={token}; HttpOnly; Path=/; SameSite=Strict"}
            )

        me = self._user()
        if me is None:
            return self._redirect("/login")

        if path == "/upload":
            if not can(me, "지원자_등록"):
                return self._deny()
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

        if path == "/recruit/stage":
            if not can(me, "채용현황_수정"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            cid = (data.get("id") or [""])[0]
            단계 = (data.get("단계") or [""])[0]
            상태 = (data.get("상태") or [""])[0]
            보이는 = auth.visible_project_ids(me)
            if 보이는 is not None and recruit.get(cid).project_id not in 보이는:
                return self._deny("배정된 과제의 지원자만 수정할 수 있습니다.")
            try:
                이전 = recruit.set_stage(cid, 단계, 상태, me.아이디)
            except ValueError as exc:
                return self._redirect("/recruit?err=" + urllib.parse.quote(str(exc)))
            if 이전 != 상태:
                audit.record(me.아이디, "채용현황", cid, 항목=단계, 이전값=이전, 새값=상태)
            return self._redirect("/recruit")

        if path == "/recruit/assign":
            if not can(me, "지원자_수정"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            cid = (data.get("id") or [""])[0]
            dept = (data.get("dept") or [""])[0]
            proj = (data.get("project") or [""])[0]
            부서_id = int(dept) if dept.isdigit() else None
            project_id = int(proj) if proj.isdigit() else None
            # 부서를 바꾸면 그 부서에 속하지 않는 과제는 떨어뜨린다
            if project_id is not None:
                소속 = {pr["id"] for pr in auth.projects(부서_id)} if 부서_id else set()
                if project_id not in 소속:
                    project_id = None
            옛부서, 옛과제 = recruit.set_assignment(cid, 부서_id, project_id, me.아이디)
            if (옛부서, 옛과제) != (부서_id, project_id):
                audit.record(me.아이디, "채용현황", cid, 항목="부서/과제",
                             이전값=f"{옛부서}/{옛과제}", 새값=f"{부서_id}/{project_id}")
            return self._redirect("/recruit")

        if path == "/recruit/note":
            if not can(me, "채용현황_수정"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            cid = (data.get("id") or [""])[0]
            비고 = (data.get("비고") or [""])[0]
            보이는 = auth.visible_project_ids(me)
            if 보이는 is not None and recruit.get(cid).project_id not in 보이는:
                return self._deny("배정된 과제의 지원자만 수정할 수 있습니다.")
            이전 = recruit.set_note(cid, 비고, me.아이디)
            if 이전 != 비고:
                audit.record(me.아이디, "채용현황", cid, 항목="비고", 이전값=이전, 새값=비고)
            return self._redirect("/recruit")

        if path == "/recruit/columns":
            if not can(me, "열_구성"):
                return self._deny("표 열 구성은 관리자만 바꿀 수 있습니다.")
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            고른것 = data.get("col") or []
            순서문 = (data.get("order") or [""])[0]
            if 순서문.strip():
                원하는 = [c.strip() for c in 순서문.split(",") if c.strip()]
                최종 = [c for c in 원하는 if c in 고른것] + [c for c in 고른것 if c not in 원하는]
            else:
                최종 = 고른것
            recruit.set_columns(최종)
            audit.record(me.아이디, "채용현황", "(표 열)", 비고=f"열 구성 변경: {', '.join(최종)}")
            return self._redirect("/recruit")

        if path == "/candidate/new":
            if not can(me, "지원자_등록"):
                return self._deny()
            rec = store.create_blank()
            audit.record(me.아이디, "지원자", rec.지원자_ID, 비고="CV 없이 직접 등록")
            return self._redirect(f"/candidate?id={urllib.parse.quote(rec.지원자_ID)}")

        if path == "/attachment/add":
            if not can(me, "지원자_수정"):
                return self._deny()
            form = parse_multipart(self._read_body(), self.headers.get("Content-Type", ""))
            cid = (form.fields.get("id") or "").strip()
            뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
            for f in form.files:
                이름 = safe_filename(f.filename)
                try:
                    store.add_attachment(cid, 이름, f.content, me.아이디)
                    audit.record(me.아이디, "지원자", cid, 항목="첨부파일", 새값=이름)
                except ValueError as exc:
                    return self._redirect(f"{뒤로}&err={urllib.parse.quote(str(exc))}")
            return self._redirect(뒤로)

        if path == "/attachment/delete":
            if not can(me, "지원자_수정"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            cid = (data.get("cid") or [""])[0]
            try:
                이름 = store.delete_attachment(int((data.get("id") or ["0"])[0]))
            except ValueError:
                이름 = ""
            if 이름:
                audit.record(me.아이디, "지원자", cid, 항목="첨부파일 삭제", 이전값=이름)
            return self._redirect(f"/candidate?id={urllib.parse.quote(cid)}")

        if path == "/candidate/edit":
            if not can(me, "지원자_수정"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            cid = (data.get("id") or [""])[0]
            항목 = (data.get("항목") or [""])[0]
            새값 = (data.get("새값") or [""])[0]
            이전값 = (data.get("이전값") or [""])[0]
            rec = store.get(cid)
            뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
            if rec is None:
                return self._redirect(뒤로)
            try:
                옛값, 저장값 = apply_edit(rec, 항목, 새값, 기대_이전값=이전값)
            except (ValidationError, ConflictError) as exc:
                return self._redirect(f"{뒤로}&err={urllib.parse.quote(str(exc))}")
            if 옛값 != 저장값:
                store.save(rec)
                audit.record(me.아이디, "지원자", cid, 항목=항목, 이전값=옛값, 새값=저장값)
            return self._redirect(뒤로)

        if path == "/candidate/year":
            if not can(me, "지원자_수정"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            cid = (data.get("id") or [""])[0]
            년도 = (data.get("년도") or [""])[0]
            뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
            옛 = store.year_of(cid)
            try:
                store.set_year(cid, 년도)
            except ValueError as exc:
                return self._redirect(f"{뒤로}&err={urllib.parse.quote(str(exc))}")
            if 옛 != 년도:
                audit.record(me.아이디, "지원자", cid, 항목="등록년도", 이전값=옛, 새값=년도)
            return self._redirect(뒤로)

        if path == "/users/add":
            if not can(me, "계정_현업추가"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            역할 = (data.get("role") or ["현업"])[0]
            if 역할 != "현업" and not can(me, "계정_전체관리"):
                return self._redirect("/users?err=" + urllib.parse.quote(
                    "채용담당자는 현업 계정만 만들 수 있습니다."))
            try:
                u = auth.create_user(
                    (data.get("userid") or [""])[0],
                    (data.get("name") or [""])[0],
                    (data.get("password") or [""])[0],
                    역할,
                    생성자=me.아이디,
                )
            except ValueError as exc:
                return self._redirect("/users?err=" + urllib.parse.quote(str(exc)))
            과제 = (data.get("project") or [""])[0]
            if 과제 and u.역할 == "현업":
                auth.assign(u.아이디, int(과제))
            audit.record(me.아이디, "계정", u.아이디, 비고=f"{u.역할} 계정 생성")
            return self._redirect("/users")

        if path == "/users/toggle":
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            대상 = auth.get_user((data.get("id") or [""])[0])
            if 대상 is None or 대상.아이디 == me.아이디:
                return self._redirect("/users")
            if not (can(me, "계정_전체관리") or 대상.역할 == "현업"):
                return self._deny()
            auth.set_active(대상.아이디, not 대상.활성)
            if 대상.활성:
                auth.end_all_sessions(대상.아이디)
            audit.record(me.아이디, "계정", 대상.아이디,
                         비고="비활성화" if 대상.활성 else "활성화")
            return self._redirect("/users")

        if path == "/users/delete":
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            대상 = auth.get_user((data.get("id") or [""])[0])
            if 대상 is None or 대상.아이디 == me.아이디:
                return self._redirect("/users")
            if not (can(me, "계정_전체관리") or 대상.역할 == "현업"):
                return self._deny()
            auth.delete_user(대상.아이디)
            audit.record(me.아이디, "계정", 대상.아이디, 비고="계정 삭제")
            return self._redirect("/users")

        if path == "/org/dept/add":
            if not can(me, "부서과제_관리"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            try:
                auth.add_department((data.get("name") or [""])[0])
            except ValueError as exc:
                return self._redirect("/org?err=" + urllib.parse.quote(str(exc)))
            audit.record(me.아이디, "과제", (data.get("name") or [""])[0], 비고="부서 추가")
            return self._redirect("/org")

        if path == "/org/project/add":
            if not can(me, "부서과제_관리"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            try:
                auth.add_project(
                    int((data.get("dept") or ["0"])[0]),
                    (data.get("name") or [""])[0],
                    (data.get("invite") or [""])[0],
                )
            except (ValueError, TypeError) as exc:
                return self._redirect("/org?err=" + urllib.parse.quote(str(exc)))
            audit.record(me.아이디, "과제", (data.get("name") or [""])[0], 비고="과제 추가")
            return self._redirect("/org")

        if path == "/org/project/delete":
            if not can(me, "부서과제_관리"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            try:
                auth.delete_project(int((data.get("id") or ["0"])[0]))
            except (ValueError, TypeError):
                pass
            return self._redirect("/org")

        if path == "/candidate/delete":
            if not can(me, "지원자_삭제"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            cid = (data.get("id") or [""])[0]
            if cid:
                store.delete(cid)
                recruit.delete(cid)
                audit.record(me.아이디, "지원자", cid, 비고="지원자 삭제")
            return self._redirect("/")

        if path == "/candidates/delete":
            if not can(me, "지원자_삭제"):
                return self._deny()
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
            if not can(me, "명칭_관리"):
                return self._deny()
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
            if not can(me, "명칭_관리"):
                return self._deny()
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
            if not can(me, "열_구성"):
                return self._deny("표 열 구성은 관리자만 바꿀 수 있습니다.")
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
    새관리자 = bootstrap_admin()
    if 새관리자:
        print(f"✅ 최초 관리자 계정을 만들었습니다: 아이디 '{새관리자}'")
        print("   비밀번호는 CVTOOL_ADMIN_PASSWORD (없으면 CVTOOL_WEB_PASSWORD) 값입니다.")
    elif not auth.count():
        print("⚠️  계정이 하나도 없고 비밀번호 설정도 없어 로그인할 수 없습니다.")
        print("   .env 에 CVTOOL_ADMIN_PASSWORD 를 넣고 다시 실행하세요.")
    else:
        print(f"계정 {auth.count()}개 / 변경 이력 {audit.count()}건")

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
