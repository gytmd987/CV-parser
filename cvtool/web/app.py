"""사내 지원자 관리 웹 앱 (표준 라이브러리 http.server).

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
import json
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
from ..edit import (
    CHOICE_FIELDS,
    READONLY_FIELDS,
    REGISTRY_FIELDS,
    ConflictError,
    ValidationError,
    apply_edit,
    custom_field_spec,
    field_spec,
    validate_custom,
)
from ..dotenv import LOADED_FROM, candidate_paths
from ..export import build_xlsx, records_to_xlsx
from ..fsutil import is_world_readable, mode_of, safe_filename, secure_dir, secure_file
from ..extract import extract_cv_from_text
from ..ingestion.parsers import UnsupportedFormat, extract_text
from ..normalize import MULTI_SEP
from ..schemas import NAME_COLUMNS, TIER_COLUMN_PREFIX
from ..schemas import columns as table_columns
from ..store import CUSTOM_TYPES, SUPPORTED_SUFFIXES, CandidateStore
from ..timeutil import now_kst
from ..dedup import fingerprint, find_duplicates
from ..names import (
    GRADED_KINDS,
    KINDS,
    SUBTYPES,
    NameRegistry,
    canonical_kind,
    observe_record,
)
from ..mailing import MailStore, Template, html_to_text, render
from ..clients import mailer as mailapi
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
mailing = MailStore(DATA_DIR / "mail.db", DATA_DIR / "mail_files")


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
header .brand{color:#fff;font-weight:700;margin-right:6px;padding-right:14px;border-right:1px solid #3a4149}
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
.p-겹침{background:#fef3c7;color:#92400e}
.dup{background:#fff1f2}
td.edit{cursor:cell}
td.edit:hover{outline:2px solid var(--accent);outline-offset:-2px}
td.saved{background:#dcfce7 !important}
td.err{background:#fee2e2 !important}
td.edit input,td.edit select{padding:2px 4px;font-size:12.5px;width:100%}
td.ctl,th.ctl{white-space:normal;max-width:none;overflow:visible}
.mergebar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;background:#eff6ff;
 border:1px solid #bfdbfe;border-radius:6px;padding:10px 12px;margin:0 0 12px}
.mergebar select{min-width:280px;max-width:100%}
.mergebar b{color:#1d4ed8}
tr.hide{display:none}
input.dirty,select.dirty{background:#fef3c7;border-color:#fcd34d}
.tbar{display:flex;gap:8px;align-items:center;margin:0 0 8px}
.tbar input.tfilter{width:220px}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{background:#dbeafe}
th[data-dir=asc]::after{content:' ↑';font-size:11px;color:var(--accent)}
th[data-dir=desc]::after{content:' ↓';font-size:11px;color:var(--accent)}
th.filtered{background:#dbeafe}
th.filtered::after{content:' (추림)';font-size:10px;color:var(--accent)}
#colmenu{position:absolute;z-index:100;background:#fff;border:1px solid var(--line);
 border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.14);padding:6px;min-width:230px;
 max-width:320px;font-size:13px}
#colmenu .cm-head{font-weight:700;padding:4px 8px;color:var(--muted);
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#colmenu button{display:block;width:100%;text-align:left;background:none;color:var(--txt);
 padding:6px 8px;border-radius:6px;font-size:13px}
#colmenu button:hover{background:#eff6ff}
#colmenu .cm-btns{display:flex;gap:6px;padding:4px 0}
#colmenu .cm-btns button{background:var(--accent);color:#fff;text-align:center}
#colmenu .cm-btns button.sec{background:#4b5563}
#colmenu .cm-sep{border-top:1px solid var(--line);margin:5px 0}
#colmenu .cm-title{font-weight:700;padding:2px 8px}
#colmenu .cm-q{width:100%;margin:4px 0;padding:5px 7px;font-size:13px}
#colmenu .cm-list{max-height:200px;overflow:auto;border:1px solid var(--line);border-radius:6px}
#colmenu .cm-row{display:block;padding:3px 8px;cursor:pointer;white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis}
#colmenu .cm-row:hover{background:#eff6ff}
#colmenu .cm-row.hide{display:none}
#colmenu .cm-allrow{padding-left:8px}
td.sel{background:#bfdbfe !important;outline:1px solid #2563eb;outline-offset:-1px}
.rt{border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#fff}
.rt-bar{display:flex;flex-wrap:wrap;gap:2px;align-items:center;padding:6px;
 background:#f3f4f6;border-bottom:1px solid var(--line)}
.rt-bar button{background:#fff;color:var(--txt);border:1px solid var(--line);
 padding:4px 7px;font-size:13px;min-width:29px;border-radius:5px}
.rt-bar button:hover{background:#eff6ff;border-color:var(--accent)}
.rt-bar button.rt-drop{display:inline-flex;align-items:center;gap:4px;justify-content:space-between}
.rt-bar button.rt-drop span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rt-bar button.rt-drop i{font-style:normal;font-size:9px;color:var(--muted)}
.rt-bar button.rt-var{background:var(--accent);color:#fff;border-color:var(--accent)}
.rt-bar button.rt-var i{color:#fff}
.rt-bar .rt-sep{width:1px;height:20px;background:var(--line);margin:0 4px}
.rt-bar .rt-ink{display:inline-block;padding:0 3px;border-radius:3px;font-weight:700}
.rt-bar label.btnlike{display:inline-flex;align-items:center;font-size:13px;
 color:var(--txt);border:1px solid var(--line);border-radius:5px;padding:4px 7px;
 background:#fff;cursor:pointer}
.rt-bar label.btnlike:hover{background:#eff6ff;border-color:var(--accent)}
.rt-body{min-height:340px;max-height:60vh;overflow:auto;padding:16px 18px;
 font:12pt/1.7 "맑은 고딕",system-ui,sans-serif;outline:none}
.rt-body:focus{box-shadow:inset 0 0 0 2px #bfdbfe}
.rt-body table{width:auto}
.rt-body img{max-width:100%}
#rtdrop{position:absolute;z-index:120;background:#fff;border:1px solid var(--line);
 border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.16);padding:5px;font-size:13px;
 max-height:340px;overflow:auto;min-width:120px}
#rtdrop > button{display:block;width:100%;text-align:left;background:none;border:0;
 color:var(--txt);padding:5px 9px;border-radius:5px;font-size:13px;cursor:pointer}
#rtdrop > button:hover{background:#eff6ff}
#rtdrop .rt-swatch{display:grid;grid-template-columns:repeat(5,22px);gap:4px;padding:4px}
#rtdrop .rt-swatch button{width:22px;height:22px;border:1px solid var(--line);
 border-radius:4px;padding:0;cursor:pointer}
#rtdrop .rt-pick{display:flex;align-items:center;gap:6px;padding:6px 6px 2px;
 color:var(--muted);border-top:1px solid var(--line);margin-top:4px;cursor:pointer}
#rtdrop .rt-grid{display:grid;grid-template-columns:repeat(6,18px);gap:3px;padding:5px}
#rtdrop .rt-grid i{width:18px;height:16px;border:1px solid var(--line);border-radius:2px;
 background:#fff;cursor:pointer}
#rtdrop .rt-grid i.on{background:#bfdbfe;border-color:var(--accent)}
#rtdrop .rt-gridlabel{text-align:center;color:var(--muted);padding:2px 0 4px}
#rtdrop.varmenu{width:280px}
#rtdrop .vm-head{padding:4px 8px;color:var(--muted)}
#rtdrop .vm-q{width:100%;margin:4px 0;padding:6px 8px;font-size:13px}
#rtdrop .vm-list{max-height:260px;overflow:auto}
#rtdrop .vm-group{font-weight:700;color:var(--accent);padding:8px 8px 3px;
 border-top:1px solid var(--line);margin-top:4px}
#rtdrop .vm-group:first-child{border-top:0;margin-top:0}
#rtdrop .vm-item{display:block;width:100%;text-align:left;background:none;
 color:var(--txt);padding:5px 8px;border-radius:6px;font-size:13px}
#rtdrop .vm-item:hover{background:#eff6ff}
#rtdrop .hide{display:none}
.mailbody{border:1px solid var(--line);border-radius:8px;padding:14px 16px;
 background:#fff;max-height:420px;overflow:auto;font:12pt/1.7 "맑은 고딕",sans-serif}
.mailbody img{max-width:100%}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#1b1f24;
 color:#fff;padding:10px 16px;border-radius:8px;opacity:0;pointer-events:none;
 transition:opacity .15s;z-index:99}
#toast.show{opacity:.95}
.done{background:#dcfce7;border:1px solid #86efac;color:#14532d;padding:10px 14px;
 border-radius:6px;margin-bottom:14px}
"""


def _page(title: str, body: str, nav: bool = True, me: User | None = None) -> bytes:
    미분류 = registry.unclassified_count() if nav else 0
    badge = f' <span class="pill p-미분류">{미분류}</span>' if 미분류 else ""
    링크 = []
    if can(me, "지원자_목록"):
        링크.append("<a href='/'>지원자</a>")
    if can(me, "채용현황_수정") or can(me, "지원자_조회"):
        링크.append("<a href='/recruit'>채용 현황</a>")
    if can(me, "지원자_등록"):
        링크.append("<a href='/upload'>CV 업로드</a>")
    if can(me, "메일_템플릿"):
        링크.append("<a href='/mail'>메일</a>")
    if can(me, "명칭_관리"):
        링크.append(
            "<a href='/names?kind=" + urllib.parse.quote("학회·저널")
            + f"'>명칭 관리{badge}</a>"
        )
    if can(me, "부서과제_관리"):
        링크.append("<a href='/org'>부서·과제</a>")
    if can(me, "계정_현업추가"):
        링크.append("<a href='/users'>계정</a>")
    if can(me, "열_구성"):
        링크.append("<a href='/fields'>표 항목</a>")
    if can(me, "변경이력_조회"):
        링크.append("<a href='/history'>변경 이력</a>")
    누구 = (
        f"<span class='muted' style='color:#94a3b8'>{html.escape(me.이름)} ({me.역할})</span> "
        if me else ""
    )
    header = (
        "<header><span class='brand'>지원자 관리</span>" + "".join(링크)
        + f"<span class='sp'></span>{누구}<a href='/logout'>로그아웃</a></header>"
        if nav
        else ""
    )
    return (
        f"<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{header}<main>{body}</main>"
        + (f"<script>{_TABLE_JS}{_INLINE_JS}</script>" if nav else "")
        + "</body></html>"
    ).encode("utf-8")


def _login_page(error: str = "") -> bytes:
    msg = f"<p class='flag'>{html.escape(error)}</p>" if error else ""
    return _page(
        "로그인",
        f"""<div class='card login'><h2>지원자 관리</h2>{msg}
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


# ---------------------------------------------------------------------------
# 표에서 바로 고치기 (칸을 눌러 편집)
# ---------------------------------------------------------------------------
#: 칸을 누르면 입력칸으로 바뀌고, Enter/포커스아웃에 /api/cell 로 저장한다.
#: 상세 화면과 같은 검사·같은 이력을 타므로 규칙이 갈라지지 않는다.
#: 페이지를 새로 그리지 않아 넓은 표에서 스크롤 위치가 유지된다.
_INLINE_JS = """
document.addEventListener('click', function(ev){
  if(window.__rangeDragged){ window.__rangeDragged = false; return; }  // 범위 선택 중이었다
  var td = ev.target.closest && ev.target.closest('td.edit');
  if(!td || td.querySelector('input,select')) return;
  openCell(td);
});
function openCell(td){
  var raw = td.dataset.raw || '', kind = td.dataset.kind || 'text', el;
  if(kind === 'select'){
    el = document.createElement('select');
    JSON.parse(td.dataset.opts || '[]').forEach(function(o){
      var op = document.createElement('option');
      op.value = o; op.textContent = o || '(빈칸)';
      if(o === raw) op.selected = true;
      el.appendChild(op);
    });
  } else {
    el = document.createElement('input');
    el.type = 'text'; el.value = raw;
    if(td.dataset.help) el.placeholder = td.dataset.help;
  }
  var before = td.textContent, done = false;
  td.textContent = ''; td.appendChild(el); el.focus();
  if(el.select) el.select();
  function cancel(){ if(done) return; done = true; td.textContent = before; }
  function save(){
    if(done) return; done = true;
    var v = el.value;
    if(v === raw){ td.textContent = before; return; }
    td.textContent = '저장 중...';
    var body = new URLSearchParams();
    body.append('id', td.dataset.id);
    body.append('항목', td.dataset.col);
    body.append('새값', v);
    body.append('이전값', raw);
    body.append('scope', td.dataset.scope || '기본');
    fetch('/api/cell', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'},
      body: body.toString()})
     .then(function(r){ return r.json(); })
     .then(function(d){
       if(d.ok){
         td.dataset.raw = d.raw; td.textContent = d.표시; td.title = d.표시;
         td.classList.add('saved');
         setTimeout(function(){ td.classList.remove('saved'); }, 1200);
       } else {
         td.textContent = before; td.title = d.error;
         td.classList.add('err'); alert(d.error);
         setTimeout(function(){ td.classList.remove('err'); }, 4000);
       }
     })
     .catch(function(e){ td.textContent = before; alert('저장 실패: ' + e); });
  }
  el.addEventListener('keydown', function(e){
    if(e.key === 'Enter'){ e.preventDefault(); save(); }
    else if(e.key === 'Escape'){ e.preventDefault(); cancel(); }
  });
  el.addEventListener('blur', save);
  if(kind === 'select') el.addEventListener('change', save);
}
"""


def _cell(cid: str, col: str, 표시: str, 원본: str, spec, *,
          scope: str = "기본", cls: str = "") -> str:
    """표 안의 편집 가능한 칸 하나.

    보이는 값(표시)과 저장된 값(원본)이 다를 수 있다 — 학교·학회는 명칭 사전을
    거쳐 대표명으로 보이기 때문이다. 편집은 언제나 원본을 고친다.
    """
    opts = json.dumps(list(spec.선택지), ensure_ascii=False) if spec.입력 == "select" else "[]"
    return (
        f"<td class='edit{cls}' data-id='{html.escape(cid)}' data-col='{html.escape(col)}'"
        f" data-raw='{html.escape(원본)}' data-kind='{html.escape(spec.입력)}'"
        f" data-opts='{html.escape(opts)}' data-scope='{scope}'"
        f" data-help='{html.escape(spec.도움말)}' title='{html.escape(표시)}'>"
        f"{html.escape(표시)}</td>"
    )


def _editable(col: str) -> bool:
    """표에서 직접 고칠 수 있는 열인가.

    명칭 사전이 관리하는 열(소속·학교·전공)은 제외한다. 표에 보이는 값은
    사전을 거친 대표명이라, 그 값을 그대로 저장하면 원문 표기가 사라진다.
    """
    return (
        col not in READONLY_FIELDS
        and col not in REGISTRY_FIELDS
        and not col.startswith(TIER_COLUMN_PREFIX)
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

    COLS = 표열()
    사용자열정의 = {f["이름"]: f for f in store.fields()}
    사용자값맵 = store.custom_map()
    수정가능 = can(me, "지원자_수정")
    이름표 = 라벨(COLS)
    head = "".join(f"<th>{html.escape(이름표[c])}</th>" for c in COLS)
    body_rows = []
    for rec in records:
        row = rec.to_row(registry)
        cid = rec.지원자_ID
        cells = [
            f"<td><input type='checkbox' name='ids' value='{html.escape(cid)}'></td>",
            f"<td><a href='/candidate?id={urllib.parse.quote(cid)}'>상세</a></td>",
            f"<td class='muted'>{html.escape(연도맵.get(cid, ''))}</td>",
        ]
        for c in COLS:
            if c in 사용자열정의:
                값 = 사용자값맵.get(cid, {}).get(c, "")
                if 수정가능:
                    cells.append(_cell(cid, c, 값, 값,
                                       custom_field_spec(사용자열정의[c]), scope="사용자"))
                else:
                    cells.append(f"<td title='{html.escape(값)}'>{html.escape(값)}</td>")
                continue
            표시 = str(row.get(c, "") or "")
            cls = " flag" if c == "검토_필요" and 표시 == "Y" else ""
            if 수정가능 and _editable(c):
                cells.append(_cell(cid, c, 표시, str(getattr(rec, c, "") or ""),
                                   field_spec(c), cls=cls))
            else:
                v = html.escape(표시)
                cells.append(f"<td class='{cls.strip()}' title='{v}'>{v}</td>")
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

    안내 = (
        "<p class='muted'>표의 칸을 눌러 바로 고칠 수 있습니다. "
        "Enter 로 저장, Esc 로 취소. 회색 칸(계산·자동 항목)은 고칠 수 없습니다.</p>"
        if 수정가능 else ""
    )
    checked = " checked" if review_only else ""
    처리중 = _busy_count()
    처리중알림 = (
        f"<div class='warn'>CV {처리중}건을 분석하고 있습니다. "
        f"<a href='/upload'>진행 상황 보기 →</a></div>" if 처리중 else ""
    )

    return _page(
        "지원자",
        f"""{''.join(warns)}{처리중알림}
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
          <p><a class='btn' href='/export.xlsx'>엑셀(.xlsx) 다운로드</a></p>
          {안내}
          {table}
        </div>""",
        me=me,
    )


# ---------------------------------------------------------------------------
# 표 공통 기능 — 정렬 · 찾기 · 엑셀처럼 범위 복사 · 엑셀 내려받기
# ---------------------------------------------------------------------------
#: 페이지마다 따로 만들지 않는다. `.scroll` 안의 표를 찾아 한 번에 붙인다.
#: 범위 복사는 숨은 textarea 에 TSV 를 넣고 선택해 두는 방식이다.
#: 사내망은 https 가 아니라 navigator.clipboard 를 못 쓰는 경우가 있어서,
#: 브라우저가 자체적으로 처리하는 Ctrl+C 가 가장 확실하다.
_TABLE_JS = r"""
function markDirty(el){
  el.classList.toggle('dirty', el.value !== (el.dataset.orig || ''));
}
function cellText(td){
  if(!td) return '';
  var f = td.querySelector ? td.querySelector('input,select,textarea') : null;
  if(f){
    if(f.type === 'checkbox') return f.checked ? 'Y' : '';
    if(f.tagName === 'SELECT'){
      var o = f.options[f.selectedIndex];
      return o ? o.text.replace(/\s+/g,' ').trim() : '';
    }
    return f.value;
  }
  return (td.textContent || '').replace(/\s+/g,' ').trim();
}
function bodyRows(tb){ return tb.tBodies[0] ? Array.prototype.slice.call(tb.tBodies[0].rows) : []; }
function headCells(tb){ return tb.tHead ? Array.prototype.slice.call(tb.tHead.rows[0].cells) : []; }
function headText(th){ return (th.dataset.label || th.textContent || '').replace(/\s+/g,' ').trim(); }

// 머리글이 빈 칸(체크박스·상세 링크)은 엑셀에 옮길 내용이 아니라 뺀다
function keepCols(tb){
  var keep = [];
  headCells(tb).forEach(function(th, i){ if(headText(th)) keep.push(i); });
  return keep.length ? keep : headCells(tb).map(function(_, i){ return i; });
}

function tableTSV(tb){
  var heads = headCells(tb), keep = keepCols(tb), out = [];
  out.push(keep.map(function(i){ return headText(heads[i]); }).join('\t'));
  bodyRows(tb).forEach(function(tr){
    if(tr.classList.contains('hide')) return;
    out.push(keep.map(function(i){ return cellText(tr.cells[i]); }).join('\t'));
  });
  return out.join('\n');
}

function copyText(text){
  var ta = document.getElementById('copybuf');
  if(!ta){
    ta = document.createElement('textarea');
    ta.id = 'copybuf';
    ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
    document.body.appendChild(ta);
  }
  ta.value = text;
  ta.select();
  var ok = false;
  try { ok = document.execCommand('copy'); } catch(e) { ok = false; }
  if(!ok && navigator.clipboard) navigator.clipboard.writeText(text);
  return ta;
}

function toast(msg){
  var el = document.getElementById('toast');
  if(!el){ el = document.createElement('div'); el.id = 'toast'; document.body.appendChild(el); }
  el.textContent = msg;
  el.className = 'show';
  clearTimeout(window.__toastT);
  window.__toastT = setTimeout(function(){ el.className = ''; }, 2200);
}

// --- 찾기 (표 전체 검색 + 열별 값 추리기) ------------------------------------
function applyFilters(tb){
  var q = (tb.__q || '').trim().toLowerCase();
  var f = tb.__filters || {};
  var cols = Object.keys(f);
  var 보임 = 0;
  bodyRows(tb).forEach(function(tr){
    var hit = !q || tr.innerText.toLowerCase().indexOf(q) >= 0;
    for(var i = 0; hit && i < cols.length; i++){
      var idx = parseInt(cols[i], 10);
      if(!f[cols[i]].has(cellText(tr.cells[idx]))) hit = false;
    }
    tr.classList.toggle('hide', !hit);
    if(hit) 보임++;
    else {                                   // 안 보이는 줄은 체크를 풀어둔다
      var c = tr.querySelector('input[type=checkbox]');
      if(c) c.checked = false;
    }
  });
  headCells(tb).forEach(function(th, i){
    th.classList.toggle('filtered', f[i] !== undefined);
  });
  var out = tb.__bar && tb.__bar.querySelector('.tcount');
  if(out) out.textContent = (q || cols.length) ? (보임 + '줄 보임') : '';
}

function sortBy(tb, idx, asc){
  headCells(tb).forEach(function(o){ o.removeAttribute('data-dir'); });
  if(idx === null){                                   // 정렬 해제 = 원래 순서
    var body = tb.tBodies[0];
    (tb.__order || []).forEach(function(r){ body.appendChild(r); });
    return;
  }
  headCells(tb)[idx].dataset.dir = asc ? 'asc' : 'desc';
  var rows = bodyRows(tb);
  rows.sort(function(a, b){
    var x = cellText(a.cells[idx]), y = cellText(b.cells[idx]);
    var nx = parseFloat(x.replace(/[^0-9.\-]/g,'')), ny = parseFloat(y.replace(/[^0-9.\-]/g,''));
    var 숫자 = x !== '' && y !== '' && !isNaN(nx) && !isNaN(ny)
              && /^[0-9.,\-\s]+$/.test(x) && /^[0-9.,\-\s]+$/.test(y);
    if(x === '' && y !== '') return 1;                // 빈칸은 늘 아래로
    if(y === '' && x !== '') return -1;
    var c = 숫자 ? (nx - ny) : x.localeCompare(y, 'ko');
    return asc ? c : -c;
  });
  var body = tb.tBodies[0];
  rows.forEach(function(r){ body.appendChild(r); });
}

function closeColMenu(){
  var m = document.getElementById('colmenu');
  if(m) m.remove();
}

// 열 제목을 누르면 무엇을 할지 고르게 한다 (엑셀 필터 단추와 같은 방식)
function openColMenu(tb, idx, th){
  closeColMenu();
  var 값들 = {}, f = tb.__filters || {};
  bodyRows(tb).forEach(function(tr){
    var v = cellText(tr.cells[idx]);
    값들[v] = (값들[v] || 0) + 1;
  });
  var 목록 = Object.keys(값들).sort(function(a, b){ return a.localeCompare(b, 'ko'); });
  var 선택 = f[idx];

  var m = document.createElement('div');
  m.id = 'colmenu';
  m.innerHTML =
    "<div class='cm-head'>" + headText(th) + "</div>"
    + "<button type='button' data-act='asc'>▲ 오름차순 정렬</button>"
    + "<button type='button' data-act='desc'>▼ 내림차순 정렬</button>"
    + "<button type='button' data-act='nosort'>정렬 해제</button>"
    + "<div class='cm-sep'></div>"
    + "<div class='cm-title'>값으로 추리기</div>"
    + "<input type='text' class='cm-q' placeholder='값 찾기'>"
    + "<label class='cm-row cm-allrow'><input type='checkbox' class='cm-all'> <b>전체</b></label>"
    + "<div class='cm-list'>"
    + 목록.map(function(v, i){
        var on = !선택 || 선택.has(v);
        return "<label class='cm-row' data-v='" + i + "'>"
          + "<input type='checkbox' value='" + i + "'" + (on ? " checked" : "") + "> "
          + (v === '' ? "<i>(빈칸)</i>" : v.replace(/</g,'&lt;'))
          + " <span class='muted'>" + 값들[v] + "</span></label>";
      }).join('')
    + "</div>"
    + "<div class='cm-btns'><button type='button' data-act='apply'>적용</button>"
    + "<button type='button' class='sec' data-act='clear'>이 열 조건 해제</button></div>"
    + "<div class='cm-sep'></div>"
    + "<button type='button' data-act='copycol'>이 열만 복사</button>";
  document.body.appendChild(m);
  var r = th.getBoundingClientRect();
  m.style.left = Math.min(r.left, window.innerWidth - m.offsetWidth - 12) + 'px';
  m.style.top = (r.bottom + window.scrollY + 2) + 'px';

  var boxes = function(){ return Array.prototype.slice.call(m.querySelectorAll('.cm-list input')); };
  var all = m.querySelector('.cm-all');
  all.checked = boxes().every(function(b){ return b.checked; });
  all.addEventListener('change', function(){
    boxes().forEach(function(b){
      if(!b.closest('.cm-row').classList.contains('hide')) b.checked = all.checked;
    });
  });
  m.querySelector('.cm-q').addEventListener('input', function(e){
    var q = e.target.value.toLowerCase();
    Array.prototype.slice.call(m.querySelectorAll('.cm-list .cm-row')).forEach(function(row){
      row.classList.toggle('hide', q && row.textContent.toLowerCase().indexOf(q) < 0);
    });
  });
  m.addEventListener('click', function(ev){
    var act = ev.target.dataset ? ev.target.dataset.act : null;
    if(!act) return;
    if(act === 'asc' || act === 'desc'){ sortBy(tb, idx, act === 'asc'); closeColMenu(); }
    else if(act === 'nosort'){ sortBy(tb, null); closeColMenu(); }
    else if(act === 'clear'){
      tb.__filters = tb.__filters || {};
      delete tb.__filters[idx];
      applyFilters(tb); closeColMenu();
    }
    else if(act === 'apply'){
      var 고른값 = new Set();
      boxes().forEach(function(b){ if(b.checked) 고른값.add(목록[parseInt(b.value, 10)]); });
      tb.__filters = tb.__filters || {};
      if(고른값.size === 목록.length) delete tb.__filters[idx];
      else tb.__filters[idx] = 고른값;
      applyFilters(tb); closeColMenu();
    }
    else if(act === 'copycol'){
      var 줄 = [headText(th)];
      bodyRows(tb).forEach(function(tr){
        if(!tr.classList.contains('hide')) 줄.push(cellText(tr.cells[idx]));
      });
      copyText(줄.join('\n'));
      toast('이 열을 복사했습니다. Ctrl+C 로 붙여넣으세요.');
      closeColMenu();
    }
  });
}
document.addEventListener('click', function(ev){
  var m = document.getElementById('colmenu');
  if(m && !m.contains(ev.target) && !(ev.target.closest && ev.target.closest('th.sortable'))) closeColMenu();
});
document.addEventListener('keydown', function(ev){ if(ev.key === 'Escape') closeColMenu(); });

function addToolbar(tb){
  var box = tb.closest('.scroll');
  if(!box) return;
  var bar = document.createElement('div');
  bar.className = 'tbar';
  bar.innerHTML =
    "<input type='text' placeholder='표에서 찾기' class='tfilter'>"
    + "<span class='muted tcount'></span><span style='flex:1'></span>"
    + "<button type='button' class='sec txlsx'>엑셀 내려받기</button>";
  box.parentNode.insertBefore(bar, box);
  tb.__bar = bar;

  bar.querySelector('.tfilter').addEventListener('input', function(e){
    tb.__q = e.target.value;
    applyFilters(tb);
  });
  bar.querySelector('.txlsx').addEventListener('click', function(){
    var form = document.createElement('form');
    form.method = 'post'; form.action = '/table.xlsx';
    form.innerHTML = "<input type='hidden' name='name'><input type='hidden' name='tsv'>";
    form.elements.name.value = tb.dataset.name || document.title;
    form.elements.tsv.value = tableTSV(tb);
    document.body.appendChild(form);
    window.__leaving = true;
    form.submit();
    setTimeout(function(){ form.remove(); window.__leaving = false; }, 1000);
  });
}

function sortable(tb){
  headCells(tb).forEach(function(th, idx){
    if(th.querySelector('input')) return;              // 전체선택 칸은 빼고
    if(!headText(th)) return;
    th.classList.add('sortable');
    th.title = '눌러서 정렬·추리기';
    th.addEventListener('click', function(ev){
      if(ev.target.tagName === 'A') return;
      openColMenu(tb, idx, th);
    });
  });
}

function rangeSelect(tb){
  var anchor = null, dragging = false;
  function clear(){
    Array.prototype.slice.call(tb.querySelectorAll('td.sel')).forEach(function(td){
      td.classList.remove('sel');
    });
  }
  function pos(td){ return {r: td.parentNode.rowIndex, c: td.cellIndex}; }
  function paint(a, b){
    clear();
    var r1 = Math.min(a.r, b.r), r2 = Math.max(a.r, b.r);
    var c1 = Math.min(a.c, b.c), c2 = Math.max(a.c, b.c);
    var heads = headCells(tb), lines = [], cols = [];
    for(var c = c1; c <= c2; c++) cols.push(c);
    // 머리글은 늘 함께 복사한다. 없으면 엑셀에서 무슨 열인지 알 수 없다.
    lines.push(cols.map(function(c){ return heads[c] ? headText(heads[c]) : ''; }).join('\t'));
    bodyRows(tb).forEach(function(tr){
      if(tr.rowIndex < r1 || tr.rowIndex > r2 || tr.classList.contains('hide')) return;
      lines.push(cols.map(function(c){
        var td = tr.cells[c];
        if(td) td.classList.add('sel');
        return cellText(td);
      }).join('\t'));
    });
    return lines.join('\n');
  }
  tb.addEventListener('mousedown', function(ev){
    var td = ev.target.closest && ev.target.closest('td');
    if(!td || !tb.contains(td)) return;
    if(ev.target.closest('input,select,textarea,a,button')) return;
    anchor = pos(td); dragging = true;
    window.__rangeDragged = false;
    clear();
  });
  tb.addEventListener('mousemove', function(ev){
    if(!dragging || !anchor) return;
    var td = ev.target.closest && ev.target.closest('td');
    if(!td || !tb.contains(td)) return;
    var here = pos(td);
    if(here.r !== anchor.r || here.c !== anchor.c){
      window.__rangeDragged = true;
      document.body.style.userSelect = 'none';
    }
    tb.__tsv = paint(anchor, here);
  });
  document.addEventListener('mouseup', function(){
    if(!dragging) return;
    dragging = false;
    document.body.style.userSelect = '';
    if(window.__rangeDragged && tb.__tsv){
      copyText(tb.__tsv);
      toast(tb.querySelectorAll('td.sel').length + '칸 선택됨 — Ctrl+C 로 복사하세요');
    }
  });
}

// --- 저장 안 한 채로 나가려 할 때 -------------------------------------------
function dirtyGuard(){
  document.querySelectorAll('form').forEach(function(f){
    f.addEventListener('submit', function(){ window.__leaving = true; });
  });
  document.addEventListener('click', function(ev){
    var a = ev.target.closest && ev.target.closest('a[href]');
    if(!a || a.target === '_blank') return;
    var href = a.getAttribute('href') || '';
    if(href.charAt(0) === '#' || href.indexOf('javascript:') === 0) return;
    if(!document.querySelector('.dirty')) return;
    if(confirm('저장하지 않은 수정이 있습니다.\n저장하지 않고 이동할까요?')) window.__leaving = true;
    else ev.preventDefault();
  }, true);
  window.addEventListener('beforeunload', function(e){
    if(window.__leaving) return;
    if(!document.querySelector('.dirty')) return;
    e.preventDefault();
    e.returnValue = '저장하지 않은 수정이 있습니다.';
    return e.returnValue;
  });
}

function enhanceTables(){
  document.querySelectorAll('.scroll table').forEach(function(tb){
    if(tb.dataset.enhanced) return;
    tb.dataset.enhanced = '1';
    if(!tb.tHead && tb.rows.length) tb.createTHead().appendChild(tb.rows[0]);
    if(!tb.tBodies.length) return;
    tb.__order = bodyRows(tb);          // 정렬 해제하면 되돌릴 원래 순서
    tb.__filters = {};
    addToolbar(tb);
    sortable(tb);
    rangeSelect(tb);
  });
  dirtyGuard();
}
document.addEventListener('DOMContentLoaded', enhanceTables);
"""


def _tsv_to_xlsx(tsv: str) -> bytes:
    """화면에 보이는 표를 그대로 엑셀로 만든다.

    표마다 서버 라우트를 만들지 않으려고, 화면에서 만든 TSV 를 받아
    같은 xlsx 작성기로 넘긴다. 머리글이 겹치면 뒤에 번호를 붙인다.
    """
    lines = [ln for ln in (tsv or "").replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        return build_xlsx([], ["(내용 없음)"])
    header, seen = [], {}
    for name in lines[0].split("\t"):
        name = name.strip() or "-"
        seen[name] = seen.get(name, 0) + 1
        header.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    rows = []
    for line in lines[1:]:
        cells = line.split("\t")
        rows.append({h: (cells[i] if i < len(cells) else "") for i, h in enumerate(header)})
    return build_xlsx(rows, header)


def 표열(registry_=None) -> list[str]:
    """지원자 표에 실제로 나갈 열 (숨김·순서 설정 반영)."""
    return store.arrange(list(table_columns(registry_ or registry)) + store.field_names())


def 라벨(열들: list[str]) -> dict[str, str]:
    return store.labels(열들)


#: 메일 본문 편집기.
#: 폐쇄망이라 외부 에디터를 못 받는다. contenteditable + execCommand 로 만들되,
#: 예전 판의 두 가지 고질병을 구조적으로 없앴다.
#:   1) 도구 모음에 native <select> 를 쓰면 포커스가 편집기에서 빠져나가서,
#:      커서 위치를 저장했다 되돌리는 방식이 필요했고 그게 들쭉날쭉했다.
#:      -> 전부 커스텀 단추로 바꾸고 mousedown 을 막아 **포커스를 아예 안 잃는다.**
#:   2) execCommand('fontSize') 는 1~7 만 받아 <font size=N> 을 남긴다.
#:      -> 7 로 표시해 두고 곧바로 <span style="font-size:12pt"> 로 바꿔치기한다.
#:      메일 클라이언트는 <style> 을 지우므로 **인라인 스타일**이 가장 안전하다.
_MAIL_JS = r"""
var RT = {editor: null, subject: null, last: null, range: null};

function rtInit(){
  RT.editor = document.getElementById('rtbody');
  if(!RT.editor) return;
  RT.subject = document.querySelector('input[name=subject]');
  RT.last = RT.editor;
  try { document.execCommand('styleWithCSS', false, true); } catch(e) {}

  ['keyup','mouseup','input'].forEach(function(ev){
    RT.editor.addEventListener(ev, function(){ RT.last = RT.editor; rtSave(); });
  });
  document.addEventListener('selectionchange', function(){
    if(document.activeElement === RT.editor) rtSave();
  });
  if(RT.subject) RT.subject.addEventListener('focus', function(){ RT.last = RT.subject; });

  // 도구를 눌러도 커서를 잃지 않게 한다 (이게 편집이 들쭉날쭉하던 원인)
  document.querySelector('.rt-bar').addEventListener('mousedown', function(e){
    if(e.target.closest('input[type=color], input[type=file]')) return;
    e.preventDefault();
  });

  RT.editor.addEventListener('paste', rtPaste);
  RT.editor.addEventListener('input', function(){
    markDirty(document.getElementById('bodyfield'));
  });
  var form = RT.editor.closest('form');
  if(form) form.addEventListener('submit', function(){
    document.getElementById('bodyfield').value = RT.editor.innerHTML;
  });
}

function rtSave(){
  var s = window.getSelection();
  if(s.rangeCount && RT.editor.contains(s.anchorNode)) RT.range = s.getRangeAt(0);
}
function rtFocus(){
  if(document.activeElement === RT.editor) return;
  var r = RT.range;                       // focus() 가 저장된 위치를 건드릴 수 있다
  RT.editor.focus();
  if(r && RT.editor.contains(r.startContainer)){
    var s = window.getSelection();
    s.removeAllRanges();
    s.addRange(r);
  }
}
function rtCmd(cmd, val){
  rtFocus();
  document.execCommand(cmd, false, val || null);
  rtSave();
  markDirty(document.getElementById('bodyfield'));
}
function rtInsert(html){
  rtFocus();
  document.execCommand('insertHTML', false, html);
  rtSave();
  markDirty(document.getElementById('bodyfield'));
}

// --- 글씨 크기: execCommand 의 1~7 을 실제 pt 로 바꿔친다 ----------------------
function rtFontSize(pt, label){
  rtFocus();
  document.execCommand('styleWithCSS', false, false);
  document.execCommand('fontSize', false, '7');      // 7 을 표시로 쓴다
  document.execCommand('styleWithCSS', false, true);
  var 표시들 = Array.prototype.slice.call(RT.editor.querySelectorAll('font[size="7"]'));
  var 마지막 = null;
  표시들.forEach(function(f){
    var s = document.createElement('span');
    s.style.fontSize = pt;
    while(f.firstChild) s.appendChild(f.firstChild);
    f.parentNode.replaceChild(s, f);
    마지막 = s;
  });
  if(마지막){                                        // 이어서 칠 수 있게 커서를 둔다
    var r = document.createRange();
    r.selectNodeContents(마지막);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
    rtSave();
  }
  var btn = document.getElementById('rt-size-btn');
  if(btn && label) btn.firstChild.textContent = label;
  markDirty(document.getElementById('bodyfield'));
}
function rtFontName(name){
  rtCmd('fontName', name);
  var btn = document.getElementById('rt-font-btn');
  if(btn) btn.firstChild.textContent = name;
}

// --- 붙여넣기 정리 ------------------------------------------------------------
function rtPaste(e){
  var dt = e.clipboardData;
  if(!dt) return;
  var html = dt.getData('text/html');
  if(!html){ return; }                                // 글자만이면 그대로 둔다
  e.preventDefault();
  var box = document.createElement('div');
  box.innerHTML = html
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/<(style|script|meta|link)[\s\S]*?<\/\1>/gi, '')
    .replace(/<(style|meta|link)[^>]*>/gi, '');
  box.querySelectorAll('*').forEach(function(el){
    ['class','id','lang','align'].forEach(function(a){ el.removeAttribute(a); });
    if(el.hasAttribute('style')){
      var 남길것 = ['color','background-color','font-size','font-family','font-weight',
                  'font-style','text-decoration','text-align'];
      var 새 = 남길것.map(function(k){
        var v = el.style.getPropertyValue(k);
        return v ? k + ':' + v : '';
      }).filter(Boolean).join(';');
      if(새) el.setAttribute('style', 새); else el.removeAttribute('style');
    }
  });
  rtInsert(box.innerHTML);
}

// --- 공통 드롭다운 -----------------------------------------------------------
function rtDrop(btn, html, onPick){
  var old = document.getElementById('rtdrop');
  if(old){
    var 같은것 = old.dataset.owner === btn.id;
    old.remove();
    if(같은것) return;
  }
  var m = document.createElement('div');
  m.id = 'rtdrop';
  m.dataset.owner = btn.id || '';
  m.innerHTML = html;
  document.body.appendChild(m);
  var r = btn.getBoundingClientRect();
  m.style.left = Math.min(r.left, window.innerWidth - m.offsetWidth - 12) + 'px';
  m.style.top = (r.bottom + window.scrollY + 3) + 'px';
  m.addEventListener('mousedown', function(e){ e.preventDefault(); });
  m.addEventListener('click', function(e){
    var it = e.target.closest('[data-v]');
    if(!it) return;
    onPick(it.dataset.v, it.dataset.label || it.textContent.trim(), it);
    if(!it.dataset.keep) m.remove();
  });
  return m;
}
document.addEventListener('click', function(e){
  var m = document.getElementById('rtdrop');
  if(m && !m.contains(e.target) && !(e.target.closest && e.target.closest('.rt-drop')))
    m.remove();
});
document.addEventListener('keydown', function(e){
  if(e.key !== 'Escape') return;
  var m = document.getElementById('rtdrop');
  if(m) m.remove();
});

function rtFontMenu(btn){
  var 목록 = window.rtFonts || [];
  rtDrop(btn, 목록.map(function(f){
    return "<button type='button' data-v=\"" + f + "\" style=\"font-family:'" + f
      + "'\">" + f + "</button>";
  }).join(''), function(v){ rtFontName(v); });
}
function rtSizeMenu(btn){
  var 목록 = window.rtSizes || [];
  rtDrop(btn, 목록.map(function(s){
    return "<button type='button' data-v='" + s + "' style='font-size:"
      + Math.min(parseInt(s, 10) * 1.2, 26) + "px'>" + s + "</button>";
  }).join(''), function(v){ rtFontSize(v, v); });
}
function rtColorMenu(btn, cmd){
  var 색 = ['#000000','#404040','#808080','#b0b0b0','#ffffff',
           '#b91c1c','#ea580c','#ca8a04','#15803d','#0e7490',
           '#1d4ed8','#4f46e5','#7c3aed','#be185d','#78350f',
           '#fee2e2','#ffedd5','#fef9c3','#dcfce7','#dbeafe'];
  var html = "<div class='rt-swatch'>" + 색.map(function(c){
    return "<button type='button' data-v='" + c + "' style='background:" + c
      + "' title='" + c + "'></button>";
  }).join('') + "</div>"
    + "<label class='rt-pick'>직접 고르기"
    + "<input type='color' onchange=\"rtCmd('" + cmd + "', this.value)\"></label>";
  rtDrop(btn, html, function(v){ rtCmd(cmd, v); });
}
function rtTableMenu(btn){
  var html = "<div class='rt-grid'>";
  for(var r = 1; r <= 6; r++){
    for(var c = 1; c <= 6; c++){
      html += "<i data-v='" + r + "x" + c + "' data-r='" + r + "' data-c='" + c + "'></i>";
    }
  }
  html += "</div><div class='rt-gridlabel'>표 크기를 고르세요</div>";
  var m = rtDrop(btn, html, function(v){
    var 조각 = v.split('x');
    rtTable(parseInt(조각[0], 10), parseInt(조각[1], 10));
  });
  if(!m) return;
  var 라벨 = m.querySelector('.rt-gridlabel');
  m.addEventListener('mouseover', function(e){
    var it = e.target.closest('i[data-v]');
    if(!it) return;
    var R = +it.dataset.r, C = +it.dataset.c;
    라벨.textContent = R + ' × ' + C;
    m.querySelectorAll('i').forEach(function(cell){
      cell.classList.toggle('on', +cell.dataset.r <= R && +cell.dataset.c <= C);
    });
  });
}
function rtTable(행, 열){
  if(!행 || !열) return;
  var s = "<table style='border-collapse:collapse;width:100%;font-size:11pt'>";
  for(var r = 0; r < 행; r++){
    s += '<tr>';
    for(var c = 0; c < 열; c++){
      s += "<td style='border:1px solid #999;padding:6px'>&nbsp;</td>";
    }
    s += '</tr>';
  }
  s += '</table><p><br></p>';
  rtInsert(s);
}
function rtLink(){
  var url = prompt('링크 주소를 넣으세요', 'https://');
  if(url) rtCmd('createLink', url);
}
function rtImage(input){
  var f = input.files && input.files[0];
  input.value = '';
  if(!f) return;
  if(f.size > 1024 * 1024){
    alert('그림이 너무 큽니다 (' + Math.round(f.size / 1024) + 'KB).\n'
      + '본문에 넣는 그림은 1MB 까지입니다. 큰 파일은 첨부로 붙이세요.');
    return;
  }
  var fr = new FileReader();
  fr.onload = function(){
    rtInsert("<img src='" + fr.result + "' style='max-width:100%'>");
  };
  fr.readAsDataURL(f);
}

// --- 자리표시자 고르기 --------------------------------------------------------
function rtVars(btn){
  var 묶음 = window.자리표시자 || [];
  var html = "<div class='vm-head'>넣을 자리에 커서를 두고 고르세요</div>"
    + "<input type='text' class='vm-q' placeholder='이름으로 찾기'>"
    + "<div class='vm-list'>"
    + 묶음.map(function(g){
        return "<div class='vm-group'>" + g[0] + "</div>"
          + g[1].map(function(v){
              return "<button type='button' class='vm-item' data-v='" + v + "'>"
                + v + "</button>";
            }).join('');
      }).join('')
    + "</div>";
  var m = rtDrop(btn, html, function(v){
    var 넣을것 = '{{' + v + '}}';
    if(RT.last === RT.subject && RT.subject){
      var s = RT.subject.selectionStart, e = RT.subject.selectionEnd;
      RT.subject.value = RT.subject.value.slice(0, s) + 넣을것
        + RT.subject.value.slice(e);
      RT.subject.focus();
      RT.subject.selectionStart = RT.subject.selectionEnd = s + 넣을것.length;
      markDirty(RT.subject);
    } else {
      rtInsert(넣을것);
    }
  });
  if(!m) return;
  m.classList.add('varmenu');
  var q = m.querySelector('.vm-q');
  q.addEventListener('mousedown', function(e){ e.stopPropagation(); });
  q.addEventListener('input', function(){
    var 찾기 = q.value.trim().toLowerCase();
    m.querySelectorAll('.vm-item').forEach(function(b){
      b.classList.toggle('hide', 찾기 && b.dataset.v.toLowerCase().indexOf(찾기) < 0);
    });
    m.querySelectorAll('.vm-group').forEach(function(g){
      var 보임 = false, el = g.nextElementSibling;
      while(el && el.classList.contains('vm-item')){
        if(!el.classList.contains('hide')) 보임 = true;
        el = el.nextElementSibling;
      }
      g.classList.toggle('hide', !보임);
    });
  });
  setTimeout(function(){ q.focus(); }, 0);
}
document.addEventListener('DOMContentLoaded', rtInit);
"""

def 홈(me: User | None) -> str:
    """이 사람이 처음 볼 화면. 현업은 채용 현황이 홈이다."""
    return "/" if can(me, "지원자_목록") else "/recruit"


def _볼수있나(me: User, 지원자_ID: str) -> bool:
    """현업은 **배정된 과제의 지원자만** 볼 수 있다.

    화면에서 감추는 것으로는 부족하다. 주소를 직접 쳐도 막혀야 한다.
    """
    if not can(me, "지원자_조회"):
        return False
    보이는 = auth.visible_project_ids(me)
    if 보이는 is None:
        return True
    return recruit.get(지원자_ID).project_id in 보이는


def _busy_count() -> int:
    with _status_lock:
        return sum(1 for s in _status.values() if s["state"] in ("대기중", "처리중"))


def _upload_page(me: User) -> bytes:
    """CV 업로드 전용 화면.

    예전에는 지원자 목록 맨 위에 업로드 상자가 붙어 있어서, 표를 보러 올 때마다
    쓰지도 않는 상자가 화면을 차지했다. 탭으로 뺐다.
    """
    보관 = "켜짐 (재분석 가능)" if settings.store_cv_text else "꺼짐 (재분석하려면 재업로드 필요)"
    가능 = ", ".join(sorted(SUPPORTED_SUFFIXES))
    등록가능 = can(me, "지원자_등록")
    if not 등록가능:
        본문 = "<div class='card'><h2>CV 업로드</h2><p>업로드 권한이 없습니다.</p></div>"
    else:
        본문 = f"""
        <div class='card'><h2>CV 업로드</h2>
          <form method='post' action='/upload' enctype='multipart/form-data'>
            <p><input type='file' name='files' multiple accept='{가능}'></p>
            <button type='submit'>업로드 후 분석</button>
            <span class='muted'>여러 개를 한 번에 고를 수 있습니다 ({가능}).</span>
          </form>
          <p class='muted'>분석은 뒤에서 돌아갑니다. 끝나면 아래 현황에 뜨고
          <a href='/'>지원자 목록</a>에 줄이 생깁니다.</p>
        </div>
        <div class='card'><h2>CV 없이 지원자 추가</h2>
          <form method='post' action='/candidate/new'>
            <button type='submit' class='sec'>빈 지원자 만들기</button>
            <span class='muted'>다른 지원서로 지원한 경우. 빈 칸을 직접 채웁니다.</span>
          </form>
        </div>
        <div class='card'><h2>보관 설정</h2>
          <p class='muted'>원문 텍스트 보관: <b>{보관}</b> ·
          보관 기간 {settings.retention_months}개월
          (0 = 무제한) · 설정은 <code>.env</code> 에서 바꿉니다.</p>
        </div>"""
    return _page("CV 업로드", 본문 + _status_table(), me=me)


def _candidate_page(지원자_ID: str, me: User, error: str = "") -> bytes:
    rec = store.get(지원자_ID)
    if rec is None:
        return _page("없음", "<div class='card'>해당 지원자를 찾을 수 없습니다.</div>")
    meta = store.meta(지원자_ID) or {}
    row = rec.to_row(registry)
    수정가능 = can(me, "지원자_수정")

    def 입력칸(항목: str, 값: str) -> str:
        if 항목 in REGISTRY_FIELDS:
            종류 = NAME_COLUMNS[항목]
            현재 = registry.display(종류, 값) if 값 else ""
            보기 = [""] + [n.표시명 for n in registry.list_all(종류)]
            opts = "".join(
                f"<option value='{html.escape(o)}'{' selected' if o == 현재 else ''}>"
                f"{html.escape(o) or '(빈칸)'}</option>"
                for o in dict.fromkeys(보기)
            )
            return f"<select name='새값'>{opts}</select>"
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

    이름표 = 라벨(list(table_columns(registry)))
    항목행 = []
    for c in table_columns(registry):
        값 = str(row.get(c, "") or "")
        보기 = html.escape(값) or "<span class='muted'>-</span>"
        if not 수정가능 or c in READONLY_FIELDS or c.startswith("1저자_해외논문_"):
            항목행.append(
                f"<tr><th style='width:170px'>{html.escape(이름표[c])}</th>"
                f"<td style='white-space:normal;max-width:none'>{보기}</td></tr>"
            )
            continue
        원본값 = str(getattr(rec, c, "") or "")
        항목행.append(
            f"<tr><th style='width:170px'>{html.escape(이름표[c])}</th>"
            f"<td style='white-space:normal;max-width:none'>"
            f"<form method='post' action='/candidate/edit' style='display:flex;gap:6px'>"
            f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
            f"<input type='hidden' name='항목' value='{html.escape(c)}'>"
            f"<input type='hidden' name='이전값' value='{html.escape(원본값)}'>"
            f"{입력칸(c, 원본값)}<button type='submit'>저장</button></form></td></tr>"
        )

    사용자열 = store.fields()
    사용자값 = store.custom_values(지원자_ID)
    사용자행 = []
    for f in 사용자열:
        이름, 값 = f["이름"], 사용자값.get(f["이름"], "")
        if not 수정가능:
            사용자행.append(
                f"<tr><th style='width:170px'>{html.escape(이름)}</th>"
                f"<td>{html.escape(값) or '<span class=muted>-</span>'}</td></tr>"
            )
            continue
        spec = custom_field_spec(f)
        if spec.입력 == "select":
            opts = "".join(
                f"<option value='{html.escape(o)}'{' selected' if o == 값 else ''}>"
                f"{html.escape(o) or '(빈칸)'}</option>" for o in spec.선택지
            )
            칸 = f"<select name='새값'>{opts}</select>"
        else:
            칸 = (
                f"<input type='text' name='새값' value='{html.escape(값)}'"
                f" style='width:260px' placeholder='{html.escape(spec.도움말)}'>"
            )
        사용자행.append(
            f"<tr><th style='width:170px'>{html.escape(이름)}"
            f"<br><span class='muted'>{html.escape(f['유형'])}</span></th>"
            f"<td><form method='post' action='/candidate/custom' style='display:flex;gap:6px'>"
            f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
            f"<input type='hidden' name='항목' value='{html.escape(이름)}'>"
            f"{칸}<button type='submit'>저장</button></form></td></tr>"
        )
    사용자카드 = (
        f"<div class='card'><h2>추가 항목</h2><table>{''.join(사용자행)}</table></div>"
        if 사용자열 else ""
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

    메일기록 = mailing.history(지원자_ID)
    메일행 = "".join(
        f"<tr><td>{html.escape(m['보낸일시'])}</td>"
        f"<td>{html.escape(m['템플릿이름'])}</td>"
        f"<td>{html.escape(m['받는사람'])}</td>"
        f"<td>{html.escape(m['상태'])}</td>"
        f"<td class='muted' style='white-space:normal'>{html.escape(m['오류'] or '')}</td></tr>"
        for m in 메일기록
    )
    메일카드 = (
        "<div class='card'><h2>보낸 메일</h2>"
        + ("<div class='warn'>탈락 메일을 보낸 지원자입니다. "
           "이후 어떤 메일도 보낼 수 없습니다.</div>"
           if mailing.rejected(지원자_ID) else "")
        + "<div class='scroll'><table data-name='보낸 메일'>"
        "<tr><th>보낸 일시</th><th>템플릿</th><th>받는 주소</th><th>상태</th><th>메모</th></tr>"
        + 메일행 + "</table></div></div>"
    ) if 메일기록 else ""

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
        {사용자카드}
        {첨부카드}
        {메일카드}
        <div class='card'><h2>변경 이력</h2><div class='scroll'>
          <table><tr><th>일시</th><th>사용자</th><th>내용</th></tr>{이력행}</table>
        </div></div>""",
        me=me,
    )


def _busy_count() -> int:
    with _status_lock:
        return sum(1 for s in _status.values() if s["state"] in ("대기중", "처리중"))


def _upload_page(me: User) -> bytes:
    """CV 업로드 전용 화면.

    예전에는 지원자 목록 맨 위에 업로드 상자가 붙어 있어서, 표를 보러 올 때마다
    쓰지도 않는 상자가 화면을 차지했다. 탭으로 뺐다.
    """
    보관 = "켜짐 (재분석 가능)" if settings.store_cv_text else "꺼짐 (재분석하려면 재업로드 필요)"
    가능 = ", ".join(sorted(SUPPORTED_SUFFIXES))
    등록가능 = can(me, "지원자_등록")
    if not 등록가능:
        본문 = "<div class='card'><h2>CV 업로드</h2><p>업로드 권한이 없습니다.</p></div>"
    else:
        본문 = f"""
        <div class='card'><h2>CV 업로드</h2>
          <form method='post' action='/upload' enctype='multipart/form-data'>
            <p><input type='file' name='files' multiple accept='{가능}'></p>
            <button type='submit'>업로드 후 분석</button>
            <span class='muted'>여러 개를 한 번에 고를 수 있습니다 ({가능}).</span>
          </form>
          <p class='muted'>분석은 뒤에서 돌아갑니다. 끝나면 아래 현황에 뜨고
          <a href='/'>지원자 목록</a>에 줄이 생깁니다.</p>
        </div>
        <div class='card'><h2>CV 없이 지원자 추가</h2>
          <form method='post' action='/candidate/new'>
            <button type='submit' class='sec'>빈 지원자 만들기</button>
            <span class='muted'>다른 지원서로 지원한 경우. 빈 칸을 직접 채웁니다.</span>
          </form>
        </div>
        <div class='card'><h2>보관 설정</h2>
          <p class='muted'>원문 텍스트 보관: <b>{보관}</b> ·
          보관 기간 {settings.retention_months}개월
          (0 = 무제한) · 설정은 <code>.env</code> 에서 바꿉니다.</p>
        </div>"""
    return _page("CV 업로드", 본문 + _status_table(), me=me)


def _candidate_page(지원자_ID: str, me: User, error: str = "") -> bytes:
    rec = store.get(지원자_ID)
    if rec is None:
        return _page("없음", "<div class='card'>해당 지원자를 찾을 수 없습니다.</div>")
    meta = store.meta(지원자_ID) or {}
    row = rec.to_row(registry)
    수정가능 = can(me, "지원자_수정")

    def 입력칸(항목: str, 값: str) -> str:
        if 항목 in REGISTRY_FIELDS:
            종류 = NAME_COLUMNS[항목]
            현재 = registry.display(종류, 값) if 값 else ""
            보기 = [""] + [n.표시명 for n in registry.list_all(종류)]
            opts = "".join(
                f"<option value='{html.escape(o)}'{' selected' if o == 현재 else ''}>"
                f"{html.escape(o) or '(빈칸)'}</option>"
                for o in dict.fromkeys(보기)
            )
            return f"<select name='새값'>{opts}</select>"
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

    사용자열 = store.fields()
    사용자값 = store.custom_values(지원자_ID)
    사용자행 = []
    for f in 사용자열:
        이름, 값 = f["이름"], 사용자값.get(f["이름"], "")
        if not 수정가능:
            사용자행.append(
                f"<tr><th style='width:170px'>{html.escape(이름)}</th>"
                f"<td>{html.escape(값) or '<span class=muted>-</span>'}</td></tr>"
            )
            continue
        spec = custom_field_spec(f)
        if spec.입력 == "select":
            opts = "".join(
                f"<option value='{html.escape(o)}'{' selected' if o == 값 else ''}>"
                f"{html.escape(o) or '(빈칸)'}</option>" for o in spec.선택지
            )
            칸 = f"<select name='새값'>{opts}</select>"
        else:
            칸 = (
                f"<input type='text' name='새값' value='{html.escape(값)}'"
                f" style='width:260px' placeholder='{html.escape(spec.도움말)}'>"
            )
        사용자행.append(
            f"<tr><th style='width:170px'>{html.escape(이름)}"
            f"<br><span class='muted'>{html.escape(f['유형'])}</span></th>"
            f"<td><form method='post' action='/candidate/custom' style='display:flex;gap:6px'>"
            f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
            f"<input type='hidden' name='항목' value='{html.escape(이름)}'>"
            f"{칸}<button type='submit'>저장</button></form></td></tr>"
        )
    사용자카드 = (
        f"<div class='card'><h2>추가 항목</h2><table>{''.join(사용자행)}</table></div>"
        if 사용자열 else ""
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
        {사용자카드}
        {첨부카드}
        <div class='card'><h2>변경 이력</h2><div class='scroll'>
          <table><tr><th>일시</th><th>사용자</th><th>내용</th></tr>{이력행}</table>
        </div></div>""",
        me=me,
    )


def _names_page(종류: str, me: User | None = None,
                error: str = "", msg: str = "") -> bytes:
    """소속·학회·저널·전공을 같은 화면에서 관리한다.

    **CV 에 적힌 표기마다 한 줄**이다. 여러 표기를 한 줄로 합쳐 대표명만 남기면,
    잘못 분류한 걸 나중에 알아채도 무엇이 잘못 들어갔는지 볼 수도 떼어낼 수도
    없었다. 지금은 그 줄의 이름만 고치면 된다.

    등급·국내해외·유형·IF 는 표기가 아니라 **이름**에 붙는다. 같은 이름을 쓰는
    표기들은 저절로 같은 분류를 쓴다.
    """
    종류 = canonical_kind(종류)
    if 종류 not in KINDS:
        종류 = "학회·저널"
    items = registry.list_all(종류)          # 표시명 오름차순이 기본
    등급목록 = registry.tier_names()
    등급종류 = 종류 in GRADED_KINDS

    무리: dict[str, list] = {}
    for i in items:
        무리.setdefault(i.표시명, []).append(i)

    탭 = " ".join(
        f"<a class='btn {'' if k == 종류 else 'sec'}' href='/names?kind={urllib.parse.quote(k)}'>"
        f"{k}"
        + (f" <b>{registry.unclassified_count(k)}</b>" if k in GRADED_KINDS
           and registry.unclassified_count(k) else "")
        + "</a>"
        for k in KINDS
    )

    등급열 = ""
    if 등급종류:
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

    이름목록 = registry.display_names(종류)
    이름옵션 = "".join(f"<option value='{html.escape(n)}'>" for n in 이름목록)

    rows = []
    for i in items:
        형제 = [x.원표기 for x in 무리.get(i.표시명, []) if x.id != i.id]
        형제칸 = (
            "<span class='muted'>" + html.escape(", ".join(형제)) + "</span>"
            if 형제 else "<span class='muted'>-</span>"
        )
        등급칸 = ""
        if 등급종류:
            유형opt = "".join(
                f"<option{' selected' if s == i.유형 else ''}>{html.escape(s)}</option>"
                for s in SUBTYPES
            )
            등급opt = "".join(
                f"<option{' selected' if g == i.등급 else ''}>{html.escape(g)}</option>"
                for g in 등급목록
            )
            해외opt = "".join(
                f"<option{' selected' if v == i.국내해외 else ''}>{v}</option>"
                for v in ("불명", "해외", "국내")
            )
            등급칸 = (
                f"<td class='ctl'><select form='saveform' name='유형_{i.id}'"
                f" data-orig='{html.escape(i.유형)}' onchange='markDirty(this)'>{유형opt}"
                f"</select></td>"
                f"<td class='ctl'><select form='saveform' name='등급_{i.id}'"
                f" data-orig='{html.escape(i.등급)}' onchange='markDirty(this)'>{등급opt}</select></td>"
                f"<td class='ctl'><select form='saveform' name='국내해외_{i.id}'"
                f" data-orig='{html.escape(i.국내해외)}' onchange='markDirty(this)'>{해외opt}"
                f"</select></td>"
                f"<td class='ctl'><input type='text' form='saveform' name='IF_{i.id}'"
                f" value='{html.escape(i.IF)}' style='width:64px' placeholder='예: 12.5'"
                f" data-orig='{html.escape(i.IF)}' oninput='markDirty(this)'>"
                f" <a href='{html.escape(i.google_url())}' target='_blank' rel='noopener'"
                f" title='구글에서 &quot;{html.escape(i.표시명)} impact factor&quot; 검색'>찾기</a>"
                f"</td>"
            )
        미분류표시 = (
            " <span class='pill p-미분류'>미분류</span>"
            if 등급종류 and i.등급 == "미분류" else ""
        )
        rows.append(
            f"<tr>"
            f"<td style='white-space:normal'>{html.escape(i.원표기)}{미분류표시}</td>"
            f"<td>{i.발견횟수}</td>"
            f"<td class='ctl'>"
            f"<input type='hidden' form='saveform' name='id' value='{i.id}'>"
            f"<input type='text' form='saveform' name='표시명_{i.id}' list='이름목록'"
            f" value='{html.escape(i.표시명)}' style='width:220px'"
            f" data-orig='{html.escape(i.표시명)}' oninput='markDirty(this)'></td>"
            f"<td style='white-space:normal'>{형제칸}</td>"
            f"{등급칸}"
            f"<td><form method='post' action='/names/forget'"
            f" onsubmit=\"return confirm('이 표기를 사전에서 지웁니다. "
            f"다시 CV 에 나오면 새로 등록됩니다.')\">"
            f"<input type='hidden' name='kind' value='{html.escape(종류)}'>"
            f"<input type='hidden' name='id' value='{i.id}'>"
            f"<button class='danger'>지움</button></form></td></tr>"
        )

    저장바 = (
        f"<form method='post' action='/names/save' id='saveform' class='mergebar'>"
        f"<input type='hidden' name='kind' value='{html.escape(종류)}'>"
        f"<button type='submit'>고친 내용 저장</button>"
        f"<span class='muted'>여러 줄을 고친 뒤 <b>한 번만</b> 누르세요. "
        f"고친 칸은 노랗게 표시됩니다.</span></form>"
        if items else ""
    )

    등급머리 = (
        "<th class='ctl'>학회/저널</th><th class='ctl'>등급</th>"
        "<th class='ctl'>국내/해외</th><th class='ctl'>Impact Factor</th>"
        if 등급종류 else ""
    )
    표 = (
        "<table><tr><th>CV 에 적힌 표기</th><th style='width:56px'>발견</th>"
        f"<th class='ctl'>표에 보일 이름</th><th>같은 이름으로 묶인 표기</th>"
        f"{등급머리}<th></th></tr>{''.join(rows)}</table>"
        if rows
        else "<p class='muted'>아직 등록된 항목이 없습니다. CV를 업로드하면 자동으로 등록됩니다.</p>"
    )
    알림 = f"<div class='done'>{html.escape(msg)}</div>" if msg else ""
    오류 = f"<div class='warn'>{html.escape(error)}</div>" if error else ""
    설명 = (
        "학교·회사가 CV 마다 다르게 적혀 있습니다(포항공대 / POSTECH / 포항공과대학교)."
        if 종류 == "소속"
        else "같은 곳이 CV 마다 다르게 적혀 있습니다(ICML / Proc. of ICML 2023)."
    )
    분류설명 = (
        " 등급·국내해외·유형·IF 는 <b>이름에 붙습니다</b> — 같은 이름을 쓰는 표기는"
        " 자동으로 같은 분류가 됩니다."
        if 등급종류 else ""
    )
    return _page(
        f"{종류} 관리",
        f"""{알림}{오류}<div class='card'><h2>명칭 관리</h2><p>{탭}</p>
        <p class='muted'>{설명}
        <b>CV 에 적힌 표기마다 한 줄</b>이고, 각 줄의 <b>표에 보일 이름</b>만 고칩니다.
        같은 곳이면 같은 이름을 적으세요 — 지원자 표에는 그 이름으로 함께 나옵니다.
        잘못 묶였으면 그 줄의 이름만 다시 고치면 됩니다.{분류설명}</p></div>
        {등급열}
        <div class='card'><h2>{html.escape(종류)} <span class='muted'>표기 {len(items)}개 ·
        이름 {len(무리)}개</span></h2>
        {저장바}
        <div class='scroll'>{표}</div>
        <datalist id='이름목록'>{이름옵션}</datalist></div>""",
        me=me,
    )


def _mail_vars(rec, 진행맵=None) -> dict[str, str]:
    """이 지원자에게 쓸 수 있는 자리표시자 값.

    표에 보이는 값과 같은 것을 쓴다(명칭 사전을 거친 대표명). 화면에서 본 것과
    메일에 나가는 것이 달라지면 안 된다.
    """
    값 = {k: str(v or "") for k, v in rec.to_row(registry).items()}
    값.update(store.custom_values(rec.지원자_ID))
    p = (진행맵 or {}).get(rec.지원자_ID)
    if p is not None:
        부서명 = {d["id"]: d["이름"] for d in auth.departments()}
        과제명 = {pr["id"]: pr["이름"] for pr in auth.projects()}
        값["부서"] = 부서명.get(p.부서_id, "")
        값["과제"] = 과제명.get(p.project_id, "")
        값["최종상태"] = p.최종상태
    값.setdefault("부서", "")
    값.setdefault("과제", "")
    값.setdefault("최종상태", "")
    값["이름"] = 값.get("한글_이름") or 값.get("영문_이름", "")
    return 값


def _mail_page(me: User, error: str = "", msg: str = "") -> bytes:
    """메일 템플릿 목록 + 새 템플릿."""
    templates = mailing.templates()
    설정경고 = ""
    빠진것 = mailapi.missing_settings()
    if settings.mail_dry_run:
        설정경고 = (
            "<div class='warn'><b>연습 모드입니다 (MAIL_DRY_RUN=1).</b> "
            "발송을 눌러도 실제로 나가지 않고 기록만 남습니다. "
            "설정을 확인한 뒤 <code>.env</code> 에서 <code>MAIL_DRY_RUN=0</code> 으로 "
            "바꾸세요.</div>"
        )
    elif 빠진것:
        설정경고 = (
            f"<div class='warn'>메일 설정이 비어 있어 보낼 수 없습니다: "
            f"<b>{html.escape(', '.join(빠진것))}</b> — <code>.env</code> 를 확인하세요.</div>"
        )
    설정경고 += (
        f"<p class='muted'>발송 구현: <b>{html.escape(mailapi.IMPL_NAME)}</b>"
        + ("" if getattr(mailapi, "LOCAL", False) else
           " · 서버에 맞춘 구현을 쓰려면 <code>cvtool/clients/mail_local.py</code> 로 두세요"
           " (git 이 건드리지 않습니다)")
        + "</p>"
    )

    탈락배지 = "<span class='pill p-미분류'>탈락 메일</span>"
    rows = "".join(
        f"<tr><td><a href='/mail/template?id={t.id}'>{html.escape(t.이름)}</a></td>"
        f"<td>{탈락배지 if t.탈락메일 else ''}</td>"
        f"<td style='white-space:normal'>{html.escape(t.제목)}</td>"
        f"<td class='muted'>{html.escape(t.참조)}</td>"
        f"<td class='muted'>{len(mailing.attachments(t.id)) or ''}</td>"
        f"<td class='muted'>{html.escape(t.수정일시)}</td>"
        f"<td><a class='btn' href='/mail/send?id={t.id}'>보내기</a></td></tr>"
        for t in templates
    ) or "<tr><td colspan='7' class='muted'>아직 만든 템플릿이 없습니다.</td></tr>"

    알림 = f"<div class='done'>{html.escape(msg)}</div>" if msg else ""
    오류 = f"<p class='flag'>{html.escape(error)}</p>" if error else ""
    return _page(
        "메일",
        알림 + 설정경고
        + "<div class='card'><h2>템플릿 만들기</h2>" + 오류
        + "<form method='post' action='/mail/template/add'>"
        "<p><input type='text' name='name' placeholder='템플릿 이름 (예: 서류합격 안내)'"
        " required style='width:320px'></p>"
        "<p><label><input type='checkbox' name='reject' value='1'> "
        "<b>탈락 메일</b> — 이걸 보낸 지원자에게는 이후 어떤 메일도 보내지 않습니다</label></p>"
        "<button type='submit'>만들기</button>"
        "<span class='muted'> 만든 뒤 편집 화면에서 제목·본문을 꾸미고 첨부를 붙입니다.</span>"
        "</form></div>"
        f"<div class='card'><h2>템플릿 {len(templates)}개</h2><div class='scroll'>"
        "<table data-name='메일 템플릿'><tr><th>이름</th><th>구분</th><th>제목</th>"
        "<th>참조</th><th>첨부</th><th>수정</th><th></th></tr>" + rows + "</table></div>"
        "<p><a class='btn sec' href='/mail/log'>발송 이력</a></p></div>",
        me=me,
    )


def _mail_var_groups() -> list[tuple[str, list[str]]]:
    """자리표시자를 사람이 찾기 쉬운 묶음으로 나눈다."""
    모든열 = list(table_columns(registry))
    기본 = ["이름", "한글_이름", "영문_이름", "생년월일", "전화번호", "이메일"]
    학력 = [c for c in 모든열 if c.startswith(("현재_", "박사_", "석사_", "학사_"))]
    연구 = [c for c in 모든열
           if c.startswith("1저자_") or c in ("연구분야_키워드", "경력_요약")]
    쓴것 = set(기본) | set(학력) | set(연구)
    나머지 = [c for c in 모든열 if c not in 쓴것]
    묶음 = [
        ("지원자", [c for c in 기본 if c == "이름" or c in 모든열]),
        ("현재·학력", 학력),
        ("연구·경력", 연구),
        ("채용", ["부서", "과제", "최종상태"]),
    ]
    if 나머지:
        묶음.append(("그 밖의 열", 나머지))
    if store.field_names():
        묶음.append(("추가한 열", store.field_names()))
    return [(이름, 항목) for 이름, 항목 in 묶음 if 항목]


def _mail_var_names() -> list[str]:
    return [v for _, 항목 in _mail_var_groups() for v in 항목]


def _mail_template_page(tid: int, me: User, error: str = "", msg: str = "") -> bytes:
    """메일 쓰듯이 꾸며서 작성한다 (글꼴·색·표·그림)."""
    tpl = mailing.template(tid)
    if tpl is None:
        return _page("없음", "<div class='card'>템플릿을 찾을 수 없습니다.</div>", me=me)

    묶음 = _mail_var_groups()
    변수 = set(_mail_var_names())
    모르는것 = [v for v in tpl.placeholders() if v not in 변수]
    경고 = (
        f"<div class='warn'>모르는 자리표시자가 있습니다: "
        f"<b>{html.escape(', '.join(모르는것))}</b> — 이대로 보내면 그 자리는 빈칸이 되고,"
        f" 해당 지원자는 발송 대상에서 빠집니다.</div>"
        if 모르는것 else ""
    )

    글꼴 = [
        "맑은 고딕", "굴림", "굴림체", "돋움", "돋움체", "바탕", "바탕체", "궁서",
        "나눔고딕", "나눔명조", "함초롬바탕",
        "Arial", "Helvetica", "Verdana", "Tahoma", "Trebuchet MS",
        "Times New Roman", "Georgia", "Courier New", "Consolas",
    ]
    크기 = ["8pt", "9pt", "10pt", "11pt", "12pt", "14pt", "16pt", "18pt",
          "20pt", "24pt", "28pt", "32pt", "36pt", "48pt"]

    def 단추(cmd: str, 표시: str, 도움말: str) -> str:
        return (f"<button type='button' title='{도움말}'"
                f" onclick=\"rtCmd('{cmd}')\">{표시}</button>")

    def 드롭(btn_id: str, 라벨: str, 함수: str, 도움말: str, 너비: str = "") -> str:
        스타일 = f" style='min-width:{너비}'" if 너비 else ""
        return (f"<button type='button' class='rt-drop' id='{btn_id}'"
                f" title='{도움말}' onclick='{함수}(this)'{스타일}>"
                f"<span>{라벨}</span><i>▾</i></button>")

    도구 = (
        드롭("rt-font-btn", "맑은 고딕", "rtFontMenu", "글꼴", "104px")
        + 드롭("rt-size-btn", "12pt", "rtSizeMenu", "글씨 크기", "62px")
        + "<span class='rt-sep'></span>"
        + 단추("bold", "<b>가</b>", "굵게 (Ctrl+B)")
        + 단추("italic", "<i>가</i>", "기울임 (Ctrl+I)")
        + 단추("underline", "<u>가</u>", "밑줄 (Ctrl+U)")
        + 단추("strikeThrough", "<s>가</s>", "취소선")
        + 드롭("rt-fore-btn", "<span class='rt-ink' style='color:#b91c1c'>가</span>",
              "rtColorMenuFore", "글자색")
        + 드롭("rt-back-btn", "<span class='rt-ink' style='background:#fef08a'>가</span>",
              "rtColorMenuBack", "배경색")
        + "<span class='rt-sep'></span>"
        + 단추("justifyLeft", "≡", "왼쪽 정렬")
        + 단추("justifyCenter", "☰", "가운데 정렬")
        + 단추("justifyRight", "≣", "오른쪽 정렬")
        + 단추("insertUnorderedList", "•", "글머리 기호")
        + 단추("insertOrderedList", "1.", "번호 매기기")
        + 단추("outdent", "⇤", "내어쓰기")
        + 단추("indent", "⇥", "들여쓰기")
        + "<span class='rt-sep'></span>"
        + "<button type='button' title='링크' onclick='rtLink()'>링크</button>"
        + 드롭("rt-table-btn", "표", "rtTableMenu", "표 넣기")
        + "<label title='그림 넣기' class='btnlike'>그림"
          "<input type='file' accept='image/*' style='display:none'"
          " onchange='rtImage(this)'></label>"
        + 단추("removeFormat", "지우기", "꾸미기 지우기")
        + "<span style='flex:1'></span>"
        + "<button type='button' class='rt-drop rt-var' id='rt-var-btn'"
          " onclick='rtVars(this)'>＋ 자리표시자</button>"
    )

    첨부 = mailing.attachments(tpl.id)
    첨부행 = "".join(
        f"<tr><td><a href='/mail/attachment?id={a['id']}'>{html.escape(a['파일명'])}</a></td>"
        f"<td class='muted'>{a['크기'] // 1024 or 1}KB</td>"
        f"<td class='muted'>{html.escape(a['올린일시'])}</td>"
        f"<td><form method='post' action='/mail/attachment/delete'"
        f" onsubmit=\"return confirm('이 첨부를 뺍니다.')\">"
        f"<input type='hidden' name='id' value='{a['id']}'>"
        f"<input type='hidden' name='template' value='{tpl.id}'>"
        f"<button class='danger'>빼기</button></form></td></tr>"
        for a in 첨부
    ) or "<tr><td colspan='4' class='muted'>붙인 파일이 없습니다.</td></tr>"

    알림 = f"<div class='done'>{html.escape(msg)}</div>" if msg else ""
    오류 = f"<p class='flag'>{html.escape(error)}</p>" if error else ""
    변수JSON = json.dumps([[이름, 항목] for 이름, 항목 in 묶음], ensure_ascii=False)
    글꼴JSON = json.dumps(글꼴, ensure_ascii=False)
    크기JSON = json.dumps(크기, ensure_ascii=False)

    return _page(
        f"{tpl.이름} 템플릿",
        알림 + 경고
        + "<div class='card'><h2>템플릿 편집</h2>" + 오류
        + "<form method='post' action='/mail/template/save'>"
        f"<input type='hidden' name='id' value='{tpl.id}'>"
        "<p><label>템플릿 이름<br><input type='text' name='name'"
        f" value='{html.escape(tpl.이름)}' required style='width:360px'"
        f" data-orig='{html.escape(tpl.이름)}' oninput='markDirty(this)'></label></p>"
        "<p><label>참조 (CC)<br><input type='text' name='cc'"
        f" value='{html.escape(tpl.참조)}' style='width:100%'"
        " placeholder='team@회사.com, hr@회사.com — 쉼표나 세미콜론으로 구분'"
        f" data-orig='{html.escape(tpl.참조)}' oninput='markDirty(this)'></label>"
        "<br><span class='muted'>이 템플릿으로 보내는 모든 메일에 함께 들어갑니다.</span></p>"
        "<p><label>제목<br><input type='text' name='subject'"
        f" value='{html.escape(tpl.제목)}' style='width:100%'"
        " placeholder='예: [{{부서}}] 서류 전형 결과 안내'"
        f" data-orig='{html.escape(tpl.제목)}' oninput='markDirty(this)'></label></p>"
        "<p>본문</p>"
        f"<div class='rt'><div class='rt-bar'>{도구}</div>"
        f"<div class='rt-body' id='rtbody' contenteditable='true'>{tpl.본문}</div></div>"
        f"<input type='hidden' name='body' id='bodyfield' data-orig=''>"
        "<p><label><input type='checkbox' name='reject' value='1'"
        f"{' checked' if tpl.탈락메일 else ''}> <b>탈락 메일</b> — 이걸 받은 지원자에게는"
        " 이후 어떤 메일도 나가지 않습니다</label></p>"
        "<p><button type='submit'>저장</button> "
        f"<a class='btn sec' href='/mail/send?id={tpl.id}'>보내기</a> "
        "<a class='btn sec' href='/mail'>목록</a></p></form>"
        "<form method='post' action='/mail/template/delete' style='margin-top:10px'"
        " onsubmit=\"return confirm('이 템플릿을 지웁니다. 이미 보낸 기록은 남습니다.')\">"
        f"<input type='hidden' name='id' value='{tpl.id}'>"
        "<button class='danger'>템플릿 삭제</button>"
        "<span class='muted'> 발송 기록은 남습니다 — 누구에게 뭘 보냈는지는 기록입니다.</span>"
        "</form></div>"

        + "<div class='card'><h2>첨부파일</h2>"
        "<form method='post' action='/mail/attachment/add' enctype='multipart/form-data'>"
        f"<input type='hidden' name='template' value='{tpl.id}'>"
        "<input type='file' name='files' multiple>"
        "<button type='submit'>붙이기</button>"
        "<span class='muted'> 이 템플릿으로 보내는 모든 메일에 함께 갑니다. "
        "한 개 10MB 까지.</span></form>"
        "<div class='scroll' style='margin-top:10px'><table data-name='첨부파일'>"
        "<tr><th>파일</th><th>크기</th><th>붙인 일시</th><th></th></tr>"
        + 첨부행 + "</table></div>"
        "<p class='muted'>본문에 넣는 그림은 <b>그림</b> 단추를 쓰세요(1MB 까지). "
        "큰 파일은 여기에 붙입니다.</p>"
        "<div class='warn' style='margin-top:8px'>본문에 <b>박아 넣은 그림</b>은 "
        "Outlook 등 일부 메일 프로그램이 <b>차단해서 안 보일 수 있습니다</b>. "
        "꼭 봐야 하는 그림이면 여기 <b>첨부로도 함께</b> 붙여 두세요.</div></div>"
        f"<script>window.자리표시자 = {변수JSON};"
f"window.rtFonts = {글꼴JSON};window.rtSizes = {크기JSON};"
f"function rtColorMenuFore(b){{rtColorMenu(b, 'foreColor');}}"
f"function rtColorMenuBack(b){{rtColorMenu(b, 'hiliteColor');}}"
f"{_MAIL_JS}</script>",
        me=me,
    )


def _mail_request_preview(tpl) -> str:
    """실제로 API 에 보낼 URL 과 본문을 그대로 보여준다.

    '정말 나가는 게 맞나' 를 눈으로 확인할 유일한 방법이라, 토큰만 가리고
    나머지는 손대지 않는다.
    """
    결과 = mailapi.send("보낼주소@example.com", tpl.제목 or "(제목)",
                      tpl.본문 or "(본문)", html=tpl.html, 참조=tpl.cc(),
                      첨부=mailing.attachment_bytes(tpl.id), dry_run=True)
    본문 = 결과.본문
    if len(본문) > 4000:
        본문 = 본문[:4000] + " … (첨부가 커서 줄임)"
    토큰 = settings.mail_api_token
    가린토큰 = (토큰[:3] + "…" + str(len(토큰)) + "자") if 토큰 else "(비어 있음)"
    설정 = "".join(
        f"<tr><th>{이름}</th><td>{html.escape(값)}</td></tr>"
        for 이름, 값 in (
            ("MAIL_API_URL", settings.mail_api_url or "(비어 있음)"),
            ("MAIL_API_TOKEN", 가린토큰),
            ("MAIL_API_SYSTEM_ID", settings.mail_api_system_id or "(비어 있음)"),
            ("MAIL_API_USER_ID", settings.mail_api_user_id or "(비어 있음)"),
            ("MAIL_SENDER", settings.mail_sender or "(안 씀)"),
            ("MAIL_DRY_RUN", "1 (보내지 않음)" if settings.mail_dry_run else "0 (실제로 보냄)"),
        )
    )
    return (
        "<div class='card'><h2>보낼 요청 내용</h2>"
        "<p class='muted'>사내 API 명세와 이 내용을 맞춰 보세요. 필드 이름이나 인증 방식이"
        " 다르면 <code>cvtool/clients/mail.py</code> 의 <code>build_payload()</code> 만"
        " 고치면 됩니다.</p>"
        f"<table style='margin-bottom:12px'>{설정}</table>"
        "<p><b>POST</b> <code>" + html.escape(결과.요청URL or "(URL 이 비어 있음)") + "</code></p>"
        "<p><b>헤더</b> <code>Content-Type: application/json; charset=utf-8</code><br>"
        "<code>Authorization: Bearer &lt;MAIL_API_TOKEN&gt;</code></p>"
        "<p><b>본문</b> (JSON 을 문자열로 직렬화해 그대로 보냅니다)</p>"
        f"<pre style='white-space:pre-wrap;word-break:break-all;background:#f6f7f9;"
        f"border:1px solid var(--line);border-radius:6px;padding:12px;font-size:12px'>"
        f"{html.escape(본문)}</pre></div>"
    )


def _mail_send_page(tid: int, me: User, error: str = "", msg: str = "",
                    peek: bool = False) -> bytes:
    """지원자를 고르고 미리보기한 뒤 보낸다."""
    tpl = mailing.template(tid)
    if tpl is None:
        return _page("없음", "<div class='card'>템플릿을 찾을 수 없습니다.</div>", me=me)

    진행맵 = recruit.all()
    보이는과제 = auth.visible_project_ids(me)
    records = store.list_all()
    if 보이는과제 is not None:
        records = [r for r in records
                   if 진행맵.get(r.지원자_ID)
                   and 진행맵[r.지원자_ID].project_id in 보이는과제]

    rows = []
    보낼수있음 = 0
    첫번째 = 미리본문 = ""
    for rec in records:
        cid = rec.지원자_ID
        값 = _mail_vars(rec, 진행맵)
        받는사람 = (값.get("이메일") or "").split(MULTI_SEP)[0].strip()
        제목, 빈1 = render(tpl.제목, 값)
        본문, 빈2 = render(tpl.본문, 값)
        빈칸 = list(dict.fromkeys(빈1 + 빈2))
        막힘 = mailing.blocked_reason(cid, tpl)
        if not 막힘 and not 받는사람:
            막힘 = "이메일 주소가 없습니다"
        if not 막힘 and 빈칸:
            막힘 = f"값이 빈 자리표시자: {', '.join(빈칸)}"
        if not 막힘:
            보낼수있음 += 1
            첫번째 = 첫번째 or 본문
        미리본문 = 미리본문 or 본문        # 아무도 못 보내도 본문은 보여준다
        체크 = (
            f"<input type='checkbox' form='sendform' name='ids' value='{html.escape(cid)}'>"
            if not 막힘 else ""
        )
        미리 = html_to_text(본문) if tpl.html else 본문
        rows.append(
            f"<tr{' class=dup' if 막힘 else ''}><td>{체크}</td>"
            f"<td>{html.escape(값.get('한글_이름') or 값.get('영문_이름') or cid)}</td>"
            f"<td>{html.escape(받는사람)}</td>"
            f"<td style='white-space:normal'>{html.escape(제목)}</td>"
            f"<td style='white-space:normal' class='muted'>{html.escape(미리[:120])}"
            f"{'…' if len(미리) > 120 else ''}</td>"
            f"<td class='flag' style='white-space:normal'>{html.escape(막힘)}</td></tr>"
        )

    첨부 = mailing.attachments(tpl.id)
    딸림 = []
    if tpl.cc():
        딸림.append("참조 <b>" + html.escape(", ".join(tpl.cc())) + "</b>")
    if 첨부:
        딸림.append("첨부 <b>"
                  + html.escape(", ".join(a["파일명"] for a in 첨부)) + "</b>")
    딸림칸 = f"<p class='muted'>{' · '.join(딸림)}</p>" if 딸림 else ""

    본문미리 = 첫번째 or 미리본문
    미리보기 = (
        "<div class='card'><h2>본문 미리보기 "
        + ("<span class='muted'>보낼 수 있는 첫 번째 지원자 기준</span>" if 첫번째
           else "<span class='muted'>첫 지원자 기준 — 지금은 보낼 수 있는 사람이 없습니다</span>")
        + f"</h2><div class='mailbody'>{본문미리}</div></div>"
        if 본문미리 and tpl.html else ""
    )

    알림 = f"<div class='done'>{html.escape(msg)}</div>" if msg else ""
    오류 = f"<div class='warn'>{html.escape(error)}</div>" if error else ""
    연습 = (
        "<div class='warn'><b>연습 모드 (MAIL_DRY_RUN=1)</b> — 실제로 나가지 않고 "
        "기록만 남습니다. 기록이 남으면 '이미 보냄' 으로 처리되니 주의하세요.</div>"
        if settings.mail_dry_run else ""
    )
    탈락표시 = (
        "<div class='warn'><b>이 템플릿은 탈락 메일입니다.</b> 보내고 나면 그 지원자에게는 "
        "이후 어떤 메일도 보낼 수 없습니다.</div>" if tpl.탈락메일 else ""
    )
    보내기 = (
        "<form method='post' action='/mail/send' id='sendform' class='mergebar'"
        " onsubmit=\"return confirm('선택한 지원자에게 메일을 보냅니다. "
        "되돌릴 수 없습니다. 진행할까요?')\">"
        f"<input type='hidden' name='id' value='{tpl.id}'>"
        "<button type='submit'>선택한 지원자에게 보내기</button>"
        f"<span class='muted'>보낼 수 있는 지원자 {보낼수있음}명. "
        "빨간 줄은 보낼 수 없는 이유가 적혀 있습니다.</span></form>"
        if can(me, "메일_발송") else ""
    )
    시험 = (
        "<div class='card'><h2>시험 발송</h2>"
        "<form method='post' action='/mail/test' style='display:flex;gap:8px;flex-wrap:wrap'>"
        f"<input type='hidden' name='id' value='{tpl.id}'>"
        "<input type='text' name='to' placeholder='내 주소를 넣으세요' required"
        " style='width:280px'>"
        "<button type='submit'>이 주소로 한 통 보내보기</button></form>"
        "<p class='muted'>지원자에게는 가지 않고 <b>발송 기록도 남지 않습니다.</b> "
        "설정이 맞는지 먼저 이걸로 확인하세요. "
        + ("지금은 연습 모드라 실제로 나가지 않고 요청 내용만 보여줍니다."
           if settings.mail_dry_run else
           "<b>지금은 실제로 나갑니다 (MAIL_DRY_RUN=0).</b>")
        + f" <a href='/mail/send?id={tpl.id}&peek=1'>보낼 요청 내용 보기</a></p></div>"
        if can(me, "메일_발송") else ""
    )
    점검 = _mail_request_preview(tpl) if peek else ""
    return _page(
        f"{tpl.이름} 보내기",
        알림 + 오류 + 연습 + 탈락표시 + 시험 + 점검
        + f"<div class='card'><h2>{html.escape(tpl.이름)} "
        f"<span class='muted'>{html.escape(tpl.제목)}</span></h2>"
        + 딸림칸
        + f"<p><a class='btn sec' href='/mail/template?id={tpl.id}'>템플릿 고치기</a> "
        "<a class='btn sec' href='/mail'>목록</a></p>"
        + 보내기
        + "<div class='scroll'><table data-name='메일 발송 대상'>"
        "<tr><th style='width:34px'><input type='checkbox' title='전체 선택'"
        " onclick=\"for(const c of this.closest('table')"
        ".querySelectorAll('input[name=ids]'))"
        "if(!c.closest('tr').classList.contains('hide'))c.checked=this.checked\"></th>"
        "<th>지원자</th><th>받는 주소</th><th>제목</th><th>본문 미리보기</th>"
        "<th>보낼 수 없는 이유</th></tr>"
        + "".join(rows) + "</table></div></div>"
        + 미리보기,
        me=me,
    )


def _mail_log_page(me: User) -> bytes:
    기록 = mailing.history(limit=500)
    탈락배지 = " <span class='pill p-미분류'>탈락</span>"
    이름맵 = {r.지원자_ID: (r.한글_이름 or r.영문_이름 or r.지원자_ID)
             for r in store.list_all()}
    rows = "".join(
        f"<tr><td>{html.escape(r['보낸일시'])}</td>"
        f"<td>{html.escape(이름맵.get(r['지원자_ID'], r['지원자_ID']))}</td>"
        f"<td>{html.escape(r['받는사람'])}</td>"
        f"<td>{html.escape(r['템플릿이름'])}"
        f"{탈락배지 if r['탈락메일'] else ''}</td>"
        f"<td>{html.escape(r['상태'])}</td>"
        f"<td style='white-space:normal' class='muted'>{html.escape(r['오류'] or '')}</td>"
        f"<td class='muted'>{html.escape(r['보낸이'])}</td></tr>"
        for r in 기록
    ) or "<tr><td colspan='7' class='muted'>보낸 메일이 없습니다.</td></tr>"
    return _page(
        "메일 발송 이력",
        f"<div class='card'><h2>발송 이력 <span class='muted'>총 {mailing.count()}건</span></h2>"
        "<p><a class='btn sec' href='/mail'>템플릿 목록</a></p>"
        "<div class='scroll'><table data-name='메일 발송 이력'>"
        "<tr><th>보낸 일시</th><th>지원자</th><th>받는 주소</th><th>템플릿</th>"
        "<th>상태</th><th>메모</th><th>보낸 사람</th></tr>" + rows + "</table></div></div>",
        me=me,
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
            "<li style='margin-bottom:6px'>"
            "<form method='post' action='/org/project/rename' style='display:flex;gap:6px'>"
            f"<input type='hidden' name='id' value='{p['id']}'>"
            f"<input type='text' name='name' value='{html.escape(p['이름'])}' style='width:200px'>"
            "<input type='password' name='invite' placeholder='초대암호 변경(비우면 유지)'>"
            "<button type='submit'>이름/암호 저장</button></form>"
            + (" <span class='muted'>초대암호 있음</span>" if p["초대암호"] else "")
            + " <form method='post' action='/org/project/delete' style='display:inline'"
            " onsubmit=\"return confirm('과제를 삭제합니다. 배정도 함께 지워집니다.')\">"
            f"<input type='hidden' name='id' value='{p['id']}'>"
            "<button class='danger'>삭제</button></form></li>"
            for p in 소속
        ) or "<li class='muted'>과제 없음</li>"
        카드.append(
            # 폼 안에 폼을 넣으면 브라우저가 안쪽을 버린다. 그래서 '부서 삭제' 를
            # 눌러도 삭제가 아니라 이름 저장이 실행되고 있었다. 나란히 둔다.
            "<div class='card'><h2 style='display:flex;gap:6px;align-items:center'>"
            "<form method='post' action='/org/dept/rename' style='display:flex;gap:6px'>"
            f"<input type='hidden' name='id' value='{d['id']}'>"
            f"<input type='text' name='name' value='{html.escape(d['이름'])}' style='width:220px'>"
            "<button type='submit'>부서명 저장</button></form>"
            "<form method='post' action='/org/dept/delete'"
            " onsubmit=\"return confirm('부서를 삭제하면 그 아래 과제와 배정도 함께 지워집니다.')\">"
            f"<input type='hidden' name='id' value='{d['id']}'>"
            "<button class='danger'>부서 삭제</button></form>"
            f"<span class='muted'>과제 {len(소속)}개</span></h2><ul>{항목}</ul>"
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



#: 채용 현황 화면 전용 — 부서를 바꾸면 과제 목록이 따라 바뀐다
_RECRUIT_JS = """
function syncProjects(sel){
  var cid = sel.dataset.cid;
  var proj = document.getElementById('proj-' + cid);
  if(!proj) return;
  var 목록 = (window.과제표 || 과제표)[sel.value] || [];
  proj.innerHTML = '';
  var 빈 = document.createElement('option');
  빈.value = ''; 빈.textContent = '-';
  proj.appendChild(빈);
  목록.forEach(function(pair){
    var op = document.createElement('option');
    op.value = pair[0]; op.textContent = pair[1];
    proj.appendChild(op);
  });
  proj.value = '';
  markDirty(proj);
}
"""


def _recruit_rows(me: User, sort: str = ""):
    """채용 현황 표에 나갈 (레코드, 진행, 값 함수) 를 만든다.

    화면과 엑셀이 같은 데이터를 보게 하려고 한 곳에서 만든다.
    """
    보이는과제 = auth.visible_project_ids(me)      # None 이면 전부
    진행맵 = recruit.all()
    부서명 = {d["id"]: d["이름"] for d in auth.departments()}
    과제명 = {p["id"]: p["이름"] for p in auth.projects()}

    records = store.list_all()
    if 보이는과제 is not None:
        records = [
            r for r in records
            if (진행맵.get(r.지원자_ID) and 진행맵[r.지원자_ID].project_id in 보이는과제)
        ]

    사용자값맵 = store.custom_map()
    사용자열이름 = set(store.field_names())

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
        if col in 사용자열이름:
            return 사용자값맵.get(rec.지원자_ID, {}).get(col, "")
        return str(rec.to_row(registry).get(col, "") or "")

    def 정렬키(rec):
        p = 진행맵.get(rec.지원자_ID)
        기본 = p.정렬키() if p else (0, 0, 0)
        if sort:
            return (기본[0], 값(rec, sort).lower())   # 불합격은 어떤 정렬에서도 아래로
        return 기본

    records.sort(key=정렬키)
    return records, 진행맵, 값


def _recruit_page(me: User, sort: str = "", error: str = "", msg: str = "") -> bytes:
    """채용 현황 관리.

    현업은 배정된 과제의 지원자만 보인다. 기본 정렬은 불합격을 맨 아래로 내린다.

    화면 규칙:
      - **저장 버튼은 맨 위 하나뿐이다.** 여러 사람 상태를 바꾸고 한 번에 저장한다.
      - **지원자 정보 열은 여기서 못 고친다.** 채용 상태를 보는 화면이라
        지원자 정보까지 고칠 수 있으면 실수로 덮어쓰기 쉽다.
        고칠 일이 있으면 지원자 목록이나 상세 화면에서 한다.
    """
    records, 진행맵, 값 = _recruit_rows(me, sort)
    보이는과제 = auth.visible_project_ids(me)
    depts = auth.departments()
    projects = auth.projects()

    표열 = recruit.columns()
    수정가능 = can(me, "채용현황_수정")
    담당자 = can(me, "지원자_수정")

    부서옵션전체 = "".join(
        f"<option value='{d['id']}'>{html.escape(d['이름'])}</option>" for d in depts
    )
    과제_by_부서: dict[int, list] = {}
    for pr in projects:
        과제_by_부서.setdefault(pr["부서_id"], []).append(pr)
    # 부서를 바꾸면 과제 목록이 따라 바뀌어야 한다 (페이지를 새로 그리지 않고)
    과제표 = json.dumps(
        {str(k): [[pr["id"], pr["이름"]] for pr in v] for k, v in 과제_by_부서.items()},
        ensure_ascii=False,
    )

    rows = []
    for rec in records:
        p = 진행맵.get(rec.지원자_ID)
        cid = rec.지원자_ID
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
                    f"<td class='ctl'><select form='recruitform'"
                    f" name='단계_{html.escape(cid)}_{html.escape(col)}'"
                    f" data-orig='{v}' onchange='markDirty(this)'>{opts}</select></td>"
                )
            elif col == "부서" and 담당자:
                현재부서 = p.부서_id if p else None
                옵션 = "".join(
                    f"<option value='{d['id']}'{' selected' if d['id'] == 현재부서 else ''}>"
                    f"{html.escape(d['이름'])}</option>" for d in depts
                )
                cells.append(
                    f"<td class='ctl'><select form='recruitform'"
                    f" name='부서_{html.escape(cid)}' data-orig='{v}'"
                    f" onchange=\"markDirty(this);syncProjects(this)\""
                    f" data-cid='{html.escape(cid)}'>"
                    f"<option value=''>-</option>{옵션}</select></td>"
                )
            elif col == "과제" and 담당자:
                현재부서 = p.부서_id if p else None
                현재과제 = p.project_id if p else None
                과제옵션 = "".join(
                    f"<option value='{pr['id']}'{' selected' if pr['id'] == 현재과제 else ''}>"
                    f"{html.escape(pr['이름'])}</option>"
                    for pr in 과제_by_부서.get(현재부서, [])
                )
                cells.append(
                    f"<td class='ctl'><select form='recruitform'"
                    f" name='과제_{html.escape(cid)}' id='proj-{html.escape(cid)}'"
                    f" data-orig='{v}' onchange='markDirty(this)'>"
                    f"<option value=''>-</option>{과제옵션}</select></td>"
                )
            elif col == "비고" and 수정가능:
                cells.append(
                    f"<td class='ctl'><input type='text' form='recruitform'"
                    f" name='비고_{html.escape(cid)}' value='{v}' style='width:180px'"
                    f" data-orig='{v}' oninput='markDirty(this)'></td>"
                )
            elif col == "최종상태":
                cls = " class='flag'" if p and p.탈락 else ""
                cells.append(f"<td{cls}>{v}</td>")
            else:
                # 지원자 정보 열은 보기만 한다 (고치려면 지원자 목록/상세에서)
                cells.append(f"<td title='{v}'>{v}</td>")
        링크 = f"<td><a href='/candidate?id={urllib.parse.quote(cid)}'>상세</a></td>"
        묶음 = " class='dup'" if p and p.탈락 else ""
        rows.append(f"<tr{묶음}>{링크}{''.join(cells)}</tr>")

    머리 = "<th></th>" + "".join(f"<th>{html.escape(c)}</th>" for c in 표열)
    알림 = f"<div class='done'>{html.escape(msg)}</div>" if msg else ""
    오류 = f"<div class='warn'>{html.escape(error)}</div>" if error else ""
    안내 = (
        "배정된 과제의 지원자만 보입니다."
        if 보이는과제 is not None
        else "열 제목을 눌러 정렬하고, 표 위 칸으로 걸러 봅니다. 불합격자는 항상 아래로 갑니다."
    )
    if not (수정가능 or 담당자):
        안내 += " 보기 전용입니다."
    else:
        안내 += " 지원자 정보 열은 여기서 고칠 수 없습니다 (지원자 목록에서 고치세요)."
    저장바 = (
        "<form method='post' action='/recruit/save' id='recruitform' class='mergebar'>"
        "<button type='submit'>고친 내용 저장</button>"
        "<span class='muted'>여러 줄을 고친 뒤 <b>한 번만</b> 누르세요. "
        "고친 칸은 노랗게 표시됩니다.</span></form>"
        if rows and (수정가능 or 담당자) else ""
    )
    열구성 = (
        "<a class='btn sec' href='/recruit/columns'>표 열 구성</a> "
        if can(me, "열_구성") else ""
    )
    표 = (
        f"<div class='scroll'><table data-name='채용현황'><tr>{머리}</tr>{''.join(rows)}</table></div>"
        if rows else "<p class='muted'>표시할 지원자가 없습니다.</p>"
    )
    return _page(
        "채용 현황",
        f"""{알림}{오류}<div class='card'><h2>채용 현황 <span class='muted'>{len(records)}명</span></h2>
        <p class='muted'>{안내}</p>
        <p>{열구성}<a class='btn' href='/recruit/export.xlsx'>엑셀(.xlsx) 다운로드</a></p>
        {저장바}{표}</div>
        <script>var 과제표 = {과제표};{_RECRUIT_JS}</script>""",
        me=me,
    )


def _recruit_columns_page(me: User) -> bytes:
    """관리자가 채용 현황 표에 보일 열과 순서를 정한다."""
    전체 = list(table_columns(registry)) + list(RECRUIT_COLUMNS) + store.field_names()
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



def _fields_page(me: User, error: str = "", msg: str = "") -> bytes:
    """표에 나갈 열을 관리한다 — **기본 열과 추가한 열을 한 자리에서.**

    예전에는 추가한 열만 보였다. 기본 열도 이름이 마음에 안 들거나 안 쓰는 게
    있어서, 같은 화면에서 보이는 이름·숨김·순서를 정할 수 있어야 한다.

    다만 기본 열의 **입력 형식은 바꾸지 않는다.** 형식 검사와 추출 스키마가
    그 열에 걸려 있어서, 여기서 바꾸면 이미 들어 있는 값과 어긋난다.
    """
    기본열 = list(table_columns(registry))
    사용자열 = {f["이름"]: f for f in store.fields()}
    cfg = store.column_config()
    유형옵션 = "".join(f"<option>{t}</option>" for t in CUSTOM_TYPES)

    def 설명(col: str) -> str:
        if col in 사용자열:
            f = 사용자열[col]
            선택지 = f["선택지"] or ""
            return (f"{f['유형']}" + (f" · {html.escape(선택지)}" if 선택지 else "")
                    + f"<br><span class='muted'>{html.escape(f['만든일시'])}"
                    + (f" ({html.escape(f['만든이'])})" if f["만든이"] else "") + "</span>")
        if col in CHOICE_FIELDS:
            return "선택 · " + html.escape(", ".join(v or "(빈칸)" for v in CHOICE_FIELDS[col]))
        if col in REGISTRY_FIELDS:
            return "명칭 사전 " + html.escape(NAME_COLUMNS[col])
        if col.startswith(TIER_COLUMN_PREFIX):
            return "<span class='muted'>계산 결과 (논문 목록에서 셈)</span>"
        spec = field_spec(col)
        return html.escape(spec.도움말 or "텍스트")

    rows = []
    for i, col in enumerate(기본열 + list(사용자열), start=1):
        c = cfg.get(col, {})
        추가열 = col in 사용자열
        rows.append(
            f"<tr>"
            f"<td>{html.escape(col)}</td>"
            f"<td><span class='pill {'p-완료' if 추가열 else 'p-대기중'}'>"
            f"{'추가한 열' if 추가열 else '기본 열'}</span></td>"
            f"<td style='white-space:normal'>{설명(col)}</td>"
            f"<td class='ctl'><input type='hidden' form='colform' name='col'"
            f" value='{html.escape(col)}'>"
            f"<input type='text' form='colform' name='label_{i}'"
            f" value='{html.escape(c.get('표시이름') or '')}'"
            f" placeholder='{html.escape(col)}' style='width:170px'"
            f" data-orig='{html.escape(c.get('표시이름') or '')}' oninput='markDirty(this)'></td>"
            f"<td class='ctl'><input type='number' form='colform' name='order_{i}'"
            f" value='{c.get('순서') or ''}' placeholder='-' style='width:70px' min='0'"
            f" data-orig='{c.get('순서') or ''}' oninput='markDirty(this)'></td>"
            f"<td><label><input type='checkbox' form='colform' name='hide_{i}'"
            f"{' checked' if c.get('숨김') else ''} onchange='markDirty(this)'"
            f" data-orig=''> 숨김</label></td>"
            f"<td>" + (
                "<form method='post' action='/fields/delete' style='display:inline'"
                " onsubmit=\"return confirm('이 열과 여기 들어있던 모든 값이 지워집니다.')\">"
                f"<input type='hidden' name='name' value='{html.escape(col)}'>"
                "<button class='danger'>삭제</button></form>" if 추가열
                else "<span class='muted'>-</span>"
            ) + "</td></tr>"
        )

    알림 = f"<div class='done'>{html.escape(msg)}</div>" if msg else ""
    오류 = f"<p class='flag'>{html.escape(error)}</p>" if error else ""
    return _page(
        "표 항목",
        알림
        + "<div class='card'><h2>열 추가</h2>" + 오류
        + "<form method='post' action='/fields/add' style='display:flex;gap:8px;flex-wrap:wrap'>"
        "<input type='text' name='name' placeholder='열 이름' required>"
        f"<select name='type'>{유형옵션}</select>"
        "<input type='text' name='choices' placeholder=\"선택지 (선택 유형만, | 로 구분)\""
        " style='width:280px'>"
        "<button type='submit'>추가</button></form>"
        "<p class='muted'>유형에 따라 입력칸이 달라지고 형식이 강제됩니다. "
        "<b>값은 사람이 채웁니다</b> — LLM 이 자동으로 채우지 않습니다.</p></div>"

        + "<div class='card'><h2>표에 나갈 열 "
        f"<span class='muted'>기본 {len(기본열)}개 · 추가 {len(사용자열)}개</span></h2>"
        "<form method='post' action='/fields/columns' id='colform' class='mergebar'>"
        "<button type='submit'>고친 내용 저장</button>"
        "<span class='muted'>보이는 이름·순서·숨김을 고치고 <b>한 번만</b> 누르세요. "
        "순서는 작은 번호가 앞이고, 비우면 원래 자리입니다.</span></form>"
        "<div class='scroll'><table data-name='표 항목'>"
        "<tr><th>열 이름</th><th>구분</th><th>입력 형식</th><th class='ctl'>표에 보일 이름</th>"
        "<th class='ctl'>순서</th><th>숨김</th><th></th></tr>"
        + "".join(rows) + "</table></div>"
        "<p class='muted'>기본 열의 <b>입력 형식은 바꾸지 않습니다</b> — 형식 검사와 "
        "추출 스키마가 걸려 있어서 여기서 바꾸면 이미 들어 있는 값과 어긋납니다. "
        "안 쓰는 열은 <b>숨김</b>으로 두세요.</p></div>",
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

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            code=code,
        )

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
            if not can(me, "지원자_목록"):
                return self._redirect(홈(me))    # 현업은 채용 현황으로
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(
                _dashboard(
                    me,
                    q=(params.get("q") or [""])[0],
                    review_only=bool(params.get("review")),
                    년도=(params.get("year") or [""])[0],
                )
            )
        if path == "/upload":
            if not can(me, "지원자_등록"):
                return self._deny()
            return self._send(_upload_page(me))
        if path == "/candidate":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not _볼수있나(me, (params.get("id") or [""])[0]):
                return self._deny("배정된 과제의 지원자만 볼 수 있습니다.")
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
            if not can(me, "변경이력_조회"):
                return self._deny()
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(_history_page(me, (params.get("kind") or [""])[0]))
        if path == "/candidate/file":
            if not _볼수있나(
                me,
                (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                 .get("id") or [""])[0],
            ):
                return self._deny("배정된 과제의 지원자만 볼 수 있습니다.")
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
            if not (can(me, "채용현황_수정") or can(me, "지원자_조회")):
                return self._deny()
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(
                _recruit_page(me, (params.get("sort") or [""])[0],
                              (params.get("err") or [""])[0],
                              (params.get("msg") or [""])[0])
            )
        if path == "/recruit/export.xlsx":
            if not (can(me, "채용현황_수정") or can(me, "지원자_조회")):
                return self._deny()
            표열 = recruit.columns()
            records, _진행, 값 = _recruit_rows(me, (urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).get("sort") or [""])[0])
            rows = [{c: 값(rec, c) for c in 표열} for rec in records]
            stamp = now_kst().strftime("%Y%m%d_%H%M")
            return self._send(
                build_xlsx(rows, 표열),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                extra={"Content-Disposition": f'attachment; filename="recruit_{stamp}.xlsx"'},
            )
        if path == "/mail":
            if not can(me, "메일_템플릿"):
                return self._deny()
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(_mail_page(me, (params.get("err") or [""])[0],
                                         (params.get("msg") or [""])[0]))
        if path == "/mail/template":
            if not can(me, "메일_템플릿"):
                return self._deny()
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                tid = int((params.get("id") or ["0"])[0])
            except ValueError:
                return self._redirect("/mail")
            return self._send(_mail_template_page(tid, me, (params.get("err") or [""])[0],
                                                  (params.get("msg") or [""])[0]))
        if path == "/mail/send":
            if not can(me, "메일_템플릿"):
                return self._deny()
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                tid = int((params.get("id") or ["0"])[0])
            except ValueError:
                return self._redirect("/mail")
            return self._send(_mail_send_page(tid, me, (params.get("err") or [""])[0],
                                              (params.get("msg") or [""])[0],
                                              peek=bool(params.get("peek"))))
        if path == "/mail/attachment":
            if not can(me, "메일_템플릿"):
                return self._deny()
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                att = mailing.attachment(int((params.get("id") or ["0"])[0]))
            except (ValueError, TypeError):
                att = None
            if not att:
                return self._send(_page("없음", "<div class='card'>첨부를 찾을 수 없습니다.</div>"),
                                  code=404)
            path_ = mailing.files_dir / att["저장명"]
            if not path_.is_file():
                return self._send(_page("없음", "<div class='card'>파일이 없습니다.</div>"),
                                  code=404)
            이름 = urllib.parse.quote(att["파일명"])
            return self._send(
                path_.read_bytes(),
                CONTENT_TYPES.get(Path(att["저장명"]).suffix.lower(),
                                  "application/octet-stream"),
                extra={"Content-Disposition":
                       f"attachment; filename=\"file\"; filename*=UTF-8''{이름}"},
            )
        if path == "/mail/log":
            if not can(me, "메일_템플릿"):
                return self._deny()
            return self._send(_mail_log_page(me))
        if path == "/fields":
            if not can(me, "열_구성"):
                return self._deny("표 항목 추가는 관리자만 할 수 있습니다.")
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(_fields_page(me, (params.get("err") or [""])[0],
                                           (params.get("msg") or [""])[0]))
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
            if not _볼수있나(me, att["지원자_ID"]):
                return self._deny("배정된 과제의 지원자만 볼 수 있습니다.")
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
            return self._send(_names_page(
                (params.get("kind") or ["학회"])[0],
                me,
                error=(params.get("err") or [""])[0],
                msg=(params.get("msg") or [""])[0],
            ))
        if path == "/favicon.ico":
            return self._send(b"", "image/x-icon", code=204)
        if path == "/export.xlsx":
            if not can(me, "엑셀_다운로드"):
                return self._deny()
            열 = 표열()
            data = records_to_xlsx(store.list_all(), registry,
                                   (store.field_names(), store.custom_map()),
                                   열=열, 라벨=라벨(열))
            stamp = now_kst().strftime("%Y%m%d_%H%M")
            return self._send(
                data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                extra={"Content-Disposition": f'attachment; filename="cv_{stamp}.xlsx"'},
            )
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
                홈(user),
                {"Set-Cookie": f"cvsession={token}; HttpOnly; Path=/; SameSite=Strict"},
            )

        me = self._user()
        if me is None:
            # 표에서 바로 고치기는 fetch 라 리다이렉트를 받으면 HTML 을 파싱하게 된다.
            if path.startswith("/api/"):
                return self._json({"ok": False, "error": "로그인이 풀렸습니다. 새로고침하세요."},
                                  code=401)
            return self._redirect("/login")

        if path == "/upload":
            if not can(me, "지원자_등록"):
                return self._deny()
            form = parse_multipart(self._read_body(), self.headers.get("Content-Type", ""))
            if not form.files:
                return self._redirect("/upload")
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
            return self._redirect("/upload")

        if path == "/table.xlsx":
            data = urllib.parse.parse_qs(
                self._read_body().decode("utf-8", "replace"), keep_blank_values=True
            )
            이름 = ((data.get("name") or ["표"])[0] or "표").strip()[:40]
            stamp = now_kst().strftime("%Y%m%d_%H%M")
            # 한글 파일명은 RFC 5987 로 따로 보낸다 (옛 브라우저는 ASCII 이름을 쓴다)
            한글 = urllib.parse.quote(f"{이름}_{stamp}.xlsx")
            return self._send(
                _tsv_to_xlsx((data.get("tsv") or [""])[0]),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                extra={"Content-Disposition":
                       f'attachment; filename="table_{stamp}.xlsx";'
                       f" filename*=UTF-8''{한글}"},
            )

        if path == "/recruit/save":
            # 표 전체가 한 번에 온다. 실제로 값이 달라진 것만 저장한다.
            if not (can(me, "채용현황_수정") or can(me, "지원자_수정")):
                return self._deny()
            data = urllib.parse.parse_qs(
                self._read_body().decode("utf-8", "replace"), keep_blank_values=True
            )
            보이는 = auth.visible_project_ids(me)
            바뀐것: list[str] = []
            이름맵 = {r.지원자_ID: (r.한글_이름 or r.영문_이름 or r.지원자_ID)
                     for r in store.list_all()}

            def 볼수있나(cid: str) -> bool:
                return 보이는 is None or recruit.get(cid).project_id in 보이는

            # 1) 단계 상태
            if can(me, "채용현황_수정"):
                for key, 값들 in data.items():
                    if not key.startswith("단계_"):
                        continue
                    몸통 = key[len("단계_"):]
                    for 단계 in STAGES:
                        if 몸통.endswith("_" + 단계):
                            cid = 몸통[: -(len(단계) + 1)]
                            break
                    else:
                        continue
                    if not 볼수있나(cid):
                        continue
                    try:
                        이전 = recruit.set_stage(cid, 단계, 값들[0], me.아이디)
                    except ValueError as exc:
                        return self._redirect("/recruit?err=" + urllib.parse.quote(str(exc)))
                    if 이전 != 값들[0]:
                        audit.record(me.아이디, "채용현황", cid, 항목=단계,
                                     이전값=이전, 새값=값들[0])
                        바뀐것.append(f"{이름맵.get(cid, cid)} {단계} {값들[0] or '(빈칸)'}")

                # 2) 비고
                for key, 값들 in data.items():
                    if not key.startswith("비고_"):
                        continue
                    cid = key[len("비고_"):]
                    if not 볼수있나(cid):
                        continue
                    이전 = recruit.set_note(cid, 값들[0], me.아이디)
                    if 이전 != 값들[0]:
                        audit.record(me.아이디, "채용현황", cid, 항목="비고",
                                     이전값=이전, 새값=값들[0])
                        바뀐것.append(f"{이름맵.get(cid, cid)} 비고")

            # 3) 부서 / 과제 (지원자 수정 권한이 있어야 배정할 수 있다)
            if can(me, "지원자_수정"):
                for key, 값들 in data.items():
                    if not key.startswith("부서_"):
                        continue
                    cid = key[len("부서_"):]
                    dept = 값들[0]
                    proj = (data.get(f"과제_{cid}") or [""])[0]
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
                        바뀐것.append(f"{이름맵.get(cid, cid)} 부서/과제")

            if not 바뀐것:
                return self._redirect("/recruit?msg=" + urllib.parse.quote("바뀐 내용이 없습니다."))
            보임 = ", ".join(바뀐것[:5]) + (" 외" if len(바뀐것) > 5 else "")
            return self._redirect("/recruit?msg=" + urllib.parse.quote(
                f"{len(바뀐것)}건 저장했습니다 — {보임}"))

        if path == "/mail/template/add":
            if not can(me, "메일_템플릿"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            이름 = (data.get("name") or [""])[0]
            탈락 = bool(data.get("reject"))
            try:
                tid = mailing.add_template(이름, 탈락메일=탈락, 만든이=me.아이디,
                                           본문형식="HTML")
            except ValueError as exc:
                return self._redirect("/mail?err=" + urllib.parse.quote(str(exc)))
            audit.record(me.아이디, "메일", 이름,
                         비고="템플릿 추가" + (" (탈락 메일)" if 탈락 else ""))
            return self._redirect(f"/mail/template?id={tid}")

        if path == "/mail/template/save":
            if not can(me, "메일_템플릿"):
                return self._deny()
            data = urllib.parse.parse_qs(
                self._read_body().decode("utf-8", "replace"), keep_blank_values=True
            )
            try:
                tid = int((data.get("id") or ["0"])[0])
            except ValueError:
                return self._redirect("/mail")
            옛 = mailing.template(tid)
            if 옛 is None:
                return self._redirect("/mail")
            try:
                새것 = mailing.update_template(
                    tid,
                    이름=(data.get("name") or [None])[0],
                    제목=(data.get("subject") or [""])[0],
                    본문=(data.get("body") or [""])[0],
                    탈락메일=bool(data.get("reject")),
                    참조=(data.get("cc") or [""])[0],
                )
            except ValueError as exc:
                return self._redirect(f"/mail/template?id={tid}&err="
                                      + urllib.parse.quote(str(exc)))
            변경 = [
                (항목, 옛값, 새값)
                for 항목, 옛값, 새값 in (
                    ("이름", 옛.이름, 새것.이름),
                    ("참조", 옛.참조, 새것.참조),
                    ("제목", 옛.제목, 새것.제목),
                    ("본문", 옛.본문, 새것.본문),
                    ("탈락메일", "Y" if 옛.탈락메일 else "", "Y" if 새것.탈락메일 else ""),
                )
                if 옛값 != 새값
            ]
            for 항목, 옛값, 새값 in 변경:
                # 본문 전체를 이력에 남기면 읽기 어려워서 바뀐 사실만 남긴다
                if 항목 == "본문":
                    audit.record(me.아이디, "메일", 새것.이름, 항목="본문", 비고="본문 수정")
                else:
                    audit.record(me.아이디, "메일", 새것.이름, 항목=항목,
                                 이전값=옛값, 새값=새값)
            메시지 = "저장했습니다." if 변경 else "바뀐 내용이 없습니다."
            return self._redirect(f"/mail/template?id={tid}&msg="
                                  + urllib.parse.quote(메시지))

        if path == "/mail/template/delete":
            if not can(me, "메일_템플릿"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            try:
                이름 = mailing.delete_template(int((data.get("id") or ["0"])[0]))
            except ValueError:
                이름 = ""
            if 이름:
                audit.record(me.아이디, "메일", 이름, 비고="템플릿 삭제 (발송 기록은 유지)")
            return self._redirect("/mail?msg=" + urllib.parse.quote(
                f"'{이름}' 템플릿을 지웠습니다." if 이름 else "지울 템플릿이 없습니다."))

        if path == "/mail/test":
            if not can(me, "메일_발송"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            try:
                tid = int((data.get("id") or ["0"])[0])
            except (ValueError, TypeError):
                return self._redirect("/mail")
            tpl = mailing.template(tid)
            if tpl is None:
                return self._redirect("/mail")
            주소 = (data.get("to") or [""])[0].strip()
            뒤로 = f"/mail/send?id={tid}"
            if not 주소:
                return self._redirect(뒤로 + "&err=" + urllib.parse.quote(
                    "시험 발송할 주소를 넣으세요."))

            # 실제 지원자 한 명의 값으로 채운다 (없으면 보기용 값)
            진행맵 = recruit.all()
            records = store.list_all()
            값 = _mail_vars(records[0], 진행맵) if records else {}
            값 = {k: (v or f"(예시){k}") for k, v in 값.items()}
            for 변수 in _mail_var_names():
                값.setdefault(변수, f"(예시){변수}")
            제목, _ = render(tpl.제목, 값)
            본문, _ = render(tpl.본문, 값)
            try:
                결과 = mailapi.send(주소, f"[시험] {제목}", 본문, html=tpl.html,
                                  참조=tpl.cc(),
                                  첨부=mailing.attachment_bytes(tpl.id))
            except mailapi.MailError as exc:
                audit.record(me.아이디, "메일", tpl.이름,
                             비고=f"시험 발송 실패 ({주소})")
                return self._redirect(뒤로 + "&err=" + urllib.parse.quote(str(exc)))
            # 시험 발송은 **지원자 발송 기록에 남기지 않는다.**
            # 남기면 그 지원자에게 진짜로 못 보내게 된다.
            audit.record(me.아이디, "메일", tpl.이름,
                         비고=f"시험 발송 {'성공' if 결과.보냄 else '(연습 모드)'} → {주소}")
            if 결과.보냄:
                알림 = (f"{주소} 로 보냈습니다. API 응답 HTTP {결과.상태코드}: "
                      f"{결과.응답[:200] or '(본문 없음)'}")
            else:
                알림 = ("연습 모드(MAIL_DRY_RUN=1)라 보내지 않았습니다. "
                      "아래 '보낼 요청 내용' 에서 형식을 확인하세요.")
            return self._redirect(뒤로 + "&msg=" + urllib.parse.quote(알림)
                                  + "&peek=1")

        if path == "/mail/attachment/add":
            if not can(me, "메일_템플릿"):
                return self._deny()
            form = parse_multipart(self._read_body(), self.headers.get("Content-Type", ""))
            try:
                tid = int(form.fields.get("template", "0"))
            except (ValueError, TypeError):
                return self._redirect("/mail")
            뒤로 = f"/mail/template?id={tid}"
            if mailing.template(tid) is None:
                return self._redirect("/mail")
            붙임, 실패 = [], ""
            for f in form.files:
                if not f.filename:
                    continue
                try:
                    mailing.add_attachment(tid, f.filename, f.content, 올린이=me.아이디)
                    붙임.append(f.filename)
                except ValueError as exc:
                    실패 = 실패 or str(exc)
            for 이름 in 붙임:
                audit.record(me.아이디, "메일", str(tid), 항목="첨부 추가", 새값=이름)
            if 실패:
                return self._redirect(f"{뒤로}&err=" + urllib.parse.quote(실패))
            if not 붙임:
                return self._redirect(f"{뒤로}&err="
                                      + urllib.parse.quote("붙일 파일을 고르세요."))
            return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote(
                f"{len(붙임)}개 붙였습니다: {', '.join(붙임)}"))

        if path == "/mail/attachment/delete":
            if not can(me, "메일_템플릿"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            try:
                tid = int((data.get("template") or ["0"])[0])
                이름 = mailing.delete_attachment(int((data.get("id") or ["0"])[0]))
            except (ValueError, TypeError):
                return self._redirect("/mail")
            if 이름:
                audit.record(me.아이디, "메일", str(tid), 항목="첨부 삭제", 이전값=이름)
            return self._redirect(f"/mail/template?id={tid}")

        if path == "/mail/send":
            if not can(me, "메일_발송"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            try:
                tid = int((data.get("id") or ["0"])[0])
            except ValueError:
                return self._redirect("/mail")
            tpl = mailing.template(tid)
            if tpl is None:
                return self._redirect("/mail")
            뒤로 = f"/mail/send?id={tid}"
            ids = data.get("ids") or []
            if not ids:
                return self._redirect(뒤로 + "&err=" + urllib.parse.quote(
                    "보낼 지원자를 하나 이상 고르세요."))

            진행맵 = recruit.all()
            보이는 = auth.visible_project_ids(me)
            참조 = tpl.cc()
            첨부파일 = mailing.attachment_bytes(tpl.id)
            첨부이름 = ", ".join(이름 for 이름, _ in 첨부파일)
            성공, 실패, 건너뜀 = 0, 0, 0
            첫오류 = ""
            for cid in ids:
                rec = store.get(cid)
                if rec is None:
                    건너뜀 += 1
                    continue
                if 보이는 is not None and recruit.get(cid).project_id not in 보이는:
                    건너뜀 += 1
                    continue
                # 화면을 그린 뒤에 상황이 바뀌었을 수 있다. 보내기 직전에 다시 본다.
                막힘 = mailing.blocked_reason(cid, tpl)
                if 막힘:
                    건너뜀 += 1
                    continue
                값 = _mail_vars(rec, 진행맵)
                받는사람 = (값.get("이메일") or "").split(MULTI_SEP)[0].strip()
                제목, 빈1 = render(tpl.제목, 값)
                본문, 빈2 = render(tpl.본문, 값)
                if not 받는사람 or 빈1 or 빈2:
                    건너뜀 += 1
                    continue
                try:
                    결과 = mailapi.send(받는사람, 제목, 본문, html=tpl.html,
                                      참조=참조, 첨부=첨부파일)
                except mailapi.MailError as exc:
                    실패 += 1
                    첫오류 = 첫오류 or str(exc)
                    mailing.record(cid, tpl, 받는사람, 제목, 본문, "실패",
                                   오류=str(exc), 보낸이=me.아이디,
                                   참조=", ".join(참조), 첨부=첨부이름)
                    continue
                상태 = "성공" if 결과.보냄 else "발송안함"
                성공 += 1
                # API 응답을 그대로 남긴다. HTTP 200 이어도 본문에 실패가 적혀 오는
                # API 가 있어서, 사람이 눈으로 확인할 수 있어야 한다.
                메모 = (f"HTTP {결과.상태코드} {결과.응답[:300]}".strip()
                      if 결과.보냄 else 결과.응답)
                mailing.record(cid, tpl, 받는사람, 제목, 본문, 상태,
                               오류=메모, 보낸이=me.아이디,
                               참조=", ".join(참조), 첨부=첨부이름)
                audit.record(me.아이디, "메일", cid, 항목=tpl.이름,
                             새값=상태, 비고=f"{받는사람} 로 발송")

            조각 = [f"{성공}명에게 보냈습니다"]
            if 실패:
                조각.append(f"{실패}명 실패 ({첫오류[:80]})")
            if 건너뜀:
                조각.append(f"{건너뜀}명은 보낼 수 없어 건너뛰었습니다")
            return self._redirect(뒤로 + "&msg=" + urllib.parse.quote(" / ".join(조각)))

        if path == "/fields/add":
            if not can(me, "열_구성"):
                return self._deny("표 항목 추가는 관리자만 할 수 있습니다.")
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            이름 = (data.get("name") or [""])[0]
            try:
                store.add_field(
                    이름,
                    (data.get("type") or ["텍스트"])[0],
                    (data.get("choices") or [""])[0],
                    만든이=me.아이디,
                )
            except ValueError as exc:
                return self._redirect("/fields?err=" + urllib.parse.quote(str(exc)))
            audit.record(me.아이디, "표항목", 이름, 비고="열 추가")
            return self._redirect("/fields")

        if path == "/fields/columns":
            if not can(me, "열_구성"):
                return self._deny("표 열 설정은 관리자만 바꿀 수 있습니다.")
            data = urllib.parse.parse_qs(
                self._read_body().decode("utf-8", "replace"), keep_blank_values=True
            )
            열들 = data.get("col") or []
            이전 = store.column_config()
            바뀐것: list[str] = []
            for i, col in enumerate(열들, start=1):
                새라벨 = (data.get(f"label_{i}") or [""])[0].strip()
                순서값 = (data.get(f"order_{i}") or [""])[0].strip()
                숨김 = f"hide_{i}" in data
                옛 = 이전.get(col, {"표시이름": "", "숨김": False, "순서": 0})
                try:
                    새순서 = int(순서값) if 순서값 else 0
                except ValueError:
                    새순서 = 옛["순서"]
                if (새라벨, 숨김, 새순서) == (옛["표시이름"], 옛["숨김"], 옛["순서"]):
                    continue
                store.set_column(col, 표시이름=새라벨, 숨김=숨김, 순서=새순서)
                조각 = []
                if 새라벨 != 옛["표시이름"]:
                    조각.append(f"이름 {새라벨 or '(원래대로)'}")
                    audit.record(me.아이디, "표항목", col, 항목="표에 보일 이름",
                                 이전값=옛["표시이름"], 새값=새라벨)
                if 숨김 != 옛["숨김"]:
                    조각.append("숨김" if 숨김 else "다시 보임")
                    audit.record(me.아이디, "표항목", col, 항목="숨김",
                                 이전값="Y" if 옛["숨김"] else "", 새값="Y" if 숨김 else "")
                if 새순서 != 옛["순서"]:
                    조각.append(f"순서 {새순서 or '원래대로'}")
                    audit.record(me.아이디, "표항목", col, 항목="순서",
                                 이전값=str(옛["순서"] or ""), 새값=str(새순서 or ""))
                바뀐것.append(f"{col}({', '.join(조각)})")
            if not 바뀐것:
                return self._redirect("/fields?msg=" + urllib.parse.quote("바뀐 내용이 없습니다."))
            보임 = ", ".join(바뀐것[:5]) + (" 외" if len(바뀐것) > 5 else "")
            return self._redirect("/fields?msg=" + urllib.parse.quote(
                f"{len(바뀐것)}건 저장했습니다 — {보임}"))

        if path == "/fields/delete":
            if not can(me, "열_구성"):
                return self._deny("표 항목 삭제는 관리자만 할 수 있습니다.")
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            이름 = (data.get("name") or [""])[0]
            store.delete_field(이름)
            audit.record(me.아이디, "표항목", 이름, 비고="열 삭제 (값도 함께 삭제)")
            return self._redirect("/fields")

        if path == "/candidate/custom":
            if not can(me, "지원자_수정"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            cid = (data.get("id") or [""])[0]
            필드명 = (data.get("항목") or [""])[0]
            뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
            field = store.field(필드명)
            if not field:
                return self._redirect(뒤로)
            try:
                저장값 = validate_custom(field, (data.get("새값") or [""])[0])
            except ValidationError as exc:
                return self._redirect(f"{뒤로}&err={urllib.parse.quote(str(exc))}")
            이전 = store.set_custom(cid, 필드명, 저장값)
            if 이전 != 저장값:
                audit.record(me.아이디, "지원자", cid, 항목=필드명, 이전값=이전, 새값=저장값)
            return self._redirect(뒤로)

        if path == "/org/dept/rename":
            if not can(me, "부서과제_관리"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            새이름 = (data.get("name") or [""])[0]
            try:
                옛이름 = auth.rename_department(int((data.get("id") or ["0"])[0]), 새이름)
            except (ValueError, TypeError) as exc:
                return self._redirect("/org?err=" + urllib.parse.quote(str(exc)))
            if 옛이름 != 새이름:
                audit.record(me.아이디, "과제", 새이름, 항목="부서명",
                             이전값=옛이름, 새값=새이름)
            return self._redirect("/org")

        if path == "/org/dept/delete":
            if not can(me, "부서과제_관리"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            try:
                auth.delete_department(int((data.get("id") or ["0"])[0]))
            except (ValueError, TypeError):
                pass
            return self._redirect("/org")

        if path == "/org/project/rename":
            if not can(me, "부서과제_관리"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            새이름 = (data.get("name") or [""])[0]
            암호 = (data.get("invite") or [""])[0]
            try:
                pid = int((data.get("id") or ["0"])[0])
                옛이름 = auth.rename_project(pid, 새이름)
            except (ValueError, TypeError) as exc:
                return self._redirect("/org?err=" + urllib.parse.quote(str(exc)))
            if 암호.strip():          # 비우면 기존 암호를 그대로 둔다
                auth.set_project_password(pid, 암호)
                audit.record(me.아이디, "과제", 새이름, 비고="초대암호 변경")
            if 옛이름 != 새이름:
                audit.record(me.아이디, "과제", 새이름, 항목="과제명",
                             이전값=옛이름, 새값=새이름)
            return self._redirect("/org")

        if path == "/fields":
            if not can(me, "열_구성"):
                return self._deny("표 항목 추가는 관리자만 할 수 있습니다.")
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(_fields_page(me, (params.get("err") or [""])[0]))
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

        if path == "/api/cell":
            # 표에서 칸 하나만 고친다. 상세 화면의 /candidate/edit 과 같은
            # 검사·같은 낙관적 잠금·같은 이력을 탄다. 다른 점은 응답이 JSON 이라
            # 페이지를 새로 그리지 않는다는 것뿐이다.
            if not can(me, "지원자_수정"):
                return self._json({"ok": False, "error": "수정 권한이 없습니다."}, code=403)
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            cid = (data.get("id") or [""])[0]
            항목 = (data.get("항목") or [""])[0]
            새값 = (data.get("새값") or [""])[0]
            이전값 = (data.get("이전값") or [""])[0]
            scope = (data.get("scope") or ["기본"])[0]

            if scope == "사용자":
                field = store.field(항목)
                if not field:
                    return self._json({"ok": False, "error": f"없는 열입니다: {항목}"}, code=404)
                현재 = store.custom_values(cid).get(항목, "")
                if 현재 != 이전값:
                    return self._json({"ok": False, "error": str(
                        ConflictError(항목, 현재, 이전값))}, code=409)
                try:
                    저장값 = validate_custom(field, 새값)
                except ValidationError as exc:
                    return self._json({"ok": False, "error": str(exc)}, code=400)
                이전 = store.set_custom(cid, 항목, 저장값)
                if 이전 != 저장값:
                    audit.record(me.아이디, "지원자", cid, 항목=항목,
                                 이전값=이전, 새값=저장값, 비고="표에서 수정")
                return self._json({"ok": True, "raw": 저장값, "표시": 저장값})

            rec = store.get(cid)
            if rec is None:
                return self._json({"ok": False, "error": "지원자를 찾을 수 없습니다."}, code=404)
            try:
                옛값, 저장값 = apply_edit(rec, 항목, 새값, 기대_이전값=이전값)
            except ConflictError as exc:
                return self._json({"ok": False, "error": str(exc)}, code=409)
            except ValidationError as exc:
                return self._json({"ok": False, "error": str(exc)}, code=400)
            if 옛값 != 저장값:
                store.save(rec)
                audit.record(me.아이디, "지원자", cid, 항목=항목,
                             이전값=옛값, 새값=저장값, 비고="표에서 수정")
            표시 = str(rec.to_row(registry).get(항목, "") or "")
            return self._json({"ok": True, "raw": 저장값, "표시": 표시})

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
                옛값, 저장값 = apply_edit(rec, 항목, 새값, 기대_이전값=이전값,
                                        registry=registry)
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
                store.delete_many(ids)       # 원본·첨부파일까지 함께 지운다
                for cid in ids:
                    recruit.delete(cid)      # 채용 현황에 유령 줄이 남지 않게
                    audit.record(me.아이디, "지원자", cid, 비고="지원자 삭제")
            return self._redirect("/")

        if path == "/candidates/purge":
            if not can(me, "지원자_삭제"):
                return self._deny()
            지운것 = store.purge_expired()
            for cid in 지운것:
                recruit.delete(cid)
                audit.record(me.아이디, "지원자", cid, 비고="보관기간 만료 삭제")
            return self._redirect("/")

        if path == "/candidate/reanalyze":
            if not can(me, "지원자_등록"):
                return self._deny()
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
            return self._redirect("/upload")

        if path == "/names/save":
            if not can(me, "명칭_관리"):
                return self._deny()
            # 빈칸도 받아야 IF 를 지울 수 있다
            data = urllib.parse.parse_qs(
                self._read_body().decode("utf-8", "replace"), keep_blank_values=True
            )
            kind = canonical_kind((data.get("kind") or ["학회·저널"])[0])
            뒤로 = f"/names?kind={urllib.parse.quote(kind)}"

            # 화면에 있던 줄 전부가 들어온다. 실제로 값이 달라진 것만 저장한다.
            바뀐것: list[str] = []
            for 원시 in data.get("id") or []:
                try:
                    nid = int(원시)
                except (ValueError, TypeError):
                    continue
                이전 = registry.get(nid)
                if 이전 is None:
                    continue
                registry.classify(
                    nid,
                    표시명=(data.get(f"표시명_{nid}") or [None])[0],
                    등급=(data.get(f"등급_{nid}") or [None])[0],
                    국내해외=(data.get(f"국내해외_{nid}") or [None])[0],
                    유형=(data.get(f"유형_{nid}") or [None])[0],
                    IF=(data.get(f"IF_{nid}") or [""])[0] if f"IF_{nid}" in data else None,
                )
                이후 = registry.get(nid)
                if 이후 is None:
                    continue
                변경 = [
                    (항목, 옛, 새)
                    for 항목, 옛, 새 in (
                        ("표에 보일 이름", 이전.표시명, 이후.표시명),
                        ("학회/저널", 이전.유형, 이후.유형),
                        ("등급", 이전.등급, 이후.등급),
                        ("국내해외", 이전.국내해외, 이후.국내해외),
                        ("IF", 이전.IF, 이후.IF),
                    )
                    if 옛 != 새
                ]
                for 항목, 옛, 새 in 변경:
                    audit.record(me.아이디, "명칭", f"{kind}:{이후.원표기}",
                                 항목=항목, 이전값=옛, 새값=새)
                if 변경:
                    이름변경 = [v for v in 변경 if v[0] == "표에 보일 이름"]
                    머리 = (f"{이전.표시명} → {이후.표시명}" if 이름변경 else 이후.표시명)
                    나머지 = [f"{항목} {새}" for 항목, _, 새 in 변경 if 항목 != "표에 보일 이름"]
                    바뀐것.append(f"{이후.원표기}: " + 머리
                                + (f" ({', '.join(나머지)})" if 나머지 else ""))

            if not 바뀐것:
                return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote("바뀐 내용이 없습니다."))
            보임 = ", ".join(바뀐것[:5]) + (" 외" if len(바뀐것) > 5 else "")
            return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote(
                f"{len(바뀐것)}건 저장했습니다 — {보임}"))

        if path == "/names/forget":
            if not can(me, "명칭_관리"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            kind = canonical_kind((data.get("kind") or ["학회·저널"])[0])
            뒤로 = f"/names?kind={urllib.parse.quote(kind)}"
            try:
                지운표기 = registry.forget(int((data.get("id") or ["0"])[0]))
            except (ValueError, TypeError):
                지운표기 = ""
            if not 지운표기:
                return self._redirect(뒤로)
            audit.record(me.아이디, "명칭", f"{kind}:{지운표기}", 비고="표기 삭제")
            return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote(
                f"'{지운표기}' 표기를 사전에서 지웠습니다."))

        if path == "/names/tiers":
            if not can(me, "열_구성"):
                return self._deny("표 열 구성은 관리자만 바꿀 수 있습니다.")
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            kind = canonical_kind((data.get("kind") or ["학회"])[0])
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
