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

import contextvars
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
from .. import review
from ..dashboards import (
    AXIS_SOURCES,
    BLOCK_KINDS,
    ROW_TARGET,
    WIDTHS,
    CELL_FORMATS,
    DashboardStore,
    format_cell,
    render_list,
    render_profile,
    render_table,
)
from .. import dash_draft
from .. import expr
from .. import formula as F
from .. import profile_form as P
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
from ..store import CUSTOM_SCOPES, CUSTOM_TYPES, SUPPORTED_SUFFIXES, CandidateStore
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
from ..mailing import (
    IMAGE_MODES,
    MailStore,
    Template,
    html_to_text,
    render,
)
from ..matching import SCORE_RUBRIC, candidate_profile, match as match_projects
from .. import projects as projectsmod
from ..clients import mailer as mailapi
from ..recruit import (
    FIXED_STATUSES,
    RECRUIT_COLUMNS,
    STARTED_COLUMN,
    STAGES,
    STATUSES,
    RecruitStore,
)
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
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
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
boards = DashboardStore(DATA_DIR / "dashboard.db")


_projects_cache: dict = {"mtime": None, "path": None, "목록": [], "오류": ""}


def 다듬은파일() -> Path:
    """필요한 과제·필드만 남겨 저장하는 파일의 위치."""
    if settings.projects_curated:
        return Path(projectsmod.resolve_path(settings.projects_curated))
    return DATA_DIR / "과제_선별.json"


def 쓰는과제파일() -> tuple[str, bool]:
    """(실제로 매칭에 쓰는 경로, 다듬은 파일인지).

    다듬은 파일이 있으면 그것을 쓴다. 원본에는 매칭에 쓸모없는 항목이 많다.
    """
    다듬 = 다듬은파일()
    if 다듬.is_file():
        return str(다듬), True
    return settings.projects_json, False


def 과제목록(다시: bool = False) -> tuple[list, str]:
    """(과제 목록, 오류 메시지). 파일이 바뀌면 자동으로 다시 읽는다."""
    경로, _다듬음 = 쓰는과제파일()
    if not 경로:
        return [], ""
    풀린것 = projectsmod.resolve_path(경로)
    mtime = 풀린것.stat().st_mtime if 풀린것 and 풀린것.is_file() else None
    if (다시 or _projects_cache["path"] != str(풀린것)
            or _projects_cache["mtime"] != mtime):
        try:
            _projects_cache["목록"] = projectsmod.load(경로)
            _projects_cache["오류"] = ""
        except projectsmod.ProjectsError as exc:
            _projects_cache["목록"] = []
            _projects_cache["오류"] = str(exc)
        _projects_cache["path"] = str(풀린것)
        _projects_cache["mtime"] = mtime
    return _projects_cache["목록"], _projects_cache["오류"]


def 매칭실행(rec, *, 사용자: str = "") -> tuple[int, str]:
    """지원자 한 명을 과제와 맞춰 보고 저장한다. (매칭 수, 오류 메시지)"""
    목록, 오류 = 과제목록()
    if 오류:
        return 0, 오류
    if not 목록:
        return 0, ""
    profile = candidate_profile(rec, registry)
    try:
        결과 = match_projects(profile, 목록, batch=settings.match_batch,
                            embed_client=_embed_client())
    except Exception as exc:  # noqa: BLE001 - 매칭이 실패해도 지원자는 남아야 한다
        return 0, f"{type(exc).__name__}: {exc}"
    store.save_matches(rec.지원자_ID, 결과)
    if 사용자 and 결과:
        audit.record(사용자, "지원자", rec.지원자_ID, 항목="과제 매칭",
                     새값=f"{결과[0].과제명} {결과[0].점수}점")
    return len(결과), ""


def _embed_client():
    """임베딩은 있으면 쓰고 없으면 만다 (후보 좁히기에만 쓴다)."""
    try:
        from ..clients.embedding import EmbeddingClient

        return EmbeddingClient()
    except Exception:  # noqa: BLE001
        return None


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
def _set_status(name: str, state: str, message: str = "", cid: str = "") -> None:
    """처리 현황 한 줄. cid 를 실어 둬야 거기서 바로 상세로 갈 수 있다."""
    with _status_lock:
        옛 = _status.get(name) or {}
        _status[name] = {
            "state": state, "message": message,
            "cid": cid or 옛.get("cid", ""),
            "시각": now_kst().strftime("%H:%M:%S"),
        }


def _worker() -> None:
    while True:
        filename, 지원자_ID, 저장_파일명 = _jobs.get()
        try:
            _set_status(filename, "처리중", cid=지원자_ID)
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

            # 과제 매칭. 실패해도 지원자 등록은 이미 끝났으니 메모만 남긴다.
            매칭메모 = ""
            if settings.match_auto and settings.projects_json:
                개수, 매칭오류 = 매칭실행(rec)
                if 매칭오류:
                    매칭메모 = f" / 과제 매칭 실패: {매칭오류}"
                elif 개수:
                    최고 = store.matches(rec.지원자_ID)[0]
                    매칭메모 = f" / 과제: {최고['과제명']} {최고['점수']}점"

            state = "중복의심" if 후보 else ("검토필요" if rec.검토_필요 == "Y" else "완료")
            _set_status(filename, state, (rec.검토_사유 or "") + 매칭메모,
                        cid=지원자_ID)
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
/* 색·간격·모서리를 토큰으로 모은다. 예전에는 값이 파일 곳곳에 흩어져 있어서
   한 군데만 고치면 나머지가 어긋났다. */
:root{
 --bg:#f7f8fa;--card:#fff;--line:#e6e8ec;--line2:#f0f1f4;--grid:#222;
 --txt:#16191d;--txt2:#42474e;--muted:#6b7280;
 --accent:#2f6fed;--accent-w:#eaf1fe;--accent-d:#1d4fc4;
 --r:10px;--r-s:7px;
 --sh:0 1px 2px rgba(16,24,40,.04),0 1px 3px rgba(16,24,40,.06);
 --sh-l:0 4px 6px -2px rgba(16,24,40,.04),0 12px 16px -4px rgba(16,24,40,.08);
}
*{box-sizing:border-box}
/* 맑은 고딕은 화면에서 낡아 보인다. 요즘 OS 에 깔린 글꼴을 먼저 쓰고,
   없으면 순서대로 내려간다 (폐쇄망이라 웹폰트는 못 받는다). */
body{margin:0;background:var(--bg);color:var(--txt);
 font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI Variable Text","Segoe UI",
 Roboto,"Pretendard","Apple SD Gothic Neo","Noto Sans KR","맑은 고딕",sans-serif;
 -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
header{background:var(--card);color:var(--txt);padding:0 20px;display:flex;gap:2px;
 align-items:stretch;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:50}
header a{color:var(--muted);text-decoration:none;font-weight:550;font-size:13.5px;
 display:flex;align-items:center;padding:13px 12px;border-bottom:2px solid transparent;
 white-space:nowrap}
header .brand{color:var(--txt);font-weight:750;font-size:15px;letter-spacing:-.01em;
 margin-right:10px;padding-right:16px;border-right:1px solid var(--line);
 border-bottom:0;align-self:center;padding-top:0;padding-bottom:0}
header a:hover{color:var(--txt)}
/* 지금 보고 있는 탭. 색만으로 알려주지 않고 굵기와 아래 밑줄이 함께 바뀐다. */
header a.on{color:var(--accent);font-weight:700;border-bottom-color:var(--accent)}
header .sp{flex:1}
/* 오른쪽 끝의 '누구로 들어와 있나'. 두 글자가 아래위로 어긋나 보이지 않게
   같은 줄에 세우고, 역할은 작은 딱지로 붙인다. */
header .who{display:flex;align-items:center;gap:6px;color:var(--muted);
 font-size:12.5px;padding:0 4px}
header .who b{font-weight:650;font-size:11px;color:var(--txt2);background:var(--bg);
 border:1px solid var(--line);border-radius:99px;padding:1px 8px}
header a[href='/logout']{font-weight:500}
/* 링크. 브라우저 기본 파랑 밑줄은 화면을 낡아 보이게 한다. */
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.card h2 a{color:inherit}
main{padding:22px 20px 40px;max-width:var(--mainw,1600px);margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
 padding:18px 20px;margin-bottom:16px;box-shadow:var(--sh)}
h2{margin:0 0 12px;font-size:15px;font-weight:700;letter-spacing:-.01em}
button,.btn{background:var(--accent);color:#fff;border:1px solid var(--accent);
 border-radius:var(--r-s);padding:7px 13px;font:inherit;font-size:13.5px;font-weight:550;
 cursor:pointer;text-decoration:none;display:inline-block;line-height:1.4;
 transition:background .12s,border-color .12s,box-shadow .12s}
button:hover,.btn:hover{background:var(--accent-d);border-color:var(--accent-d)}
button:active,.btn:active{transform:translateY(.5px)}
/* 키보드로 옮겨 다닐 때 지금 어디인지 보여야 한다 (마우스 클릭에는 안 뜬다) */
button:focus-visible,.btn:focus-visible,input:focus-visible,select:focus-visible,
textarea:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button:disabled{opacity:.5;cursor:not-allowed}
.btn.sec,button.sec{background:var(--card);color:var(--txt2);border-color:var(--line)}
.btn.sec:hover,button.sec:hover{background:var(--bg);border-color:#d0d4da;color:var(--txt)}
button.danger,.btn.danger{background:#dc2626;border-color:#dc2626}
button.danger:hover,.btn.danger:hover{background:#b91c1c;border-color:#b91c1c}
/* 지우기처럼 **되돌릴 수 없는** 일은 눈에 띄되 손이 먼저 가면 안 된다.
   평소엔 조용히 있다가 손이 닿으면 빨갛게 찬다. */
button.ghost,.btn.ghost{background:var(--card);color:#c02626;border-color:#f3c9c9}
button.ghost:hover,.btn.ghost:hover{background:#dc2626;color:#fff;border-color:#dc2626}
/* 단추가 여럿 늘어서는 줄 */
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 14px}
.bar .muted{margin-left:2px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
/* 칸마다 테두리를 두른다. 가로줄만 있으면 열이 여럿일 때 **어디까지가 한 칸인지**
   눈으로 자를 수가 없다 — 엑셀에서 표를 볼 때 격자를 켜는 이유와 같다.
   선은 진한 실선이다. 연한 선은 칸을 갈라 주지 못한다. */
th,td{border:1px solid var(--grid);padding:7px 9px;text-align:left;
 white-space:nowrap;max-width:260px;overflow:hidden;text-overflow:ellipsis}
/* 머리글은 줄바꿈을 허용한다. 안 그러면 '저널_주저자_수' 같은 긴 이름 하나가
   값은 한 글자뿐인 열을 통째로 넓혀 버린다. keep-all 은 한국어 낱말을 안 쪼갠다. */
th{background:var(--bg);position:sticky;top:0;white-space:normal;word-break:keep-all;
 line-height:1.3;vertical-align:bottom;font-size:11.5px;font-weight:650;color:var(--txt2);
 padding:8px 9px;border-bottom:1px solid var(--grid);z-index:1}
/* 열 성격에 맞춘 너비. 다 같게 하면 어떤 건 남고 어떤 건 모자란다. */
.w-xs{max-width:76px;min-width:52px}
.w-sm{max-width:96px;min-width:64px}
.w-md{max-width:150px;min-width:88px}
.w-lg{max-width:230px;min-width:130px}
.w-xl{max-width:380px;min-width:200px}
/* 표 안에서는 **줄을 바꾸지 않는다.** 한 줄이 길어지면 그 줄만 키가 커져서
   표가 들쭉날쭉해지고 눈이 줄을 못 따라간다. 넘치는 글은 … 으로 자르고,
   마우스를 올리면 전체가 뜬다(title). **내용은 그대로 있다** — 자르는 건
   보이는 것뿐이고, 복사·엑셀·검색은 원래 글을 쓴다. */
.scroll table td{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.scroll table td.ctl{overflow:visible}
/* 얼룩말 무늬 대신 **지금 보고 있는 줄**만 밝힌다. 눈이 가로로 따라가기 쉽고
   화면도 조용해진다. (자를 대고 읽던 걸 마우스가 대신한다) */
.scroll table tr:hover td{background:var(--accent-w)}
.scroll{overflow:auto;max-height:70vh;border:1px solid var(--line);border-radius:var(--r-s);
 background:var(--card)}
.flag{color:#c02626;font-weight:650}
.ok{color:#15803d}
.muted{color:var(--muted);font-size:12.5px}
/* 작업 결과 알림 — 화면 맨 위 띠가 아니라 오른쪽 위에 잠깐 떴다 사라진다.
   띠로 붙이면 아래 내용이 통째로 밀려서, 방금 고친 자리가 눈에서 사라진다.
   자리를 차지하지 않게 화면 위에 띄우고, 좁게 잡아 뒤를 가리지 않는다. */
#알림상자{position:fixed;top:58px;right:18px;z-index:900;display:flex;
 flex-direction:column;gap:8px;width:min(340px,42vw);pointer-events:none}
main .toast{display:none}          /* 제자리로 옮기기 전에는 안 보인다 */
#알림상자 .toast{display:block;pointer-events:auto;cursor:pointer;
 background:var(--card);border:1px solid var(--line);border-radius:var(--r-s);
 box-shadow:var(--sh-l);padding:11px 13px 11px 32px;font-size:13.5px;
 line-height:1.45;color:var(--txt);position:relative;word-break:break-word;
 opacity:0;transform:translateY(-6px);animation:토스트등장 .16s ease-out forwards}
#알림상자 .toast::before{content:'';position:absolute;left:12px;top:15px;
 width:9px;height:9px;border-radius:50%}
#알림상자 .toast.ok::before{background:#22c55e}
#알림상자 .toast.bad::before{background:#dc2626}
#알림상자 .toast.bad{border-color:#f3c9c9}
#알림상자 .toast.out{opacity:0;transform:translateY(-6px);
 transition:opacity .35s,transform .35s}
@keyframes 토스트등장{to{opacity:1;transform:none}}
@media (max-width:700px){#알림상자{width:auto;left:12px;right:12px;top:52px}}
.warn{background:#fffaeb;border:1px solid #fde68a;border-left:3px solid #f59e0b;
 padding:11px 14px;border-radius:var(--r-s);margin-bottom:14px;color:#7c4a03}
.done{background:#f0fdf4;border:1px solid #bbf7d0;border-left:3px solid #22c55e;
 padding:11px 14px;border-radius:var(--r-s);margin-bottom:14px;color:#14532d}
input[type=password],input[type=text],input[type=number],input[type=email],
input[type=search],select,textarea{padding:7px 10px;border:1px solid var(--line);
 border-radius:var(--r-s);font:inherit;font-size:13.5px;background:var(--card);
 color:var(--txt);transition:border-color .12s,box-shadow .12s}
input[type=text]:focus,input[type=password]:focus,input[type=number]:focus,
select:focus,textarea:focus{border-color:var(--accent);
 box-shadow:0 0 0 3px var(--accent-w);outline:none}
input::placeholder{color:#aeb4bd}
.login{max-width:360px;margin:14vh auto}
.pill{padding:2px 9px;border-radius:99px;font-size:11px;font-weight:650;
 display:inline-block;line-height:1.7}
.p-미분류{background:#fee2e2;color:#b91c1c}
.p-처리중{background:#dbeafe;color:#1d4ed8}
.p-완료{background:#dcfce7;color:#15803d}
.p-검토필요{background:#fef3c7;color:#92400e}
.p-실패{background:#fee2e2;color:#b91c1c}
.p-중복의심{background:#ffe4e6;color:#9f1239}
.p-대기중{background:#e5e7eb;color:#374151}
.p-겹침{background:#fef3c7;color:#92400e}
.dup{background:#fff1f2}
tr.grouphead td{background:#eef2ff;border-top:2px solid #c7d2fe}
/* 검토가 필요한 줄. 색만으로 알리지 않고 배지도 같이 붙는다. */
tr.needs th,tr.needs td{background:#fffbeb}
tr.needs th{border-left:3px solid #f59e0b}
tr.needs td:first-child{border-left:3px solid #f59e0b}
.p-안본것{background:#fef3c7;color:#92400e}
td.edit{cursor:cell}
td.edit:hover{outline:2px solid var(--accent);outline-offset:-2px}
td.saved{background:#dcfce7 !important}
td.err{background:#fee2e2 !important}
td.edit input,td.edit select{padding:2px 4px;font-size:12.5px;width:100%}
td.ctl,th.ctl{white-space:normal;max-width:none;overflow:visible}
/* 고르는 칸이 긴 항목 이름만큼 늘어나 표를 밀어내지 않게 한다 */
td.ctl select{max-width:180px}
td.ctl input[type=text]{max-width:220px}
.mergebar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;background:#eff6ff;
 border:1px solid #bfdbfe;border-radius:var(--r-s);padding:10px 12px;margin:0 0 12px}
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
 border-radius:var(--r);box-shadow:var(--sh-l);padding:6px;min-width:230px;
 max-width:320px;font-size:13px}
#colmenu .cm-head{font-weight:700;padding:4px 8px;color:var(--muted);
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#colmenu button{display:block;width:100%;text-align:left;background:none;color:var(--txt);
 padding:6px 8px;border-radius:var(--r-s);font-size:13px}
#colmenu button:hover{background:#eff6ff}
#colmenu .cm-btns{display:flex;gap:6px;padding:4px 0}
#colmenu .cm-btns button{background:var(--accent);color:#fff;text-align:center}
#colmenu .cm-btns button.sec{background:var(--txt2)}
#colmenu .cm-sep{border-top:1px solid var(--line);margin:5px 0}
#colmenu .cm-title{font-weight:700;padding:2px 8px}
#colmenu .cm-q{width:100%;margin:4px 0;padding:5px 7px;font-size:13px}
#colmenu .cm-list{max-height:200px;overflow:auto;border:1px solid var(--line);border-radius:var(--r-s)}
#colmenu .cm-row{display:block;padding:3px 8px;cursor:pointer;white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis}
#colmenu .cm-row:hover{background:#eff6ff}
#colmenu .cm-row.hide{display:none}
#colmenu .cm-allrow{padding-left:8px}
td.sel{background:#bfdbfe !important;outline:1px solid #2563eb;outline-offset:-1px}
/* 열 순서 끌기 */
#colorder tr[data-col]{cursor:default}
#colorder td.grip{white-space:nowrap;cursor:grab;user-select:none}
#colorder td.grip .griph{color:#b6bcc5;font-size:15px;letter-spacing:-2px}
#colorder tr.dragging{opacity:.45}
#colorder tr.dropmark td{box-shadow:inset 0 2px 0 var(--accent)}
button.tiny{padding:1px 6px;font-size:12px;min-width:22px;line-height:1.3}
#colform button.dirty{background:#b45309;border-color:#b45309}
/* 수식 미리보기 — 친 대로 바로 아래에 결과가 뜬다 */
.fxout{display:block;font-size:12px;margin-top:3px;min-height:16px;word-break:break-all;white-space:pre-line}
/* 대시보드 표 모양 — 만드는 사람이 고른다 */
table.dtbl th{background:var(--headbg,var(--bg))}
/* 격자는 **진한 검정 실선**이다. 연한 선은 칸을 갈라 주지 못한다 —
   격자를 켜는 이유가 칸 구분이니, 흐리면 켜는 의미가 없다. */
table.dtbl.b-grid th,table.dtbl.b-grid td{border:1px solid #222}
table.dtbl.b-row th,table.dtbl.b-row td{border:0;border-bottom:1px solid var(--line2)}
table.dtbl.b-row th{border-bottom:1px solid var(--line)}
table.dtbl.b-none th,table.dtbl.b-none td{border:0}
table.dtbl.b-none th{border-bottom:1px solid var(--line)}
/* 내용에 맞춤 — 칸을 억지로 줄이지 않는다. 넘치면 **가로로 스크롤**한다.
   (칸이 적으면 허전하지 않게 최소한 화면 폭은 채운다) */
table.dtbl.fit{width:auto;min-width:100%}
/* 한 칸이 통째로 화면을 잡아먹지 않게 상한만 둔다 (직접 정한 너비가 이긴다).
   상한이 있어도 열이 많으면 합이 화면을 넘어 가로 스크롤이 걸린다. */
table.dtbl.fit th,table.dtbl.fit td{max-width:420px}
table.dtbl.zebra tr:nth-child(even) td{background:#fafbfc}
/* 조건서식으로 칠한 칸은 얼룩말도 hover 도 덮지 않는다 — 일부러 칠한 것이다.
   (인라인 스타일이라 이 규칙들보다 우선하지만, 명시해 두어야 나중에 규칙을
    하나 더 얹어도 안 깨진다) */
table.dtbl.zebra tr:nth-child(even) td.painted{background:none}
.scroll table.dtbl tr:hover td.painted{background:none}
table.dtbl.zebra tr:hover td{background:var(--accent-w)}
table.dtbl.tight th,table.dtbl.tight td{padding:3px 6px;font-size:12px}
/* CHAR(10) 을 넣은 칸만 줄을 바꾼다. 나머지는 한 줄로 잘린 채 둔다 */
.scroll table td.multi{white-space:normal;overflow:visible;text-overflow:clip;line-height:1.45}
.fxout.fxok{color:#15803d}
input.fx{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
.rt{border:1px solid var(--line);border-radius:var(--r);overflow:hidden;background:#fff}
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
/* 메일 본문 표는 **지원자 표가 아니다.** 위쪽 th,td 규칙(260px 상한, 한 줄로
   자르기)은 화면의 데이터 표를 위한 것이라, 그게 편집기까지 죄면 열 너비를
   아무리 잡아도 260px 에서 멈춘다. 여기서는 푼다. */
.rt-body table td,.rt-body table th,
.mailbody table td,.mailbody table th{
 max-width:none;min-width:0;white-space:normal;overflow:visible;text-overflow:clip;
 border:0}
.rt-body table{width:auto}
.rt-body img{max-width:100%}
/* 열 경계에 마우스를 대면 끌 수 있다는 걸 알려 준다 */
.rt-body table{cursor:auto}
#rtdrop{position:absolute;z-index:120;background:#fff;border:1px solid var(--line);
 border-radius:var(--r);box-shadow:var(--sh-l);padding:5px;font-size:13px;
 max-height:340px;overflow:auto;min-width:120px}
#rtdrop > button{display:block;width:100%;text-align:left;background:none;border:0;
 color:var(--txt);padding:5px 9px;border-radius:5px;font-size:13px;cursor:pointer}
#rtdrop > button:hover{background:#eff6ff}
#rtdrop .rt-swatch{display:grid;grid-template-columns:repeat(5,22px);gap:4px;padding:4px}
#rtdrop .rt-swatch button{width:22px;height:22px;border:1px solid var(--line);
 border-radius:4px;padding:0;cursor:pointer}
#rtdrop .rt-pick{display:flex;align-items:center;gap:6px;padding:6px 6px 2px;
 color:var(--muted);border-top:1px solid var(--line);margin-top:4px;cursor:pointer}
#rtdrop .rt-grid{display:grid;grid-template-columns:repeat(10,14px);gap:3px;padding:5px}
#rtdrop .rt-grid i{width:14px;height:13px;border:1px solid var(--line);border-radius:2px;
 background:#fff;cursor:pointer}
#rtdrop .rt-grid i.on{background:#bfdbfe;border-color:var(--accent)}
#rtdrop .rt-gridlabel{text-align:center;color:var(--muted);padding:2px 0 4px}
#rtdrop .rt-gridmore{border-top:1px solid var(--line);padding:6px;color:var(--muted);
 display:flex;align-items:center;gap:4px;white-space:nowrap}
#rtdrop .rt-gridmore input{padding:3px 4px;font-size:12px}
#rtdrop .rt-gridmore button{width:auto;display:inline-block;background:var(--accent);
 color:#fff;border:0;border-radius:5px;padding:4px 9px;cursor:pointer}
/* 표 도구 — 커서가 표 안에 있을 때만 뜬다 */
.rt-tablebar{background:#eff6ff;border-bottom:1px solid #bfdbfe}
.rt-tablebar[hidden]{display:none}
.rt-bar .rt-lbl{display:inline-flex;align-items:center;gap:4px;font-size:12.5px;
 color:var(--muted);padding:0 2px}
.rt-bar .rt-lbl input[type=number],.rt-bar .rt-lbl select{padding:3px 5px;font-size:12.5px}
/* 편집기 안에서만 보이는 표 눈금. 메일에는 안 나간다 (인라인 스타일이 아니다) */
.rt-body table td:empty::after{content:'\00a0'}
#rtdrop.varmenu{width:280px}
#rtdrop .vm-head{padding:4px 8px;color:var(--muted)}
#rtdrop .vm-q{width:100%;margin:4px 0;padding:6px 8px;font-size:13px}
#rtdrop .vm-list{max-height:260px;overflow:auto}
#rtdrop .vm-group{font-weight:700;color:var(--accent);padding:8px 8px 3px;
 border-top:1px solid var(--line);margin-top:4px}
#rtdrop .vm-group:first-child{border-top:0;margin-top:0}
#rtdrop .vm-item{display:block;width:100%;text-align:left;background:none;
 color:var(--txt);padding:5px 8px;border-radius:var(--r-s);font-size:13px}
#rtdrop .vm-item:hover{background:#eff6ff}
#rtdrop .hide{display:none}
.mailbody{border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;
 background:#fff;max-height:420px;overflow:auto;font:12pt/1.7 "맑은 고딕",sans-serif}
.mailbody img{max-width:100%}
pre.rubric{background:var(--bg);border:1px solid var(--line);border-radius:var(--r-s);padding:10px 12px;font-size:12px;white-space:pre-wrap;margin:8px 0 0;color:var(--muted)}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#16191d;
 color:#fff;padding:11px 18px;border-radius:var(--r);opacity:0;pointer-events:none;
 transition:opacity .15s,transform .15s;z-index:99;box-shadow:var(--sh-l);font-size:13.5px}
#toast.show{opacity:1;transform:translateX(-50%) translateY(-2px)}
"""


#: 지금 처리 중인 요청 경로. 어느 탭에 불을 켤지 정하는 데만 쓴다.
#: _page 를 부르는 곳이 마흔 군데라 인자를 하나 더 받게 하는 대신 여기 둔다.
#: 요청마다 스레드가 따로라 값이 섞이지 않는다.
현재경로: contextvars.ContextVar[str] = contextvars.ContextVar("현재경로", default="")

#: (라벨, 주소, 이 탭에 속하는 경로들, 볼 수 있나)
#: 소속 경로를 적어 두는 이유: 지원자 상세(/candidate)는 탭이 아니지만
#: 인재 Pool 에서 들어간 화면이라 그 탭에 불이 켜져 있어야 한다.
def _탭들(me: User | None, badge: str) -> list[tuple[str, str, tuple[str, ...]]]:
    학회 = "/names?kind=" + urllib.parse.quote("학회·저널")
    후보 = [
        # 일이 흘러가는 순서대로: 넣고 → 보고 → 뽑고 → 들여다본다
        ("지원자 추가", "/upload", ("/upload",), can(me, "지원자_등록")),
        ("인재 Pool", "/", ("/", "/candidate", "/attachment", "/export.xlsx"),
         can(me, "지원자_목록")),
        ("채용 현황", "/recruit", ("/recruit",),
         can(me, "채용현황_수정") or can(me, "지원자_조회")),
        ("대시보드", "/dash", ("/dash",), can(me, "대시보드_조회")),
        ("메일", "/mail", ("/mail",), can(me, "메일_템플릿")),
        (f"명칭 관리{badge}", 학회, ("/names",), can(me, "명칭_관리")),
        # 과제 파일 관리는 이 아래 하위 화면으로 들어갔다 (/match/*)
        ("부서·과제", "/org", ("/org", "/match"), can(me, "부서과제_관리")),
        ("계정", "/users", ("/users",), can(me, "계정_현업추가")),
        ("표 항목", "/fields", ("/fields",), can(me, "열_구성")),
        ("변경 이력", "/history", ("/history",), can(me, "변경이력_조회")),
    ]
    return [(라벨, 주소, 소속) for 라벨, 주소, 소속, 보임 in 후보 if 보임]


def _지금탭(경로: str, 소속: tuple[str, ...]) -> bool:
    """이 경로가 그 탭에 속하나.

    '/' 는 정확히 같을 때만이다. 안 그러면 모든 화면이 인재 Pool 이 된다.
    """
    for base in 소속:
        if base == "/":
            if 경로 == "/":
                return True
        elif 경로 == base or 경로.startswith(base + "/"):
            return True
    return False


def _알림(msg: str = "", err: str = "") -> str:
    """작업 결과 알림.

    예전에는 화면 맨 위에 띠로 붙였다. 그러면 아래 내용이 통째로 밀려 내려가
    **방금 고친 자리가 눈에서 사라진다.** 알림은 결과를 알려주는 것뿐이라
    자리를 뺏을 이유가 없다. 오른쪽 위에 잠깐 띄우고 저절로 없앤다.

    여기 쓰는 건 **한 번 하고 끝나는 일의 결과**뿐이다(저장했습니다 · 지웠습니다).
    화면에 계속 붙어 있어야 하는 안내 — 연습 모드입니다, 검토가 필요한 CV 가
    3건 있습니다 — 는 띠 그대로 둔다. 10초 뒤에 사라지면 안 되는 글이다.
    """
    조각 = [f"<div class='toast {종류}' role='status'>{html.escape(글)}</div>"
          for 글, 종류 in ((msg, "ok"), (err, "bad")) if 글]
    return "".join(조각)


def _page(title: str, body: str, nav: bool = True, me: User | None = None,
          폭: str = "") -> bytes:
    # 탭 옆 숫자 = **아직 사람이 안 본 표기 수.** 등급을 안 매긴 것만 세면
    # 소속·전공은 늘 0 이라, 학교 이름이 엉뚱하게 들어와도 아무 표시가 없었다.
    안본것 = registry.unconfirmed_count() if nav else 0
    badge = f' <span class="pill p-안본것">{안본것}</span>' if 안본것 else ""
    경로 = 현재경로.get()
    켜진것 = ""
    for _라벨, 주소, 소속 in _탭들(me, badge):
        if _지금탭(경로, 소속):
            켜진것 = 주소
            break
    링크 = [
        f"<a href='{주소}'{' class=on' if 주소 == 켜진것 else ''}>{라벨}</a>"
        for 라벨, 주소, _소속 in _탭들(me, badge)
    ]
    누구 = (
        f"<span class='who'>{html.escape(me.이름)}"
        f"<b>{html.escape(me.역할)}</b></span>"
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
        f"<body{f' style=--mainw:{폭}' if 폭 else ''}>{header}<main>{body}</main>"
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
    """업로드 처리 현황.

    검토 필요로 끝난 줄에서는 **바로 그 지원자 상세로 갈 수 있어야 한다.**
    예전에는 여기서 '검토 필요' 라고만 알려주고, 사람은 인재 Pool 로 가서
    이름을 찾아 들어가야 했다.
    """
    with _status_lock:
        items = list(_status.items())
    if not items:
        return ""
    끝낸것 = store.review_done_map()
    rows = []
    검토수 = 0
    for n, s in items:
        cid = s.get("cid") or ""
        rec = store.get(cid) if cid else None
        남은 = review.remaining(rec.검토_사유, 끝낸것.get(cid, set())) if rec else []
        할일 = ""
        if rec is not None and 남은:
            검토수 += 1
            보임 = " · ".join(review.short(x, 40) for x in 남은[:2])
            더 = f" 외 {len(남은) - 2}건" if len(남은) > 2 else ""
            할일 = (
                f"<a class='btn' href='/candidate?id={urllib.parse.quote(cid)}#검토'>"
                f"검토 {len(남은)}건 →</a>"
                f"<div class='muted' style='white-space:normal;margin-top:3px'>"
                f"{html.escape(보임)}{더}</div>"
            )
        elif rec is not None:
            할일 = (f"<a class='btn sec' href='/candidate?id={urllib.parse.quote(cid)}'>"
                  "상세</a>")
        rows.append(
            f"<tr><td>{html.escape(n)}</td>"
            f"<td><span class='pill p-{s['state']}'>{s['state']}</span></td>"
            f"<td title='{html.escape(s.get('message',''))}'>"
            f"{html.escape(s.get('message',''))}</td>"
            f"<td>{s['시각']}</td>"
            f"<td class='ctl'>{할일}</td></tr>"
        )
    처리중 = any(s["state"] in ("대기중", "처리중") for _, s in items)
    안내 = (
        f"<p class='warn'>검토가 필요한 CV 가 <b>{검토수}건</b> 있습니다. "
        "오른쪽 <b>검토 N건 →</b> 을 누르면 그 지원자의 검토 항목으로 바로 "
        "갑니다.</p>" if 검토수 else ""
    )
    # **페이지를 통째로 새로고침하지 않는다.** 예전에는 <meta refresh> 로 5초마다
    # 다시 그렸는데, 그러면 분석이 도는 동안 파일을 고르는 순간 새로고침이
    # 끼어들어 <input type=file> 선택이 날아갔다 ("첨부가 안 된다"). 지금은
    # 이 표 안쪽만 갈아 끼우므로 고르던 파일도, 스크롤도 그대로 있다.
    상태 = ("<span class='live'>● 처리 중</span> "
          "<span class='muted'>표만 3초마다 갱신됩니다. 파일을 고르는 중이어도 "
          "선택이 풀리지 않습니다.</span>" if 처리중 else "")
    return (
        f"<div class='card'><h2>업로드 처리 현황</h2>{안내}"
        f"<p id='상태알림' data-busy='{'1' if 처리중 else ''}'>{상태}</p>"
        "<div class='scroll'><table data-name='처리 현황' id='현황표'>"
        "<tr><th>파일</th><th>상태</th><th>메모</th><th>시각</th><th>할 일</th></tr>"
        + "".join(rows) + "</table></div>"
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
/* ---- 알림 ------------------------------------------------------------------
   화면 맨 위 띠 대신 오른쪽 위에 잠깐 띄운다. 닫기를 누를 필요가 없다 —
   저절로 사라지고, 마우스를 올리고 있는 동안에는 안 사라진다(읽는 중이니까).
   잘못됐다는 알림은 더 오래 둔다. 읽고 고쳐야 하는 글이라서. */
function 알림상자(){
  var 상자 = document.getElementById('알림상자');
  if(!상자){
    상자 = document.createElement('div');
    상자.id = '알림상자';
    document.body.appendChild(상자);
  }
  return 상자;
}
function 알림닫기(t){
  if(t.__감) return;
  t.__감 = 1;
  t.classList.add('out');
  setTimeout(function(){ if(t.parentNode) t.parentNode.removeChild(t); }, 400);
}
function 알림시작(t){
  var 남은 = t.classList.contains('bad') ? 16000 : 10000;
  var 켠때 = 0, 타이머 = null;
  function 걸기(){ 켠때 = Date.now(); 타이머 = setTimeout(function(){ 알림닫기(t); }, 남은); }
  t.addEventListener('mouseenter', function(){
    clearTimeout(타이머);
    남은 -= Date.now() - 켠때;
  });
  t.addEventListener('mouseleave', function(){
    if(남은 < 1200) 남은 = 1200;      /* 스쳐 지나갔다고 바로 없어지면 안 된다 */
    걸기();
  });
  t.addEventListener('click', function(){ 알림닫기(t); });
  걸기();
}
/* 화면 안에서 바로 알릴 때 (칸 저장 실패 등). alert 은 눌러서 꺼야 해서 안 쓴다. */
function 토스트(글, 나쁨){
  var t = document.createElement('div');
  t.className = 'toast ' + (나쁨 ? 'bad' : 'ok');
  t.textContent = 글;
  알림상자().appendChild(t);
  알림시작(t);
  return t;
}
document.addEventListener('DOMContentLoaded', function(){
  var 것들 = document.querySelectorAll('main .toast');
  for(var i = 0; i < 것들.length; i++){
    알림상자().appendChild(것들[i]);
    알림시작(것들[i]);
  }
});

/* ---- 저장하면 보던 자리로 돌아온다 -----------------------------------------
   폼을 내면 페이지가 다시 그려지고 **맨 위로 튄다.** 상세 화면 아래쪽 칸을
   고치거나, 명칭 관리에서 백 줄짜리 표 중간을 고칠 때마다 다시 스크롤해서
   내려와야 했다. 고칠 게 여러 개면 그걸 매번 한다.

   낼 때 지금 위치를 적어 두고, 같은 주소로 돌아오면 그 자리로 되돌린다.
   브라우저가 스스로 복원하려 드는 것도 꺼서 두 번 움직이지 않게 한다. */
try { if('scrollRestoration' in history) history.scrollRestoration = 'manual'; } catch(e) {}
function 자리키(){ return '자리:' + location.pathname; }
document.addEventListener('submit', function(){
  try { sessionStorage.setItem(자리키(), String(window.scrollY)); } catch(e) {}
}, true);
document.addEventListener('DOMContentLoaded', function(){
  var y = null;
  try { y = sessionStorage.getItem(자리키()); } catch(e) {}
  if(y === null) return;
  try { sessionStorage.removeItem(자리키()); } catch(e) {}
  /* 주소에 #조각이 있으면 그쪽이 먼저다 — 검토 카드처럼 일부러 보낸 자리다 */
  if(location.hash) return;
  var 되돌리기 = function(){ window.scrollTo(0, +y); };
  되돌리기();
  /* 표가 늦게 그려지면 높이가 바뀐다. 한 번 더 맞춘다. */
  window.requestAnimationFrame(되돌리기);
  setTimeout(되돌리기, 60);
});

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
         td.classList.add('err'); 토스트(d.error, 1);
         setTimeout(function(){ td.classList.remove('err'); }, 4000);
       }
     })
     .catch(function(e){ td.textContent = before; 토스트('저장 실패: ' + e, 1); });
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


# ---------------------------------------------------------------------------
# 대시보드가 셀 데이터 — 화면들이 쓰는 것과 **같은 것**을 넘긴다.
# 새 질의 계층을 만들면 화면 숫자와 대시보드 숫자가 어긋난다.
# ---------------------------------------------------------------------------
def 대시보드_행() -> F.Rows:
    """지원자(인재 Pool 전체) / 채용(채용 시작한 사람) 두 묶음."""
    진행맵 = recruit.all()
    부서명 = {d["id"]: d["이름"] for d in auth.departments()}
    과제명 = {p["id"]: p["이름"] for p in auth.projects()}
    사용자값 = store.custom_map()
    관리값 = store.meta_map()
    시작한사람 = recruit.started()
    상위매칭 = store.top_matches()
    끝낸검토 = store.review_done_map()

    지원자행, 채용행 = [], []
    for rec in store.list_all():
        cid = rec.지원자_ID
        행 = rec.to_row(registry)
        행["지원자_ID"] = cid
        # 표·엑셀과 같은 규칙: 확인한 사유는 세지 않는다.
        행["검토_사유"] = review.display(rec.검토_사유, 끝낸검토.get(cid, set()))
        행.update(관리값.get(cid, {}))
        행.update(사용자값.get(cid, {}))
        p = 진행맵.get(cid)
        행["부서"] = 부서명.get(p.부서_id, "") if p else ""
        행["과제"] = 과제명.get(p.project_id, "") if p else ""
        행["최종상태"] = p.최종상태 if p else "미시작"
        행["비고"] = p.비고 if p else ""
        for 단계 in STAGES:
            행[단계] = (p.단계상태.get(단계, "") if p else "")
        m = 상위매칭.get(cid)
        행["매칭_과제"] = (m or {}).get("과제명", "") if isinstance(m, dict) else ""
        행["매칭_점수"] = str((m or {}).get("점수", "")) if isinstance(m, dict) else ""
        지원자행.append(행)
        if cid in 시작한사람:
            채용행.append(행)
    return F.Rows(지원자=지원자행, 채용=채용행)


def 대시보드_열() -> set[str]:
    """수식·문장 틀에서 쓸 수 있는 열 이름 전부."""
    이름 = set(지원자열()) | set(RECRUIT_COLUMNS) | set(store.field_names())
    이름 |= {"지원자_ID", "매칭_과제", "매칭_점수"}
    return 이름


def 대시보드_축() -> dict[str, list[str]]:
    """축으로 쓸 수 있는 값 목록. 조직·설정이 바뀌면 표가 알아서 따라 늘어난다."""
    return {
        "부서": [d["이름"] for d in auth.departments()],
        "과제": [p["이름"] for p in auth.projects()],
        "단계": list(STAGES),
        "최종상태": [s for s in recruit.statuses() if s],
        "등록년도": store.years(),
        "현재_신분": [v for v in CHOICE_FIELDS.get("현재_신분", []) if v],
    }


def _dashboard(me: User, q: str = "", review_only: bool = False, 년도: str = "",
               msg: str = "") -> bytes:
    records = store.list_filtered(q, review_only, 년도)
    전체 = store.count()
    만료 = store.expired_count()
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
    표값 = _표값맵()
    보기전용열 = set(RECRUIT_COLUMNS) | set(추가열("채용 현황")) | {MAIL_COLUMN}
    수정가능 = can(me, "지원자_수정")
    채용중 = recruit.started()
    채용가능 = can(me, "채용현황_수정")
    이름표 = 라벨(COLS)
    head = "".join(
        f"<th class='{열폭(c)}'>{머리글(이름표[c])}</th>" for c in COLS)
    body_rows = []
    for rec in records:
        row = rec.to_row(registry)
        cid = rec.지원자_ID
        cells = [
            f"<td><input type='checkbox' name='ids' value='{html.escape(cid)}'></td>",
            f"<td><a href='/candidate?id={urllib.parse.quote(cid)}'>상세</a></td>",
        ]
        for c in COLS:
            폭 = 열폭(c)
            if c == "검토_사유":
                # 검토 카드가 관리하는 열이다. 여기서 글을 고치면 '확인함'
                # 기록과 글자가 어긋나 되돌릴 수 없다. 그래서 고칠 수 없고,
                # **아직 안 본 항목만** 보인다 (전부 봤으면 '확인함').
                v = html.escape(표값.get(cid, {}).get(c, ""))
                남음 = bool(v) and v != review.DONE_MARK
                cells.append(
                    f"<td class='{'flag' if 남음 else 'muted'} {폭}' title='{v}'>"
                    + (f"<a href='/candidate?id={urllib.parse.quote(cid)}#검토'>{v}</a>"
                       if 남음 else v)
                    + "</td>")
                continue
            if c == "구글_스칼라_링크":
                # 표에서는 **눌러서 여는 링크**다. 여기서 칸을 눌러 고치게 하면
                # 링크를 열 수가 없다 — 고치는 건 상세 화면에서 한다.
                v = str(row.get(c, "") or "")
                cells.append(
                    f"<td class='{폭}' title='{html.escape(v)}'>"
                    + (f"<a href='{html.escape(v)}' target='_blank' rel='noopener'>"
                       "구글 스칼라 ↗</a>" if v.startswith("http") else html.escape(v))
                    + "</td>")
                continue
            if c == STARTED_COLUMN:
                v = 표값.get(cid, {}).get(c, "")
                모양 = "p-처리중" if v == "채용 중" else "p-대기중"
                cells.append(f"<td class='{폭}'>"
                             + (f"<span class='pill {모양}'>{html.escape(v)}</span>"
                                if v else "") + "</td>")
                continue
            if c in MANAGE_COLUMNS or c in 보기전용열:
                # 등록·보관 정보, 채용 현황, 메일 이력은 여기서 고치지 않는다.
                # 고치는 자리가 따로 있는 값이라 여기서 덮어쓰면 어긋난다.
                v = html.escape(표값.get(cid, {}).get(c, ""))
                cells.append(f"<td class='muted {폭}' title='{v}'>{v}</td>")
                continue
            if c in 사용자열정의:
                값 = 사용자값맵.get(cid, {}).get(c, "")
                if 수정가능:
                    cells.append(_cell(cid, c, 값, 값,
                                       custom_field_spec(사용자열정의[c]),
                                       scope="사용자", cls=" " + 폭))
                else:
                    cells.append(f"<td class='{폭}' title='{html.escape(값)}'>"
                                 f"{html.escape(값)}</td>")
                continue
            표시 = str(row.get(c, "") or "")
            cls = " flag" if c == "검토_필요" and 표시 == "Y" else ""
            if 수정가능 and _editable(c):
                cells.append(_cell(cid, c, 표시, str(getattr(rec, c, "") or ""),
                                   field_spec(c), cls=cls + " " + 폭))
            else:
                v = html.escape(표시)
                cells.append(f"<td class='{cls.strip()} {폭}' title='{v}'>{v}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    # 걸어 둔 검색 조건. 엑셀도, 메일 보낸 뒤 돌아올 곳도 이 조건을 그대로 쓴다.
    조건 = {k: v for k, v in (("q", q), ("year", 년도),
                             ("review", "1" if review_only else "")) if v}
    조건쿼리 = ("?" + urllib.parse.urlencode(조건)) if 조건 else ""
    내려받기안내 = (f"지금 걸린 조건({len(records)}명)만 받습니다"
                if 조건 else "전체를 받습니다")
    메일단추 = (
        "<button formaction='/mail/compose'>선택한 사람에게 메일</button> "
        if can(me, "메일_발송") else ""
    )
    # 파란 단추는 **한 줄에 하나**만 둔다. 셋이 나란히 파랗게 있으면 무엇이
    # 주된 일인지 알 수 없고, 화면이 시끄럽다.
    묶음단추 = 메일단추 + (
        "<button formaction='/candidates/start' class='sec'>채용 시작</button> "
        "<button formaction='/candidates/stop' class='sec'>채용 현황에서 내리기</button> "
        if 채용가능 else ""
    )
    if records:
        # 줄마다 있는 단추는 이 표 **밖의** 폼으로 보낸다. 폼 안에 폼을 넣으면
        # 브라우저가 안쪽을 버려서 엉뚱한 동작이 실행된다 (전에 그랬다).
        table = f"""
        <form method='post' action='/candidates/delete'>
          <input type='hidden' name='back' value='{html.escape("/" + 조건쿼리)}'>
          <p class='bar'>{묶음단추}<button type='submit' class='danger ghost'
               onclick="return window.confirm('선택한 지원자를 삭제합니다. 되돌릴 수 없습니다.')"
               >선택 삭제</button>
             <span class='muted'>체크한 사람에게 적용합니다.</span></p>
          <div class='scroll'><table data-name='인재 Pool'
                data-export='/export.xlsx{조건쿼리}'>
            <tr><th><input type='checkbox' onclick="selectVisible(this)"
                title='보이는 줄만 선택합니다'>
            </th><th></th>{head}</tr>
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

    알림 = _알림(msg=msg)
    return _page(
        "인재 Pool",
        f"""{알림}{''.join(warns)}{처리중알림}
        <div class='card'>
          <h2>인재 Pool {len(records)}명{f' / 전체 {전체}명' if len(records) != 전체 else ''}<span class='muted'> · 채용 중 {len(채용중)}명</span></h2>
          <form method='get' action='/' style='margin-bottom:12px'>
            <input type='text' name='q' value='{html.escape(q)}' placeholder='이름·소속·학교·파일명 검색'>
            <select name='year'><option value=''>전체 년도</option>{연도선택}</select>
            <label class='muted'><input type='checkbox' name='review' value='1'{checked}>
              검토 필요만</label>
            <button type='submit'>검색</button>
            <a class='btn sec' href='/'>초기화</a>
          </form>

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
// 머리줄 전체선택 — **보이는 줄만** 고른다.
// 표 위 찾기 칸으로 걸러 놓고 전체선택을 눌렀을 때, 화면에 없는 사람까지
// 선택되면 그대로 메일이 나가거나 지워진다.
function selectVisible(head){
  var tb = head.closest('table');
  var 센것 = 0;
  bodyRows(tb).forEach(function(tr){
    if(tr.classList.contains('hide')) return;
    var c = tr.querySelector('input[name=ids]');
    if(c){ c.checked = head.checked; if(c.checked) 센것++; }
  });
  showPicked(tb);
}

// 지금 몇 명 골랐는지 표 위에 적어 둔다.
function showPicked(tb){
  var bar = tb.__bar;
  if(!bar) return;
  var 칸 = bar.querySelector('.tpicked');
  if(!칸){
    칸 = document.createElement('span');
    칸.className = 'muted tpicked';
    bar.querySelector('.tcount').after(칸);
  }
  var n = tb.querySelectorAll('input[name=ids]:checked').length;
  칸.textContent = n ? ' · 고른 사람 ' + n + '명' : '';
}

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
  var 머리 = tb.querySelector('th input[type=checkbox]');
  if(머리) 머리.checked = false;      // 걸러내면 전체선택도 풀린다
  showPicked(tb);
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
  // 줄마다 체크를 켜고 끌 때도 고른 사람 수를 따라가게 한다
  tb.addEventListener('change', function(ev){
    if(ev.target && ev.target.name === 'ids') showPicked(tb);
  });
  showPicked(tb);
  // 내려받기 단추는 표마다 **하나뿐**이다 (예전에는 카드 위에도 있었다).
  //
  // 표에 data-export 가 달려 있으면 서버가 만든 엑셀을 받는다 — 전화번호
  // 앞자리 0 이 살아 있고 열 너비도 잡혀 있다. 다만 서버는 화면에서 방금
  // 걸러낸 것까지는 모르므로, **표 위 찾기·열 필터를 쓰고 있으면** 보이는
  // 줄 그대로 만들어 보낸다. 화면과 파일이 다르면 안 된다.
  bar.querySelector('.txlsx').addEventListener('click', function(){
    var 걸러냄 = (tb.__q || '').trim() !== ''
      || Object.keys(tb.__filters || {}).length > 0;
    var 서버 = tb.dataset.export || '';
    window.__leaving = true;
    if(서버 && !걸러냄){
      location.href = 서버;
      setTimeout(function(){ window.__leaving = false; }, 1000);
      return;
    }
    var form = document.createElement('form');
    form.method = 'post'; form.action = '/table.xlsx';
    form.innerHTML = "<input type='hidden' name='name'><input type='hidden' name='tsv'>";
    form.elements.name.value = tb.dataset.name || document.title;
    form.elements.tsv.value = tableTSV(tb);
    document.body.appendChild(form);
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


#: DB 에는 있지만 CV 에서 뽑은 값이 아닌 열 — 언제 등록했고 원본이 무엇인지.
#: 표에서 고칠 수 없다 (등록년도는 상세 화면에서 고친다).
MANAGE_COLUMNS = ("등록년도", "등록일시", "원본_파일명", "보관_만료일")


def 열목록(registry_=None) -> list[tuple[str, str, bool]]:
    """이 시스템이 아는 **모든 열**을 (구분, 열이름, 추가한열인가) 로 돌려준다.

    표 항목 탭이 이걸 그대로 보여준다. 지원자 정보 열만 관리할 수 있으면
    채용 현황 열 이름을 못 바꾸고, 관리 정보 열은 아예 표에 못 올린다.

    추가한 열도 **어느 표에 속하는지**를 달고 그 묶음 안에 들어간다.
    직접 만든 '면접 평점' 이 지원자 정보인지 채용 현황인지 모르면, 어느 표에서
    찾아야 하는지도 알 수 없다.
    """
    reg = registry_ or registry
    out: list[tuple[str, str, bool]] = []
    out += [("지원자 정보", c, False) for c in table_columns(reg)]
    out += [("지원자 정보", c, True) for c in 추가열("지원자 정보")]
    # 표에 나오는데 여기 없으면 이름을 바꾸거나 숨길 방법이 없다.
    # 지원자_ID 는 내부 열쇠지만 표에 올릴 수 있어야 한다 (엑셀에서 대조할 때 쓴다).
    out += [("관리 정보", c, False) for c in ("지원자_ID", *MANAGE_COLUMNS, MAIL_COLUMN)]
    out += [("채용 현황", c, False) for c in RECRUIT_COLUMNS]
    out += [("채용 현황", c, True) for c in 추가열("채용 현황")]
    return out


def 추가열(구분: str) -> list[str]:
    """그 묶음에 속하는 추가한 열 이름."""
    return store.field_names(구분)


#: 지원자마다 어떤 메일을 보냈는지 한 열로. 세어 나오는 값이라 못 고친다.
MAIL_COLUMN = "메일_발송이력"


def 지원자열(registry_=None) -> list[str]:
    """인재 Pool·엑셀에 나갈 수 있는 열 **전부.**

    지원자 정보뿐 아니라 채용 현황 열과 메일 발송이력까지 한 표에서 본다.
    한 사람에 대해 아는 것을 보려고 화면을 옮겨 다니지 않아도 되게.
    채용 열은 여기서 **보기만** 한다 (고치는 건 채용 현황 화면 몫).
    """
    return (list(table_columns(registry_ or registry))
            + ["지원자_ID"] + list(MANAGE_COLUMNS)
            + 추가열("지원자 정보") + list(RECRUIT_COLUMNS)
            + 추가열("채용 현황") + [MAIL_COLUMN])


#: 관리 정보 중 처음에는 접어 두는 열. 표가 넓어지기만 하고 평소엔 안 본다.
#: 표 항목 탭에서 숨김을 풀면(설정이 생기면) 그때부터 보인다.
MANAGE_HIDDEN_BY_DEFAULT = ("지원자_ID", "등록일시", "원본_파일명", "보관_만료일")


def 기본숨김(col: str, cfg: dict) -> bool:
    """설정을 한 번도 안 건드린 관리 정보 열인가."""
    return col in MANAGE_HIDDEN_BY_DEFAULT and col not in cfg


def 표열(registry_=None) -> list[str]:
    """지원자 표에 실제로 나갈 열 (숨김·순서 설정 반영)."""
    cfg = store.column_config()
    return store.arrange([c for c in 지원자열(registry_) if not 기본숨김(c, cfg)])


#: 열 이름별 너비 등급. 값이 짧은 열에 넓은 자리를 주면 정작 긴 글이 잘린다.
_넓은열 = {
    "경력_요약", "검토_사유", "연구분야_키워드", "1저자_해외논문_제출처",
    "메일_발송이력", "비고", "현재_소속_상세", "중복_메모", "원본_파일명",
}
_중간열 = {
    "한글_이름", "영문_이름", "이메일", "전화번호", "현재_소속", "부서", "과제",
    "경력_회사", "직책", "최종상태", "박사_학교", "석사_학교", "학사_학교",
    "박사_전공", "석사_전공", "학사_전공", "박사_지도교수", "석사_지도교수",
    "현재_지도교수", "등록일시", "보관_만료일",
}
_짧은열 = {
    "검토_필요", "박사_석박통합", "등록년도", "현재_신분",
    "박사_학위상태", "생년월일",
}


def 머리글(이름: str) -> str:
    """표 머리글 HTML. 밑줄 뒤에서 줄바꿈해도 된다고 알려준다.

    `저널_주저자_수` 처럼 공백 없는 긴 이름은 한 낱말로 취급돼 줄바꿈이 안
    되고, 값은 한 글자뿐인 열을 통째로 넓혀 버린다. <wbr> 로 끊을 자리를
    준다 (글자를 넣는 게 아니라 '여기서 끊어도 된다' 는 표시다).
    """
    return html.escape(이름).replace("_", "_<wbr>")


def 열폭(col: str) -> str:
    """이 열에 줄 너비 등급 (CSS 클래스 이름)."""
    if col in _넓은열:
        return "w-xl"
    if col in _중간열:
        return "w-lg"
    if col in _짧은열:
        return "w-sm"
    if col.endswith("_수") or col.startswith(TIER_COLUMN_PREFIX):
        return "w-xs"
    if col.endswith(("_시작", "_졸업", "_종료")):
        return "w-sm"
    if col in STAGES:
        return "w-sm"
    return "w-md"


def 라벨(열들: list[str]) -> dict[str, str]:
    return store.labels(열들)


def _표값맵() -> dict[str, dict[str, str]]:
    """추출 결과에 없는 열의 값. {지원자_ID: {열: 값}}

    관리 정보 · 추가한 열 · 채용 현황 · 메일 발송이력을 한 번에 모은다.
    화면과 엑셀이 **같은 함수**를 쓰므로 둘이 어긋날 수 없다.
    """
    합침 = store.meta_map()
    for cid, 값들 in store.custom_map().items():
        합침.setdefault(cid, {}).update(값들)

    부서명 = {d["id"]: d["이름"] for d in auth.departments()}
    과제명 = {p["id"]: p["이름"] for p in auth.projects()}
    for cid, p in recruit.all().items():
        칸 = 합침.setdefault(cid, {})
        칸["부서"] = 부서명.get(p.부서_id, "")
        칸["과제"] = 과제명.get(p.project_id, "")
        칸["최종상태"] = p.최종상태
        칸["비고"] = p.비고
        for 단계 in STAGES:
            칸[단계] = p.단계상태.get(단계, "")
    for cid, 보낸것 in mailing.sent_summary().items():
        합침.setdefault(cid, {})[MAIL_COLUMN] = 보낸것

    # 검토 사유는 **남은 것만** 보여준다. 사람이 '확인함' 을 눌렀는데도 원문이
    # 표에 그대로 남아 있으면 아직 볼 게 있는 것처럼 보인다. DB 원문은 그대로
    # 두고 (LLM 이 무엇을 확신 못 했는지의 기록이다) 보이는 글만 줄인다.
    끝낸것 = store.review_done_map()
    시작한사람 = recruit.started()
    for rec in store.list_all():
        cid = rec.지원자_ID
        칸 = 합침.setdefault(cid, {})
        # 표에 나오는 값은 전부 여기서 나와야 표 항목 탭에서 관리할 수 있다.
        칸["지원자_ID"] = cid
        칸[STARTED_COLUMN] = "채용 중" if cid in 시작한사람 else "인재 Pool"
        if (rec.검토_사유 or "").strip():
            칸["검토_사유"] = review.display(rec.검토_사유, 끝낸것.get(cid, set()))
    return 합침


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
var RT = {editor: null, subject: null, last: null, range: null, cell: null};

function rtInit(){
  RT.editor = document.getElementById('rtbody');
  if(!RT.editor) return;
  RT.subject = document.querySelector('input[name=subject]');
  RT.last = RT.editor;
  try { document.execCommand('styleWithCSS', false, true); } catch(e) {}

  ['keyup','mouseup','input'].forEach(function(ev){
    RT.editor.addEventListener(ev, function(){
      RT.last = RT.editor; rtSave(); rtSyncTableBar();
    });
  });
  document.addEventListener('selectionchange', function(){
    if(document.activeElement !== RT.editor) return;
    rtSave(); rtSyncTableBar();
  });
  if(RT.subject) RT.subject.addEventListener('focus', function(){ RT.last = RT.subject; });

  // 도구를 눌러도 커서를 잃지 않게 한다 (이게 편집이 들쭉날쭉하던 원인)
  // 표 도구도 같다 — 커서가 표 안에 있어야 어느 칸에 적용할지 알 수 있다.
  document.querySelectorAll('.rt-bar').forEach(function(bar){
    bar.addEventListener('mousedown', function(e){
      if(e.target.closest('input[type=color], input[type=file],'
                          + ' input[type=number], select')) return;
      e.preventDefault();
    });
  });

  rtDragInit();
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
function closeRtDrop(){
  var m = document.getElementById('rtdrop');
  if(m) m.remove();
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
/* 격자에서 고르는 크기. 예전에는 6×6 이 끝이라 그보다 큰 표를 아예 못 만들었다.
   격자를 넓히고, 그보다 더 크면 숫자로 직접 치게 한다. */
var RT_GRID_R = 10, RT_GRID_C = 10;

function rtTableMenu(btn){
  var html = "<div class='rt-grid' style='grid-template-columns:repeat("
    + RT_GRID_C + ",14px)'>";
  for(var r = 1; r <= RT_GRID_R; r++){
    for(var c = 1; c <= RT_GRID_C; c++){
      html += "<i data-v='" + r + "x" + c + "' data-r='" + r + "' data-c='" + c + "'></i>";
    }
  }
  html += "</div><div class='rt-gridlabel'>표 크기를 고르세요</div>"
    + "<div class='rt-gridmore'>더 크게: "
    + "<input type='number' id='rt-mr' min='1' max='60' value='3' style='width:46px'>행 × "
    + "<input type='number' id='rt-mc' min='1' max='30' value='3' style='width:46px'>열 "
    + "<button type='button' id='rt-mgo'>넣기</button></div>";
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
  /* 숫자 칸은 격자와 달리 클릭이 메뉴를 닫으면 안 된다 */
  m.addEventListener('mousedown', function(e){
    if(e.target.closest('.rt-gridmore')) e.stopPropagation();
  });
  m.querySelector('#rt-mgo').addEventListener('click', function(){
    var R = parseInt(m.querySelector('#rt-mr').value, 10);
    var C = parseInt(m.querySelector('#rt-mc').value, 10);
    if(R > 0 && C > 0) rtTable(R, C);
    closeRtDrop();
  });
}

/* 메일에서 표가 깨지는 걸 막으려면 인라인 스타일이어야 한다 (<style> 은 지워진다). */
var RT_CELL = 'border:1px solid #999;padding:6px;vertical-align:top';

function rtTable(행, 열){
  if(!행 || !열) return;
  var s = "<table class='rt-tbl' style='border-collapse:collapse;width:100%;"
    + "font-size:11pt' width='100%' cellpadding='0' cellspacing='0'>";
  for(var r = 0; r < 행; r++){
    s += '<tr>';
    for(var c = 0; c < 열; c++){ s += "<td style='" + RT_CELL + "'>&nbsp;</td>"; }
    s += '</tr>';
  }
  s += '</table><p><br></p>';
  rtInsert(s);
}

/* ---- 넣은 뒤에 고치기 -------------------------------------------------------
   예전에는 표를 넣고 나면 손댈 방법이 없어서, 열 하나를 더 넣으려고 표를 지우고
   처음부터 다시 만들어야 했다. 커서가 든 표를 찾아서 그 자리에서 고친다. */
/* 커서가 지금 든 칸. 없으면 null. */
function rtCellNow(){
  var sel = window.getSelection();
  if(!sel || !sel.rangeCount || !RT.editor) return null;
  var n = sel.getRangeAt(0).startContainer;
  if(n.nodeType !== 1) n = n.parentNode;
  var td = n && n.closest ? n.closest('td,th') : null;
  return (td && RT.editor.contains(td)) ? td : null;
}
/* 도구가 손댈 칸.
   너비 칸이나 테두리 목록을 **누르는 순간 편집기 커서를 잃는다** (포커스가
   그 칸으로 옮겨간다). 그래서 마지막으로 커서가 있던 칸을 기억해 두고 쓴다.
   기억한 칸이 지워졌으면(행·열 삭제) 버린다. */
function rtCellAt(){
  var 지금 = rtCellNow();
  if(지금){ RT.cell = 지금; return 지금; }
  var 기억 = RT.cell;
  if(기억 && RT.editor && RT.editor.contains(기억)) return 기억;
  RT.cell = null;
  return null;
}
function rtTableAt(){
  var td = rtCellAt();
  return td ? td.closest('table') : null;
}
function rtColIndex(td){
  return Array.prototype.indexOf.call(td.parentNode.children, td);
}
function rtRows(t){ return Array.prototype.slice.call(t.rows); }

function rtRow(어디){                      /* -1 위, +1 아래 */
  var td = rtCellAt(); if(!td) return;
  var tr = td.parentNode, 새 = tr.cloneNode(true);
  Array.prototype.forEach.call(새.cells, function(c){ c.innerHTML = '&nbsp;'; });
  tr.parentNode.insertBefore(새, 어디 < 0 ? tr : tr.nextSibling);
  rtTouched();
}
function rtRowDel(){
  var td = rtCellAt(); if(!td) return;
  var t = td.closest('table');
  if(t.rows.length <= 1){ rtTableDel(); return; }   /* 마지막 줄이면 표째 */
  td.parentNode.parentNode.removeChild(td.parentNode);
  rtTouched();
}
function rtCol(어디){                      /* -1 왼쪽, +1 오른쪽 */
  var td = rtCellAt(); if(!td) return;
  var i = rtColIndex(td), t = td.closest('table');
  rtRows(t).forEach(function(tr){
    var 기준 = tr.cells[i];
    var 새 = document.createElement(기준 && 기준.tagName === 'TH' ? 'th' : 'td');
    새.setAttribute('style', 기준 ? 기준.getAttribute('style') || RT_CELL : RT_CELL);
    새.innerHTML = '&nbsp;';
    if(어디 < 0) tr.insertBefore(새, 기준 || null);
    else tr.insertBefore(새, 기준 ? 기준.nextSibling : null);
  });
  rtTouched();
}
function rtColDel(){
  var td = rtCellAt(); if(!td) return;
  var i = rtColIndex(td), t = td.closest('table');
  if(t.rows[0] && t.rows[0].cells.length <= 1){ rtTableDel(); return; }
  rtRows(t).forEach(function(tr){ if(tr.cells[i]) tr.deleteCell(i); });
  rtTouched();
}
/* 표 전체의 너비.
   예전에는 만들 때 width:100% 를 박아 두고 그것뿐이었다. 그러면 열 너비를
   아무리 고쳐도 **정해진 폭을 나눠 갖는 것**이라, 열 하나만 넓히는 게 아예
   불가능했다 (옆 열이 그만큼 줄어든다). 표 폭 자체를 정할 수 있어야 한다. */
function rtTableWidth(){
  var t = rtTableAt(); if(!t) return;
  var sel = document.getElementById('rt-tblw');
  var px  = document.getElementById('rt-tblpx');
  var 값 = sel ? sel.value : '100%';
  if(px) px.style.display = (값 === 'px') ? '' : 'none';
  if(값 === 'px'){
    var n = parseInt(px && px.value, 10);
    if(!(n > 0)) return;                       /* 아직 안 쳤다 */
    t.style.width = n + 'px'; t.setAttribute('width', n);
  } else if(값 === 'fit'){
    /* 열마다 px 로 잡고 표는 그 합계를 따른다 — 열 너비 조절이 제대로 되는 길 */
    rtSeedColPx(t);
    rtSumToTable(t);
  } else if(값 === 'auto'){
    t.style.width = 'auto'; t.removeAttribute('width');
    t.style.tableLayout = '';
    rtRows(t).forEach(function(tr){
      Array.prototype.forEach.call(tr.cells, function(c){
        c.style.width = ''; c.removeAttribute('width');
      });
    });
  } else {
    t.style.width = '100%'; t.setAttribute('width', '100%');
  }
  rtTouched();
}

/* 열 너비. px 로도, % 로도 잡을 수 있다.
   px 로 잡으려면 표가 '창에 맞춤' 이면 안 된다 — 그건 폭이 이미 정해진
   것이라 px 가 의미를 잃는다. 그래서 px 를 쓰면 표를 '내용에 맞춤' 으로
   옮겨 준다. 말없이 안 먹는 것보다 낫다. */
function rtColWidth(){
  var td = rtCellAt(); if(!td) return;
  var 칸 = document.getElementById('rt-colw');
  var 단위 = document.getElementById('rt-colu');
  var u = 단위 ? 단위.value : 'px';
  rtSetColWidth(td, 칸 ? 칸.value : '', u);
}

/* 지금 그려진 폭을 그대로 px 로 못박는다.
   폭이 안 정해진 표를 '열 너비에 맞춤' 으로 옮기면 표가 글자 길이만큼 쪼그라든다
   (빈 표는 50px 쯤 된다). 보이던 모습 그대로에서 시작해야 놀라지 않는다. */
function rtSeedColPx(t){
  var 첫줄 = t.rows[0]; if(!첫줄) return;
  var 폭 = Array.prototype.map.call(첫줄.cells, function(c){
    return Math.round(c.getBoundingClientRect().width);
  });
  t.style.tableLayout = 'fixed';
  rtRows(t).forEach(function(tr){
    Array.prototype.forEach.call(tr.cells, function(c, i){
      if(!폭[i]) return;
      c.style.width = 폭[i] + 'px';
      c.setAttribute('width', String(폭[i]));
    });
  });
}

/* 표 폭 = 열 폭의 합.
   **엑셀과 같다 — 열을 넓히면 표가 따라 넓어진다.** 표 폭이 먼저 고정돼 있으면
   열 하나를 넓힐 때 옆 열이 그만큼 줄어들 뿐이라, 열 너비 조절이 반쪽이 된다. */
function rtSumToTable(t){
  var 첫줄 = t.rows[0]; if(!첫줄) return;
  var 합 = 0;
  for(var i = 0; i < 첫줄.cells.length; i++){
    var v = 첫줄.cells[i].style.width || '';
    if(v.slice(-2) !== 'px') return;            /* px 아닌 열이 있으면 손대지 않는다 */
    합 += parseFloat(v) || 0;
  }
  if(!(합 > 0)) return;
  t.style.width = Math.round(합) + 'px';
  t.setAttribute('width', String(Math.round(합)));
  var sel = document.getElementById('rt-tblw');
  if(sel) sel.value = 'fit';
  var px = document.getElementById('rt-tblpx');
  if(px) px.style.display = 'none';
}

function rtSetColWidth(td, 값, 단위){
  var i = rtColIndex(td), t = td.closest('table');
  var w = parseFloat(값);
  /* px 로 잡는다는 건 '이 열을 이만큼' 이라는 뜻이다. 표 폭이 먼저 정해져
     있으면 그 말이 지켜지지 않으므로, 표를 열 합계에 맞추는 쪽으로 옮긴다. */
  if(단위 === 'px' && w > 0){
    var 첫 = t.rows[0] && t.rows[0].cells[0];
    if(!첫 || (첫.style.width || '').slice(-2) !== 'px') rtSeedColPx(t);
  }
  /* 너비를 정한 표는 table-layout:fixed 여야 정한 대로 선다. 안 그러면
     브라우저가 내용 길이를 보고 제멋대로 다시 나눈다. */
  if(w > 0) t.style.tableLayout = 'fixed';
  rtRows(t).forEach(function(tr){
    var c = tr.cells[i]; if(!c) return;
    /* 메일 클라이언트(특히 Outlook)는 width 속성을 스타일보다 잘 따른다 */
    if(w > 0){
      c.style.width = w + 단위;
      c.setAttribute('width', 단위 === 'px' ? String(Math.round(w)) : w + '%');
    } else {
      c.style.width = ''; c.removeAttribute('width');
    }
  });
  if(단위 === 'px' && w > 0) rtSumToTable(t);
  rtTouched();
}

/* 경계선을 끌어서 넓히기.
   숫자를 치는 것보다 이게 먼저 손이 간다. 칸의 오른쪽 끝 4px 안에서 누르면
   그 열의 너비를 끄는 대로 바꾼다. contenteditable 이 글자를 고르려 들기
   때문에 mousedown 에서 기본 동작을 막아야 한다. */
var RTDrag = null;
function rtDragInit(){
  if(!RT.editor) return;
  RT.editor.addEventListener('mousemove', function(e){
    if(RTDrag) return;
    var td = e.target.closest && e.target.closest('td,th');
    var 끝인가 = td && (td.getBoundingClientRect().right - e.clientX) <= 5;
    RT.editor.style.cursor = 끝인가 ? 'col-resize' : '';
  });
  RT.editor.addEventListener('mousedown', function(e){
    var td = e.target.closest && e.target.closest('td,th');
    if(!td) return;
    var r = td.getBoundingClientRect();
    if(r.right - e.clientX > 5) return;
    e.preventDefault();
    RTDrag = {td: td, x: e.clientX, w: r.width};
    RT.cell = td;
  });
  document.addEventListener('mousemove', function(e){
    if(!RTDrag) return;
    var 새폭 = Math.max(24, Math.round(RTDrag.w + (e.clientX - RTDrag.x)));
    rtSetColWidth(RTDrag.td, 새폭, 'px');
    var 칸 = document.getElementById('rt-colw');
    var 단위 = document.getElementById('rt-colu');
    if(칸) 칸.value = 새폭;
    if(단위) 단위.value = 'px';
  });
  document.addEventListener('mouseup', function(){
    if(!RTDrag) return;
    RTDrag = null;
    RT.editor.style.cursor = '';
  });
}
function rtBorder(값){
  var t = rtTableAt(); if(!t) return;
  t.querySelectorAll('td,th').forEach(function(c){
    c.style.border = (값 === 'none') ? 'none' : 값;
  });
  rtTouched();
}
function rtHeadRow(켬){
  var t = rtTableAt(); if(!t || !t.rows.length) return;
  Array.prototype.forEach.call(t.rows[0].cells, function(c){
    c.style.fontWeight = 켬 ? 'bold' : '';
    c.style.background = 켬 ? '#eef2f7' : '';
  });
  rtTouched();
}
function rtTableDel(){
  var t = rtTableAt(); if(!t) return;
  if(!window.confirm('이 표를 통째로 지웁니다.')) return;
  t.parentNode.removeChild(t);
  rtTouched();
}
function rtTouched(){
  if(RT.editor) RT.editor.dispatchEvent(new Event('input', {bubbles: true}));
  rtSyncTableBar();
}
/* 커서가 표 안에 있을 때만 표 도구를 보여 준다. 늘 떠 있으면 자리만 차지하고,
   무엇에 적용되는지도 알 수 없다. */
function rtSyncTableBar(){
  var bar = document.getElementById('rttablebar');
  if(!bar) return;
  /* 편집기 안에 커서가 있을 때만 기억을 갱신한다. 도구 칸에 포커스가 가 있는
     동안에는 기억한 칸을 그대로 두어야 표 도구가 사라지지 않는다. */
  var 안에있나 = document.activeElement === RT.editor;
  var td = 안에있나 ? rtCellNow() : null;
  if(안에있나) RT.cell = td;
  if(!td) td = (RT.cell && RT.editor.contains(RT.cell)) ? RT.cell : null;
  bar.hidden = !td;
  if(!td) return;
  var t = td.closest('table');
  var w = bar.querySelector('#rt-colw'), u = bar.querySelector('#rt-colu');
  if(w && document.activeElement !== w){
    var v = td.style.width || '';
    if(v.slice(-1) === '%'){ w.value = parseFloat(v); if(u) u.value = '%'; }
    else if(v.slice(-2) === 'px'){ w.value = parseFloat(v); if(u) u.value = 'px'; }
    else w.value = '';
  }
  /* 표 너비 칸도 지금 표에 맞춰 둔다. 안 그러면 다른 표로 옮겨도 앞 표의
     설정이 남아 있어서, 건드리는 순간 엉뚱한 값이 적용된다. */
  var tw = bar.querySelector('#rt-tblw'), tp = bar.querySelector('#rt-tblpx');
  if(tw && document.activeElement !== tw && document.activeElement !== tp){
    var 폭 = t.style.width || '100%';
    var 첫 = t.rows[0] && t.rows[0].cells[0];
    var 열px = 첫 && (첫.style.width || '').slice(-2) === 'px';
    if(폭 === '100%') tw.value = '100%';
    else if(폭 === 'auto' || 폭 === '') tw.value = 'auto';
    else if(열px) tw.value = 'fit';            /* 열 합계를 따르는 중 */
    else { tw.value = 'px'; if(tp) tp.value = parseFloat(폭); }
    if(tp) tp.style.display = (tw.value === 'px') ? '' : 'none';
  }
  var h = bar.querySelector('#rt-head');
  if(h) h.checked = !!(t.rows[0] && t.rows[0].cells[0]
                       && t.rows[0].cells[0].style.fontWeight === 'bold');
  var b = bar.querySelector('#rt-border');
  if(b){
    var 현재 = td.style.border || '1px solid #999';
    var 있나 = Array.prototype.some.call(b.options, function(o){ return o.value === 현재; });
    if(있나) b.value = 현재;
  }
}
function rtLink(){
  var url = prompt('링크 주소를 넣으세요', 'https://');
  if(url) rtCmd('createLink', url);
}
// 그림은 본문에 base64 로 박지 않고 **서버에 파일로 올린다.**
// 본문에는 짧은 참조만 남는다. 원본이 본문 글자 안에만 있으면, 본문이 한 번
// 상했을 때 되돌릴 방법이 없다.
function rtImage(input){
  var f = input.files && input.files[0];
  input.value = '';
  if(!f) return;
  if(f.size > 2 * 1024 * 1024){
    토스트('그림이 너무 큽니다 (' + Math.round(f.size / 1024) + 'KB). '
      + '본문에 넣는 그림은 2MB 까지입니다. 큰 파일은 첨부로 붙이세요.', 1);
    return;
  }
  var fd = new FormData();
  fd.append('template', window.템플릿ID || '0');
  fd.append('file', f, f.name);
  rtInsert("<span id='rtimgwait' class='muted'>그림 올리는 중…</span>");
  fetch('/mail/image/add', {method: 'POST', body: fd})
    .then(function(r){ return r.json(); })
    .then(function(res){
      var 자리 = document.getElementById('rtimgwait');
      if(자리 && 자리.parentNode){
        if(res.ok){
          var img = document.createElement('img');
          img.src = res.src;
          img.style.maxWidth = '100%';
          자리.parentNode.replaceChild(img, 자리);
        }else{
          자리.parentNode.removeChild(자리);
          토스트(res.error || '그림을 올리지 못했습니다.', 1);
        }
      }
    })
    .catch(function(e){
      var 자리 = document.getElementById('rtimgwait');
      if(자리 && 자리.parentNode) 자리.parentNode.removeChild(자리);
      토스트('그림을 올리지 못했습니다: ' + e, 1);
    });
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
              var 설명 = (window.자리표시자설명 || {})[v];
              return "<button type='button' class='vm-item' data-v='" + v + "'>"
                + v
                + (설명 ? " <span class='muted'>— " + 설명 + "</span>" : "")
                + "</button>";
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


def _등급이름(m: dict) -> str:
    """저장된 점수로 등급 이름을 되살린다 (Match.등급 과 같은 눈금)."""
    if not m.get("평가됨", True):
        return "미평가"
    점수 = m.get("점수") or 0
    for 문턱, 이름 in ((90, "매우 적합"), (70, "적합"), (50, "인접 분야"),
                    (30, "기초만 겹침")):
        if 점수 >= 문턱:
            return 이름
    return "접점 없음"


def _점수색(점수: int) -> str:
    if 점수 >= 90:
        return "p-완료"
    if 점수 >= 70:
        return "p-처리중"
    if 점수 >= 50:
        return "p-검토필요"
    return "p-대기중"


def _curate_page(me: User, error: str = "", msg: str = "") -> bytes:
    """원본 과제 파일을 읽고 **어떤 과제·어떤 정보만 남길지** 고른다.

    원본에는 매칭에 쓸모없는 항목이 많다. 그대로 LLM 에 밀어 넣으면 프롬프트만
    길어지고 판단이 흐려진다. 사람이 한 번 골라 다듬은 파일을 만들고, 매칭은
    그 파일을 쓴다. **원본은 건드리지 않는다.**
    """
    원본경로 = settings.projects_json
    다듬 = 다듬은파일()
    메타 = projectsmod.curated_meta(다듬)

    try:
        data = projectsmod.read_json(원본경로)
        항목 = projectsmod.raw_items(data)
        필드 = projectsmod.field_stats(항목)
        읽기오류 = ""
    except projectsmod.ProjectsError as exc:
        항목, 필드, 읽기오류 = [], [], str(exc)

    # 지금 다듬은 파일에 들어 있는 것을 미리 체크해 둔다
    고른필드 = set(메타.get("필드") or [])
    고른과제: set[str] = set()
    if 메타:
        try:
            고른과제 = {p.키 for p in projectsmod.load(다듬)}
            고른과제 |= {p.이름 for p in projectsmod.load(다듬)}
        except projectsmod.ProjectsError:
            고른과제 = set()
    처음 = not 메타

    필드줄 = "".join(
        f"<tr><td><label><input type='checkbox' form='curform' name='fields'"
        f" value='{html.escape(f.이름)}'"
        + (" checked disabled" if f.필수 else
           (" checked" if (처음 or f.이름 in 고른필드) else ""))
        + f"> <b>{html.escape(f.라벨)}</b></label>"
        + ("<br><span class='muted'>과제 이름이라 항상 남습니다</span>" if f.필수 else "")
        + f"</td><td class='muted'>{html.escape(f.이름)}</td>"
        f"<td>{f.채운수}/{f.전체수} <span class='muted'>({f.비율}%)</span></td>"
        f"<td class='muted' title='{html.escape(f.예시)}'>"
        f"{html.escape(f.예시)}</td></tr>"
        for f in 필드
    ) or "<tr><td colspan='4' class='muted'>읽은 필드가 없습니다.</td></tr>"

    과제줄 = []
    for 기본키, 원본 in 항목:
        p = projectsmod.to_project(원본, 기본키)
        if p is None:
            continue
        키 = projectsmod.item_key(기본키, 원본)
        체크 = " checked" if (처음 or 키 in 고른과제 or p.이름 in 고른과제) else ""
        과제줄.append(
            f"<tr><td><input type='checkbox' form='curform' name='keys'"
            f" value='{html.escape(키)}'{체크}></td>"
            f"<td>{html.escape(p.담당)}</td>"
            f"<td><b>{html.escape(p.이름)}</b>"
            f"<br><span class='muted'>{html.escape(키)}</span></td>"
            f"<td>{html.escape(', '.join(p.키워드[:8]))}</td>"
            f"<td class='muted' title='{html.escape(p.설명[:400])}'>"
        f"{html.escape(p.설명[:150])}"
            f"{'…' if len(p.설명) > 150 else ''}</td></tr>"
        )
    과제표 = "".join(과제줄) or \
        "<tr><td colspan='5' class='muted'>읽은 과제가 없습니다.</td></tr>"

    현황 = (
        f"<table><tr><th style='width:150px'>원본 파일</th>"
        f"<td><code>{html.escape(str(projectsmod.resolve_path(원본경로) or '(설정 안 됨)'))}"
        f"</code> <span class='muted'>.env 의 CVTOOL_PROJECTS_JSON</span></td></tr>"
        f"<tr><th>원본 과제</th><td>{len(항목)}개 · 필드 {len(필드)}종</td></tr>"
        f"<tr><th>다듬은 파일</th><td><code>{html.escape(str(다듬))}</code></td></tr>"
        + (f"<tr><th>지금 쓰는 것</th><td><b>다듬은 파일</b> — 과제 {메타['과제수']}개 · "
           f"필드 {len(메타['필드'])}종 · {html.escape(메타['만든일시'])}"
           + (f" ({html.escape(메타['만든이'])})" if 메타.get("만든이") else "")
           + "</td></tr>"
           if 메타 else
           "<tr><th>지금 쓰는 것</th><td><b>원본 파일</b> — 아직 다듬지 않았습니다</td></tr>")
        + "</table>"
    )

    오류 = (_알림(err=error)
          + (f"<div class='warn'>{html.escape(읽기오류)}</div>" if 읽기오류 else ""))
    알림 = _알림(msg=msg)
    저장바 = (
        "<form method='post' action='/match/curate' id='curform' class='mergebar'>"
        "<button type='submit'>고른 것만 남겨 저장</button>"
        "<span class='muted'>원본은 그대로 두고 <b>다듬은 파일</b>을 새로 씁니다. "
        "저장하면 매칭은 이 파일을 씁니다.</span></form>"
        if 항목 else ""
    )
    지우기 = (
        "<form method='post' action='/match/curate/reset' style='margin-top:10px'"
        " onsubmit=\"return confirm('다듬은 파일을 지웁니다. 매칭은 다시 원본을 씁니다.')\">"
        "<button class='danger'>다듬은 파일 지우기</button>"
        "<span class='muted'> 원본 파일은 지워지지 않습니다.</span></form>"
        if 메타 else ""
    )
    return _page(
        "과제 파일 다듬기",
        알림 + 오류
        + "<div class='card'><h2>과제 파일</h2>" + 현황
        + "<p class='muted'>원본에 매칭과 상관없는 항목이 많으면 여기서 걸러내세요. "
        "프롬프트가 짧아지고 판단이 또렷해집니다.</p>"
        + f"<p><a class='btn sec' href='/match'>과제 매칭으로</a></p>{지우기}</div>"
        + 저장바
        + f"<div class='card'><h2>1. 남길 정보 고르기 <span class='muted'>필드 {len(필드)}종"
        "</span></h2>"
        "<p class='muted'>채움 비율이 낮거나(작성자·문서버전 같은) 매칭과 상관없는 "
        "필드는 빼세요.</p><div class='scroll'><table data-name='과제 필드'>"
        "<tr><th>남길까</th><th>원본 필드명</th><th>채움</th><th>예시</th></tr>"
        + 필드줄 + "</table></div></div>"
        + f"<div class='card'><h2>2. 남길 과제 고르기 <span class='muted'>"
        f"{len(과제줄)}개</span></h2>"
        "<div class='scroll'><table data-name='과제 고르기'>"
        "<tr><th style='width:34px'><input type='checkbox' title='전체 선택'"
        " onclick=\"for(const c of this.closest('table')"
        ".querySelectorAll('input[name=keys]'))"
        "if(!c.closest('tr').classList.contains('hide'))c.checked=this.checked\"></th>"
        "<th>부서</th><th>과제명</th><th>키워드</th><th>내용</th></tr>"
        + 과제표 + "</table></div></div>",
        me=me,
    )


def _projects_page(me: User, error: str = "", msg: str = "") -> bytes:
    """과제 정보 관리 — 어떤 과제 파일을 읽고 있고 무엇이 들어 있는지.

    예전에는 `과제 매칭` 탭에서 이 화면과 **지원자별 1순위 표**를 같이 보여줬다.
    지원자별 매칭은 어차피 지원자 상세에서 보므로 표는 뺐다. 여기 남는 것은
    과제 쪽 관리뿐이라 `부서·과제` 아래로 들어왔다.
    """
    목록, 파일오류 = 과제목록()
    경로 = projectsmod.resolve_path(쓰는과제파일()[0])
    _쓰는것, 다듬음 = 쓰는과제파일()
    설정 = (
        "<table><tr><th style='width:150px'>과제 파일</th>"
        f"<td><code>{html.escape(str(경로) if 경로 else '(설정 안 됨)')}</code>"
        + (" <span class='pill p-완료'>다듬은 파일</span>" if 다듬음
           else " <span class='pill p-대기중'>원본</span>")
        + "</td></tr>"
        f"<tr><th>읽은 과제</th><td>{len(목록)}개</td></tr>"
        f"<tr><th>맞춰본 지원자</th><td>{store.matched_count()}명 "
        f"/ 전체 {store.count()}명</td></tr>"
        f"<tr><th>자동 매칭</th><td>{'켜짐' if settings.match_auto else '꺼짐'} "
        "<span class='muted'>(CVTOOL_MATCH_AUTO)</span></td></tr>"
        f"<tr><th>비교 방식</th><td>과제 <b>전부</b>와 비교 · "
        f"한 번에 {settings.match_batch}개씩 물어봄 "
        "<span class='muted'>(CVTOOL_MATCH_BATCH)</span></td></tr></table>"
    )
    과제줄 = "".join(
        f"<tr><td>{html.escape(p.번호 or p.키)}</td><td><b>{html.escape(p.이름)}</b></td>"
        f"<td>{html.escape(', '.join(p.키워드))}</td>"
        f"<td>{html.escape(p.담당)}</td>"
        f"<td class='muted' title='{html.escape(p.설명[:400])}'>"
        f"{html.escape(p.설명[:160])}"
        f"{'…' if len(p.설명) > 160 else ''}</td></tr>"
        for p in 목록
    ) or "<tr><td colspan='5' class='muted'>읽은 과제가 없습니다.</td></tr>"

    오류 = (_알림(err=error)
          + (f"<div class='warn'>{html.escape(파일오류)}</div>" if 파일오류 else ""))
    알림 = _알림(msg=msg)
    실행 = (
        "<form method='post' action='/match/all' class='mergebar'"
        " onsubmit=\"return confirm('아직 안 맞춰본 지원자를 전부 맞춰 봅니다. "
        "사람이 많으면 시간이 걸립니다. 진행할까요?')\">"
        "<button type='submit'>안 맞춰본 지원자 맞춰보기</button>"
        "<label class='muted'><input type='checkbox' name='again' value='1'> "
        "이미 맞춰본 사람도 다시</label>"
        "<span class='muted'>과제 파일을 고쳤으면 다시 돌리세요.</span></form>"
        if can(me, "지원자_등록") and 목록 else ""
    )
    return _page(
        "과제 정보 관리",
        알림 + 오류
        + "<div class='card'><p><a class='btn sec' href='/org'>부서·과제로</a></p></div>"
        + "<div class='card'><h2>과제 파일</h2>" + 설정
        + "<p class='muted'>경로는 <code>.env</code> 의 "
        "<code>CVTOOL_PROJECTS_JSON</code> 으로 정합니다. 상대경로는 "
        "<b>CV-parser 폴더 기준</b>입니다.</p>"
        + ("<p><a class='btn' href='/match/curate'>과제 파일 다듬기</a>"
           "<span class='muted'> 원본에서 매칭에 쓸 과제·정보만 골라 둡니다.</span></p>"
           if can(me, "지원자_등록") else "")
        + "</div>"
        + f"<div class='card'><h2>연구 과제 {len(목록)}개</h2><div class='scroll'>"
        "<table data-name='연구 과제'><tr><th>번호</th><th>과제명</th><th>키워드</th>"
        "<th>담당</th><th>설명</th></tr>" + 과제줄 + "</table></div></div>"
        + "<div class='card'><h2>지원자 맞춰보기</h2>" + 실행
        + "<p class='muted'>지원자마다 <b>모든 과제와 비교</b>합니다. 결과는 "
        "<b>지원자 상세 화면</b>에서 봅니다 — 한 사람을 볼 때 같이 보는 게 "
        "맞아서 따로 목록을 두지 않습니다.</p>"
        f"<pre class='rubric'>{html.escape(SCORE_RUBRIC)}</pre></div>",
        me=me,
    )


def _org_hub_page(me: User) -> bytes:
    """부서·과제 탭의 첫 화면. 무엇을 할지 고른다."""
    목록, _오류 = 과제목록()
    return _page(
        "부서·과제",
        "<div class='card'><h2>부서·과제</h2>"
        "<p class='muted'>둘 중 무엇을 할지 고르세요.</p></div>"
        "<div class='card'><h2><a href='/org/edit'>부서·과제 편집</a></h2>"
        f"<p class='muted'>부서 {len(auth.departments())}개 · "
        f"과제 {len(auth.projects())}개 — 이름을 고치고, 새로 만들고, "
        "과제 초대암호를 겁니다. 현업 계정이 배정되는 그 과제입니다.</p>"
        "<p><a class='btn' href='/org/edit'>열기</a></p></div>"
        "<div class='card'><h2><a href='/match'>과제 정보 관리</a></h2>"
        f"<p class='muted'>연구 과제 파일 {len(목록)}개 — 매칭에 쓸 과제 파일을 "
        "확인하고 다듬습니다. 지원자와 맞춰보는 것도 여기서 돌립니다.</p>"
        "<p><a class='btn' href='/match'>열기</a></p></div>",
        me=me,
    )


def _busy_count() -> int:
    with _status_lock:
        return sum(1 for s in _status.values() if s["state"] in ("대기중", "처리중"))


def _upload_page(me: User) -> bytes:
    """지원자 추가 — CV 를 올리거나, CV 없이 빈 줄을 만든다.

    예전에는 지원자 목록 맨 위에 업로드 상자가 붙어 있어서, 표를 보러 올 때마다
    쓰지도 않는 상자가 화면을 차지했다. 탭으로 뺐다.
    """
    보관 = "켜짐 (재분석 가능)" if settings.store_cv_text else "꺼짐 (재분석하려면 재업로드 필요)"
    가능 = ", ".join(sorted(SUPPORTED_SUFFIXES))
    등록가능 = can(me, "지원자_등록")
    if not 등록가능:
        본문 = "<div class='card'><h2>지원자 추가</h2><p>추가 권한이 없습니다.</p></div>"
    else:
        본문 = f"""
        <div class='card'><h2>CV 올려서 추가</h2>
          <form method='post' action='/upload' enctype='multipart/form-data'>
            <p><input type='file' name='files' multiple accept='{가능}'></p>
            <button type='submit'>업로드 후 분석</button>
            <span class='muted'>여러 개를 한 번에 고를 수 있습니다 ({가능}).</span>
          </form>
          <p class='muted'>분석은 뒤에서 돌아갑니다. 끝나면 아래 현황에 뜨고
          <a href='/'>인재 Pool</a>에 줄이 생깁니다.</p>
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
    return _page("지원자 추가", 본문 + _status_table() + _STATUS_POLL_JS, me=me)


#: 현황 표만 갈아 끼우는 폴링.
#:
#: <meta refresh> 로 페이지를 통째로 다시 그리면 분석이 도는 동안 파일을 고를 수
#: 없다 — 고르는 순간 새로고침이 끼어들어 선택이 풀린다. 그래서 표 안쪽만 바꾼다.
#: 처리 중일 때만 돌고, 다 끝나면 스스로 멈춘다 (빈 서버를 계속 두드리지 않는다).
_STATUS_POLL_JS = """
<script>
(function(){
  var 표 = document.getElementById('현황표');
  var 알림 = document.getElementById('상태알림');
  if(!표 || !알림 || !알림.dataset.busy) return;
  var 타이머 = setInterval(function(){
    fetch('/status/rows', {credentials: 'same-origin'})
      .then(function(r){ return r.ok ? r.text() : null; })
      .then(function(html){
        if(html === null) return;
        var 담을것 = document.createElement('div');
        담을것.innerHTML = html;
        var 새표 = 담을것.querySelector('#현황표');
        var 새알림 = 담을것.querySelector('#상태알림');
        if(새표) 표.innerHTML = 새표.innerHTML;
        if(새알림){
          알림.innerHTML = 새알림.innerHTML;
          if(!새알림.dataset.busy){        /* 다 끝났다 — 그만 두드린다 */
            알림.dataset.busy = '';
            clearInterval(타이머);
          }
        }
      })
      .catch(function(){ /* 잠깐 끊긴 것뿐이다. 다음 차례에 다시 해본다 */ });
  }, 3000);
})();
</script>"""


def _candidate_page(지원자_ID: str, me: User, error: str = "",
                    msg: str = "") -> bytes:
    rec = store.get(지원자_ID)
    if rec is None:
        return _page("없음", "<div class='card'>해당 지원자를 찾을 수 없습니다.</div>")
    meta = store.meta(지원자_ID) or {}
    row = rec.to_row(registry)
    수정가능 = can(me, "지원자_수정")

    # 검토가 필요한 항목이 어느 열에 대한 이야기인지 미리 알아 둔다.
    끝낸검토 = store.review_done(지원자_ID)
    검토항목 = review.items(rec.검토_사유, 끝낸검토)
    검토열 = review.columns_needing_review(rec.검토_사유, 끝낸검토)

    def 입력칸(항목: str, 값: str, 이름: str) -> str:
        """한 칸. 이름은 값_{i} 처럼 번호를 달아 **한 폼에** 담는다."""
        if 항목 in REGISTRY_FIELDS:
            종류 = NAME_COLUMNS[항목]
            현재 = registry.display(종류, 값) if 값 else ""
            보기 = [""] + [n.표시명 for n in registry.list_all(종류)]
            opts = "".join(
                f"<option value='{html.escape(o)}'{' selected' if o == 현재 else ''}>"
                f"{html.escape(o) or '(빈칸)'}</option>"
                for o in dict.fromkeys(보기)
            )
            return (f"<select form='saveform' name='{이름}' onchange='markDirty(this)'"
                    f" data-orig='{html.escape(현재)}'>{opts}</select>")
        spec = field_spec(항목)
        if spec.입력 == "select":
            opts = "".join(
                f"<option value='{html.escape(o)}'{' selected' if o == 값 else ''}>"
                f"{html.escape(o) or '(빈칸)'}</option>"
                for o in spec.선택지
            )
            return (f"<select form='saveform' name='{이름}' onchange='markDirty(this)'"
                    f" data-orig='{html.escape(값)}'>{opts}</select>")
        도움 = f" placeholder='{html.escape(spec.도움말)}'" if spec.도움말 else ""
        return (f"<input type='text' form='saveform' name='{이름}'"
                f" value='{html.escape(값)}' style='width:100%;max-width:420px'"
                f" data-orig='{html.escape(값)}' oninput='markDirty(this)'{도움}>")

    def 검토배지(c: str) -> str:
        return (" <span class='pill p-검토필요'>검토</span>"
                if c in 검토열 else "")

    이름표 = 라벨(list(table_columns(registry)))
    항목행 = []
    숨은칸 = []
    번호 = 0
    for c in table_columns(registry):
        값 = str(row.get(c, "") or "")
        보기 = html.escape(값) or "<span class='muted'>-</span>"
        if c == "검토_사유" and 검토항목:
            # 확인한 항목은 여기서 빠진다. 원문은 위 검토 카드에 회색으로 남아
            # 있으니 무엇을 봤는지 되짚을 수 있고, 이 줄은 **남은 것만** 말한다.
            보일글 = review.display(rec.검토_사유, 끝낸검토)
            보기 = ("<a href='#검토'>위 검토 카드에서 항목별로 봅니다 →</a>"
                  f"<br><span class='muted'>{html.escape(보일글)}</span>")
        줄표시 = " class='needs'" if c in 검토열 else ""
        # 검토_사유는 위 검토 카드가 관리한다. 여기서 글을 고치면 '확인함'
        # 표시와 짝이 안 맞는다.
        if (not 수정가능 or c in READONLY_FIELDS or c == "검토_사유"
                or c.startswith("1저자_해외논문_")):
            항목행.append(
                f"<tr{줄표시}><th style='width:180px'>{html.escape(이름표[c])}"
                f"{검토배지(c)}</th>"
                f"<td style='white-space:normal;max-width:none'>{보기}</td></tr>"
            )
            continue
        번호 += 1
        원본값 = str(getattr(rec, c, "") or "")
        숨은칸.append(
            f"<input type='hidden' form='saveform' name='항목_{번호}'"
            f" value='{html.escape(c)}'>"
            f"<input type='hidden' form='saveform' name='이전_{번호}'"
            f" value='{html.escape(원본값)}'>"
        )
        항목행.append(
            f"<tr{줄표시}><th style='width:180px'>{html.escape(이름표[c])}"
            f"{검토배지(c)}</th>"
            f"<td style='white-space:normal;max-width:none'>"
            f"{입력칸(c, 원본값, f'값_{번호}')}</td></tr>"
        )

    사용자열 = store.fields()
    사용자값 = store.custom_values(지원자_ID)
    사용자행 = []
    for f in 사용자열:
        이름, 값 = f["이름"], 사용자값.get(f["이름"], "")
        if not 수정가능:
            사용자행.append(
                f"<tr><th style='width:180px'>{html.escape(이름)}</th>"
                f"<td>{html.escape(값) or '<span class=muted>-</span>'}</td></tr>"
            )
            continue
        번호 += 1
        spec = custom_field_spec(f)
        if spec.입력 == "select":
            opts = "".join(
                f"<option value='{html.escape(o)}'{' selected' if o == 값 else ''}>"
                f"{html.escape(o) or '(빈칸)'}</option>" for o in spec.선택지
            )
            칸 = (f"<select form='saveform' name='값_{번호}' onchange='markDirty(this)'"
                 f" data-orig='{html.escape(값)}'>{opts}</select>")
        else:
            칸 = (
                f"<input type='text' form='saveform' name='값_{번호}'"
                f" value='{html.escape(값)}' style='width:100%;max-width:420px'"
                f" data-orig='{html.escape(값)}' oninput='markDirty(this)'"
                f" placeholder='{html.escape(spec.도움말)}'>"
            )
        숨은칸.append(
            f"<input type='hidden' form='saveform' name='항목_{번호}'"
            f" value='{html.escape(이름)}'>"
            f"<input type='hidden' form='saveform' name='이전_{번호}'"
            f" value='{html.escape(값)}'>"
            f"<input type='hidden' form='saveform' name='구분_{번호}' value='추가'>"
        )
        사용자행.append(
            f"<tr><th style='width:180px'>{html.escape(이름)}"
            f"<br><span class='muted'>{html.escape(f['유형'])}</span></th>"
            f"<td>{칸}</td></tr>"
        )

    사용자카드 = (
        f"<div class='card'><h2>추가 항목</h2><table>{''.join(사용자행)}</table></div>"
        if 사용자열 else ""
    )

    # 검토 카드 — 항목마다 '확인함' 을 눌러 하나씩 지운다.
    남은검토 = [x for x in 검토항목 if not x["완료"]]
    본검토 = [x for x in 검토항목 if x["완료"]]

    def 검토줄(x: dict, 완료: bool) -> str:
        가리킴 = " · ".join(이름표.get(c, c) for c in x["열"])
        단추 = ""
        if 수정가능:
            길 = "/candidate/review/undo" if 완료 else "/candidate/review/done"
            단추 = (
                f"<form method='post' action='{길}' style='display:inline'>"
                f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
                f"<input type='hidden' name='사유' value='{html.escape(x['글'])}'>"
                + ("<button class='sec'>되돌리기</button>" if 완료
                   else "<button>확인함</button>")
                + "</form>"
            )
        return (
            f"<tr><td style='white-space:normal'>"
            + ("<span class='muted'>" if 완료 else "<b>")
            + html.escape(x["글"])
            + ("</span>" if 완료 else "</b>")
            + (f"<br><span class='muted'>관련 항목: {html.escape(가리킴)}</span>"
               if 가리킴 else "")
            + f"</td><td class='ctl'>{단추}</td></tr>"
        )

    검토카드 = ""
    if 검토항목:
        줄 = ("".join(검토줄(x, False) for x in 남은검토)
             + "".join(검토줄(x, True) for x in 본검토))
        머리 = (f"검토 필요 <span class='pill p-검토필요'>{len(남은검토)}건</span>"
              if 남은검토 else "검토 완료 <span class='pill p-완료'>전부 확인함</span>")
        안내 = (
            "<p class='muted'>LLM 이 <b>확신하지 못한 것</b>들입니다. 아래 표에서 "
            "해당 항목이 <span class='pill p-검토필요'>검토</span> 로 표시돼 있습니다. "
            "값을 고치거나 그대로 둔 뒤 <b>확인함</b> 을 누르세요. "
            "전부 확인하면 이 지원자는 검토 필요에서 빠집니다.</p>"
            if 남은검토 else
            "<p class='muted'>모두 확인했습니다. 이 지원자는 검토 필요가 아닙니다.</p>"
        )
        검토카드 = (
            f"<div class='card' id='검토'"
            + (" style='border-color:#fcd34d;background:#fffbeb'" if 남은검토 else "")
            + f"><h2>{머리}</h2>{안내}"
            "<table><tr><th>사유</th><th style='width:110px'></th></tr>"
            + 줄 + "</table></div>"
        )


    # 논문·특허 목록 — 표의 '수' 열이 무엇을 세었는지 눈으로 볼 수 있어야 한다.
    논문보기 = rec.papers_view(registry)
    주저자배지 = "<span class='pill p-완료'>주저자</span>"
    공저자배지 = "<span class='muted'>공저자</span>"
    논문행 = "".join(
        f"<tr><td>{주저자배지 if v['주저자'] else 공저자배지}</td>"
        f"<td>{html.escape(v['유형'])}</td>"
        f"<td title='{html.escape(v.get('제목') or v['표시명'])}'>"
        f"{html.escape(v.get('제목') or '')}"
        + (f"<br><span class='muted'>{html.escape(v['표시명'])}</span>"
           if v.get('제목') else html.escape(v['표시명']))
        + f"</td><td>{html.escape(v['연도'])}</td>"
        f"<td>{html.escape(v['국내해외'])}</td>"
        f"<td>{html.escape(v['등급'])}</td></tr>"
        for v in 논문보기
    )
    특허행 = "".join(
        f"<tr><td>{html.escape(pt.상태)}</td>"
        f"<td title='{html.escape(pt.제목)}'>{html.escape(pt.제목) or '-'}</td>"
        f"<td>{html.escape(pt.연도)}</td><td>{html.escape(pt.번호)}</td></tr>"
        for pt in rec.특허
    )
    센것 = {**rec.논문_수(registry), **rec.특허_수()}
    실적카드 = ""
    if 논문보기 or rec.특허:
        실적카드 = (
            "<div class='card'><h2>연구 실적 <span class='muted'>"
            f"저널 {센것['저널_수']}편(주저자 {센것['저널_주저자_수']}) · "
            f"학회 {센것['학회_수']}편(주저자 {센것['학회_주저자_수']}) · "
            f"특허 등록 {센것['특허_등록_수']} / 출원 {센것['특허_출원_수']}"
            "</span></h2>"
            + ("<div class='scroll'><table data-name='논문'>"
               "<tr><th style='width:80px'>저자</th><th style='width:60px'>유형</th>"
               "<th>제목 / 제출처</th><th style='width:60px'>연도</th>"
               "<th style='width:70px'>국내해외</th><th style='width:80px'>등급</th></tr>"
               + 논문행 + "</table></div>" if 논문보기 else "")
            + ("<h2 style='margin-top:14px'>특허</h2><div class='scroll'>"
               "<table data-name='특허'><tr><th style='width:70px'>상태</th><th>제목</th>"
               "<th style='width:60px'>연도</th><th>번호</th></tr>"
               + 특허행 + "</table></div>" if rec.특허 else "")
            + "<p class='muted'>표의 <b>저널_수 · 학회_수 · 특허_등록_수</b> 열은 "
              "여기 있는 것을 셉니다. 학회·저널 구분과 등급은 "
              "<a href='/names?kind=" + urllib.parse.quote("학회·저널")
            + "'>명칭 관리</a>에서 판별한 값을 씁니다.</p></div>"
        )

    년도 = store.year_of(지원자_ID)
    if 수정가능:
        번호 += 1
        숨은칸.append(
            f"<input type='hidden' form='saveform' name='항목_{번호}' value='등록년도'>"
            f"<input type='hidden' form='saveform' name='이전_{번호}'"
            f" value='{html.escape(년도)}'>"
            f"<input type='hidden' form='saveform' name='구분_{번호}' value='년도'>"
        )
        년도폼 = (
            f"<input type='text' form='saveform' name='값_{번호}'"
            f" value='{html.escape(년도)}' style='width:90px' placeholder='YYYY'"
            f" data-orig='{html.escape(년도)}' oninput='markDirty(this)'>"
            "<span class='muted'> 아래 <b>고친 내용 저장</b> 으로 함께 저장됩니다.</span>"
        )
    else:
        년도폼 = html.escape(년도)

    저장바 = (
        "<form method='post' action='/candidate/save' id='saveform' class='mergebar'>"
        f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
        f"<input type='hidden' name='끝' value='{번호}'>"
        + "".join(숨은칸)
        + "<button type='submit'>고친 내용 저장</button>"
        "<span class='muted'>여러 칸을 고치고 <b>한 번만</b> 누르세요. "
        "고친 칸은 노랗게 표시됩니다.</span></form>"
        if 수정가능 else ""
    )

    진행 = recruit.get(지원자_ID)
    if 진행.시작함:
        채용줄 = (
            f"<span class='pill p-처리중'>채용 중</span> "
            f"<span class='muted'>{html.escape(진행.채용시작일시)} 시작</span>"
            + ("<form method='post' action='/candidates/stop' style='display:inline'>"
               f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
               "<button class='sec' style='margin-left:8px'>채용 현황에서 내리기</button>"
               "</form>" if can(me, "채용현황_수정") else "")
        )
    else:
        채용줄 = (
            "<span class='pill p-대기중'>인재 Pool</span> "
            "<span class='muted'>아직 채용 절차를 시작하지 않았습니다</span>"
            + ("<form method='post' action='/candidates/start' style='display:inline'>"
               f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
               "<button style='margin-left:8px'>채용 시작</button></form>"
               if can(me, "채용현황_수정") else "")
        )

    관리 = (
        f"<tr><th style='width:170px'>채용</th><td>{채용줄}</td></tr>"
        f"<tr><th>등록 년도</th><td>{년도폼}</td></tr>"
        f"<tr><th>원본 파일명</th><td>{html.escape(meta.get('원본_파일명') or '-')}</td></tr>"
        f"<tr><th>등록 일시</th><td>{html.escape(meta.get('등록일시') or '-')}</td></tr>"
        f"<tr><th>원본 파일 보관</th><td>{'예' if meta.get('원본보유') else '아니오'}</td></tr>"
    )
    중복 = store.duplicate_note(지원자_ID)
    if 중복:
        관리 += f"<tr><th>중복 후보</th><td class='flag' style='white-space:normal'>{html.escape(중복)}</td></tr>"

    매칭 = store.matches(지원자_ID)
    매칭카드 = ""
    # 매칭 결과에는 회사 연구 과제 전체가 들어 있다. 현업에게는 보이면 안 된다.
    if settings.projects_json and can(me, "과제매칭_조회"):
        보여줄수 = max(1, settings.match_show)

        def 매칭줄(m: dict) -> str:
            유사 = ("<br><span class='muted'>임베딩 유사도 "
                  f"{m['유사도']:.2f}</span>" if m.get("유사도") is not None else "")
            점수칸 = (f"<span class='pill {_점수색(m['점수'])}'>{m['점수']}점</span>"
                   f"<br><span class='muted'>{_등급이름(m)}</span>"
                   if m["평가됨"] else
                   "<span class='pill p-실패'>미평가</span>")
            return (
                f"<tr><td>{m['순위']}</td><td><b>{html.escape(m['과제명'])}</b>"
                f"<br><span class='muted'>{html.escape(m['과제키'])}</span>{유사}</td>"
                f"<td>{점수칸}</td>"
                f"<td class='w-xl' title='{html.escape(m['사유'])}'>"
                f"{html.escape(m['사유'])}"
                + ("<br><span class='muted'>근거: "
                   + html.escape(" · ".join(m["근거"])) + "</span>" if m["근거"] else "")
                + "</td></tr>"
            )

        if 매칭:
            위 = "".join(매칭줄(m) for m in 매칭[:보여줄수])
            나머지 = "".join(매칭줄(m) for m in 매칭[보여줄수:])
            더보기 = (
                f"<details><summary class='muted' style='cursor:pointer;padding:6px 0'>"
                f"나머지 {len(매칭) - 보여줄수}개 과제도 보기</summary>"
                "<div class='scroll'><table>"
                "<tr><th style='width:44px'>순위</th><th>과제</th>"
                "<th style='width:92px'>점수</th><th>판단 사유</th></tr>"
                f"{나머지}</table></div></details>"
                if len(매칭) > 보여줄수 else ""
            )
            미평가 = sum(1 for m in 매칭 if not m["평가됨"])
            안내 = (
                f"<p class='muted'>{html.escape(매칭[0]['판단일시'])} 기준 · "
                f"과제 <b>{len(매칭)}개 전부</b>와 비교했습니다"
                + (f" · <span class='flag'>{미평가}개는 모델이 답하지 않아 미평가</span>"
                   if 미평가 else "")
                + "</p>"
            )
        else:
            위, 더보기, 안내 = ("<tr><td colspan='4' class='muted'>"
                             "아직 맞춰보지 않았습니다.</td></tr>", "", "")
        다시 = (
            "<form method='post' action='/match/one' style='display:inline'>"
            f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
            "<button type='submit'>과제와 맞춰보기</button></form>"
            if can(me, "지원자_등록") else ""
        )
        매칭카드 = (
            "<div class='card'><h2>연구 과제 매칭</h2>" + 안내
            + f"<p>{다시} <a class='btn sec' href='/match'>과제 매칭 화면</a></p>"
            "<div class='scroll'><table data-name='과제 매칭'>"
            "<tr><th style='width:44px'>순위</th><th>과제</th>"
            "<th style='width:92px'>점수</th><th>판단 사유</th></tr>"
            + 위 + "</table></div>" + 더보기
            + "<p class='muted'>점수는 아래 눈금으로 매깁니다. "
            "<b>LLM 의 판단이지 측정값이 아닙니다</b> — 순위를 참고하고 "
            "사유와 근거를 사람이 확인하세요.</p>"
            f"<pre class='rubric'>{html.escape(SCORE_RUBRIC)}</pre></div>"
        )

    메일기록 = mailing.history(지원자_ID)
    메일행 = "".join(
        f"<tr><td>{html.escape(m['보낸일시'])}</td>"
        f"<td>{html.escape(m['템플릿이름'])}</td>"
        f"<td>{html.escape(m['받는사람'])}</td>"
        f"<td>{html.escape(m['상태'])}</td>"
        f"<td class='muted' title='{html.escape(m['오류'] or '')}'>"
        f"{html.escape(m['오류'] or '')}</td></tr>"
        for m in 메일기록
    )
    # 이 한 사람에게 바로 보내기. 예전에는 여기서 **이력만** 볼 수 있어서,
    # 상세를 보다가 메일을 보내려면 인재 Pool 로 돌아가 그 사람을 다시 찾아
    # 체크해야 했다. 보내기 화면은 여러 명을 받게 돼 있으니 한 명만 실어 보낸다.
    보내기단추 = ""
    if can(me, "메일_발송"):
        막힘 = mailing.rejected(지원자_ID)
        받는주소 = (row.get("이메일") or "").split(MULTI_SEP)[0].strip()
        if 막힘:
            보내기단추 = ("<p class='muted'>탈락 메일을 보낸 지원자라 "
                      "더는 보낼 수 없습니다.</p>")
        elif not 받는주소:
            보내기단추 = ("<p class='muted'>이메일 주소가 없어 보낼 수 없습니다. "
                      "아래 표에서 <b>이메일</b> 을 채우세요.</p>")
        else:
            뒤로 = f"/candidate?id={urllib.parse.quote(지원자_ID)}"
            보내기단추 = (
                f"<form method='post' action='/mail/compose'>"
                f"<input type='hidden' name='ids' value='{html.escape(지원자_ID)}'>"
                f"<input type='hidden' name='back' value='{html.escape(뒤로)}'>"
                f"<button type='submit'>이 지원자에게 메일 보내기</button> "
                f"<span class='muted'>{html.escape(받는주소)} 로 나갑니다. "
                f"다음 화면에서 템플릿을 고르고 내용을 확인합니다.</span></form>"
            )

    메일카드 = (
        "<div class='card'><h2>메일</h2>"
        + ("<div class='warn'>탈락 메일을 보낸 지원자입니다. "
           "이후 어떤 메일도 보낼 수 없습니다.</div>"
           if mailing.rejected(지원자_ID) else "")
        + 보내기단추
        + ("<div class='scroll'><table data-name='보낸 메일'>"
           "<tr><th>보낸 일시</th><th>템플릿</th><th>받는 주소</th>"
           "<th>상태</th><th>메모</th></tr>"
           + 메일행 + "</table></div>"
           if 메일기록 else "<p class='muted'>아직 보낸 메일이 없습니다.</p>")
        + "</div>"
    ) if (메일기록 or 보내기단추) else ""

    이력 = audit.for_target("지원자", 지원자_ID)
    이력행 = "".join(
        f"<tr><td>{html.escape(e.일시)}</td><td>{html.escape(e.사용자)}</td>"
        f"<td title='{html.escape(e.summary())}'>{html.escape(e.summary())}</td></tr>"
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
        "<button type='submit' class='danger ghost'>지원자 삭제</button></form>"
        if can(me, "지원자_삭제") else ""
    )
    오류 = _알림(err=error)

    첨부목록 = "".join(
        f"<li><a href='/attachment?id={a['id']}'>{html.escape(a['파일명'])}</a>"
        f" <span class='muted'>{html.escape(a['올린일시'])} · {html.escape(a['올린이'] or '-')}</span>"
        + (
            " <form method='post' action='/attachment/delete' style='display:inline'"
            " onsubmit=\"return confirm('첨부파일을 삭제합니다.')\">"
            f"<input type='hidden' name='id' value='{a['id']}'>"
            f"<input type='hidden' name='cid' value='{html.escape(지원자_ID)}'>"
            "<button class='danger ghost'>삭제</button></form>"
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

    알림 = _알림(msg=msg)
    return _page(
        f"지원자 {rec.한글_이름 or rec.지원자_ID}",
        f"""{알림}{오류}
        <div class='card'>
          <h2>{html.escape(rec.한글_이름 or '(이름 미상)')}
              <span class='muted'>{html.escape(rec.지원자_ID)}</span></h2>
          <p class='bar'><a class='btn sec' href='/'>← 목록으로</a>
             {원본버튼}{재분석}<span style='flex:1'></span>{삭제}</p>
          {'<p class=muted>수정 권한이 없어 읽기 전용입니다.</p>' if not 수정가능 else ''}
        </div>
        {검토카드}
        <div class='card'><h2>관리 정보</h2><table>{관리}</table></div>
        <div class='card' id='추출결과'><h2>추출 결과</h2>
          {저장바}
          <table>{''.join(항목행)}</table></div>
        {사용자카드}
        {실적카드}
        {매칭카드}
        {첨부카드}
        {메일카드}
        <div class='card'><h2>변경 이력</h2><div class='scroll'>
          <table><tr><th>일시</th><th>사용자</th><th>내용</th></tr>{이력행}</table>
        </div></div>""",
        me=me,
    )


def _names_page(종류: str, me: User | None = None,
                error: str = "", msg: str = "", 안본것만: bool = False) -> bytes:
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
    전부 = registry.list_all(종류)           # 표시명 오름차순이 기본
    등급목록 = registry.tier_names()
    등급종류 = 종류 in GRADED_KINDS

    # 아직 사람이 안 본 줄을 **위로** 올린다. 할 일이 화면 아래로 밀려나면
    # 스무 줄만 넘어도 못 보고 지나친다. (그 안에서는 원래 순서 그대로)
    안본것 = [i for i in 전부 if not i.확인]
    items = 안본것 if 안본것만 else (안본것 + [i for i in 전부 if i.확인])

    무리: dict[str, list] = {}
    for i in 전부:
        무리.setdefault(i.표시명, []).append(i)

    탭 = " ".join(
        f"<a class='btn {'' if k == 종류 else 'sec'}' href='/names?kind={urllib.parse.quote(k)}'>"
        f"{k}"
        + (f" <span class='pill p-안본것'>{registry.unconfirmed_count(k)}</span>"
           if registry.unconfirmed_count(k) else "")
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
    아직안내 = "아직 사람이 안 본 줄입니다 (LLM 이 넣어 둔 그대로)"

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
        # 확인칸 — 이 줄을 **사람이 봤는가**. 안 본 줄은 LLM 이 넣어 둔 그대로다.
        본때 = (f"{i.확인일시} {i.확인자}".strip() if i.확인 else "")
        확인칸 = (
            f"<td class='ctl' title='{html.escape(본때) or 아직안내}'>"
            f"<label><input type='checkbox' form='saveform' name='확인_{i.id}'"
            f"{' checked' if i.확인 else ''} data-orig='{'y' if i.확인 else ''}'"
            f" onchange='markDirty(this)'> "
            + (f"<span class='muted'>{html.escape(i.확인일시)}</span>" if i.확인
               else "<b class='flag'>확인</b>")
            + "</label></td>"
        )
        rows.append(
            f"<tr class='{'' if i.확인 else 'needs'}'>"
            f"{확인칸}"
            f"<td title='{html.escape(i.원표기)}'>{html.escape(i.원표기)}{미분류표시}</td>"
            f"<td>{i.발견횟수}</td>"
            f"<td class='ctl'>"
            f"<input type='hidden' form='saveform' name='id' value='{i.id}'>"
            f"<input type='text' form='saveform' name='표시명_{i.id}' list='이름목록'"
            f" value='{html.escape(i.표시명)}' style='width:220px'"
            f" data-orig='{html.escape(i.표시명)}' oninput='markDirty(this)'></td>"
            f"<td>{형제칸}</td>"
            f"{등급칸}"
            f"<td><form method='post' action='/names/forget'"
            f" onsubmit=\"return window.confirm('이 표기를 사전에서 지웁니다. "
            f"다시 CV 에 나오면 새로 등록됩니다.')\">"
            f"<input type='hidden' name='kind' value='{html.escape(종류)}'>"
            f"<input type='hidden' name='id' value='{i.id}'>"
            f"<button class='danger'>지움</button></form></td></tr>"
        )

    저장바 = (
        f"<form method='post' action='/names/save' id='saveform' class='mergebar'>"
        f"<input type='hidden' name='kind' value='{html.escape(종류)}'>"
        f"<input type='hidden' name='todo' value='{'1' if 안본것만 else ''}'>"
        f"<button type='submit'>고친 내용 저장</button>"
        f"<span class='muted'>여러 줄을 고친 뒤 <b>한 번만</b> 누르세요. "
        f"고친 칸은 노랗게 표시됩니다. <b>고친 줄은 저절로 확인 표시</b>가 됩니다."
        f"</span></form>"
        if items else ""
    )

    등급머리 = (
        "<th class='ctl'>학회/저널</th><th class='ctl'>등급</th>"
        "<th class='ctl'>국내/해외</th><th class='ctl'>Impact Factor</th>"
        if 등급종류 else ""
    )
    표 = (
        "<table><tr><th class='ctl w-sm' title='사람이 보고 맞다고 한 줄'>확인</th>"
        "<th>CV 에 적힌 표기</th><th style='width:56px'>발견</th>"
        f"<th class='ctl'>표에 보일 이름</th><th>같은 이름으로 묶인 표기</th>"
        f"{등급머리}<th></th></tr>{''.join(rows)}</table>"
        if rows
        else ("<p class='muted'>안 본 항목이 없습니다. 전부 확인했습니다.</p>"
              if 안본것만 and 전부 else
              "<p class='muted'>아직 등록된 항목이 없습니다. "
              "CV를 업로드하면 자동으로 등록됩니다.</p>")
    )

    # 안 본 것만 보기 — 표기가 수백 줄이 되면 이게 유일하게 쓸 만한 길이 된다
    주소 = f"/names?kind={urllib.parse.quote(종류)}"
    거르개 = (
        f"<a class='btn {'sec' if 안본것만 else ''}' href='{주소}'>전체 {len(전부)}</a> "
        f"<a class='btn {'' if 안본것만 else 'sec'}' href='{주소}&todo=1'>"
        f"아직 안 본 것 {len(안본것)}</a>"
        if 전부 else ""
    )
    알림 = _알림(msg=msg)
    오류 = _알림(err=error)
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
        잘못 묶였으면 그 줄의 이름만 다시 고치면 됩니다.{분류설명}</p>
        <p class='muted'>표기는 CV 에서 발견하는 대로 <b>자동으로</b> 등록되고,
        등급·국내해외는 LLM 이 짐작한 값입니다. 그래서 각 줄에
        <b>확인</b> 칸이 있습니다 — 사람이 보고 맞다고 한 줄은 체크가 켜지고,
        <span class='pill p-안본것'>아직 안 본 줄</span>은 노랗게 남습니다.
        값을 고쳐서 저장하면 그 줄은 저절로 확인 처리됩니다.</p></div>
        {등급열}
        <div class='card'><h2>{html.escape(종류)} <span class='muted'>표기 {len(전부)}개 ·
        이름 {len(무리)}개</span></h2>
        <p>{거르개}</p>
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
        f"<td title='{html.escape(t.제목)}'>{html.escape(t.제목)}</td>"
        f"<td class='muted'>{html.escape(t.참조)}</td>"
        f"<td class='muted'>{len(mailing.attachments(t.id)) or ''}</td>"
        f"<td class='muted'>{html.escape(t.수정일시)}</td>"
        f"<td><a class='btn sec' href='/mail/test?id={t.id}'>확인·시험 발송</a></td></tr>"
        for t in templates
    ) or "<tr><td colspan='7' class='muted'>아직 만든 템플릿이 없습니다.</td></tr>"

    알림 = _알림(msg=msg)
    오류 = _알림(err=error)
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


#: DB 열이 아니라 **여기서 만들어 내는** 자리표시자. 뭘로 채워지는지 화면에 적는다.
#: 이걸 안 적어 두면 "이름 열이 없는데 {{이름}} 은 뭐냐" 는 질문이 계속 나온다.
MAIL_VAR_NOTES = {
    "이름": "한글_이름, 비어 있으면 영문_이름",
    "부서": "채용 현황에서 배정한 부서",
    "과제": "채용 현황에서 배정한 과제",
    "최종상태": "단계 상태에서 계산 (예: 기술 면접 합격)",
}


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

    # 표 편집 도구 — 커서가 표 안에 있을 때만 뜬다.
    #
    # 예전에는 표를 **넣기만** 하고 그 뒤로는 손댈 수가 없었다. 6×6 보다 크게
    # 만들 수도, 열 너비를 잡을 수도, 테두리를 바꿀 수도 없어서 결국 표를 지우고
    # 다시 넣는 수밖에 없었다.
    _표도구 = (
        "<span class='rt-lbl'>표</span>"
        "<button type='button' onclick='rtRow(-1)' title='커서가 있는 줄 위에 넣기'"
        ">행 ↑</button>"
        "<button type='button' onclick='rtRow(1)' title='커서가 있는 줄 아래에 넣기'"
        ">행 ↓</button>"
        "<button type='button' onclick='rtRowDel()' class='sec' title='이 줄 지우기'"
        ">행 −</button>"
        "<span class='rt-sep'></span>"
        "<button type='button' onclick='rtCol(-1)' title='커서가 있는 칸 왼쪽에 넣기'"
        ">열 ←</button>"
        "<button type='button' onclick='rtCol(1)' title='커서가 있는 칸 오른쪽에 넣기'"
        ">열 →</button>"
        "<button type='button' onclick='rtColDel()' class='sec' title='이 열 지우기'"
        ">열 −</button>"
        "<span class='rt-sep'></span>"
        # 표 자체의 너비. 이게 100% 로 박혀 있으면 열 너비를 아무리 고쳐도
        # 정해진 폭을 나눠 갖는 것뿐이라, 열 하나만 넓히는 게 불가능했다.
        "<label class='rt-lbl' title='표 전체의 너비'>표"
        "<select id='rt-tblw' onchange='rtTableWidth()'>"
        "<option value='100%'>창에 맞춤 (100%)</option>"
        "<option value='fit'>열 너비에 맞춤</option>"
        "<option value='auto'>내용에 맞춤</option>"
        "<option value='px'>표 폭 고정 (px)</option>"
        "</select>"
        "<input type='number' id='rt-tblpx' min='80' max='2000' step='10'"
        " style='width:70px;display:none' placeholder='px'"
        " oninput='rtTableWidth()'></label>"
        "<span class='rt-sep'></span>"
        "<label class='rt-lbl' title='커서가 있는 열의 너비. 비우면 자동."
        " 경계선을 끌어도 됩니다'>열"
        "<input type='number' id='rt-colw' min='1' max='2000' step='1'"
        " style='width:66px' oninput='rtColWidth()'>"
        "<select id='rt-colu' onchange='rtColWidth()'>"
        "<option value='px'>px</option><option value='%'>%</option>"
        "</select></label>"
        "<span class='rt-sep'></span>"
        "<label class='rt-lbl' title='표 전체의 테두리'>테두리"
        "<select id='rt-border' onchange='rtBorder(this.value)'>"
        "<option value='1px solid #999'>실선 (얇게)</option>"
        "<option value='2px solid #333'>실선 (굵게)</option>"
        "<option value='1px solid #d1d5db'>연한 회색</option>"
        "<option value='none'>없음</option>"
        "</select></label>"
        "<label class='rt-lbl' title='첫 줄을 머리글처럼 (굵게 + 회색 배경)'>"
        "<input type='checkbox' id='rt-head' onchange='rtHeadRow(this.checked)'>"
        "머리글 줄</label>"
        "<span style='flex:1'></span>"
        "<button type='button' onclick='rtTableDel()' class='danger'"
        " title='표를 통째로 지웁니다'>표 지우기</button>"
    )

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

    알림 = _알림(msg=msg)
    오류 = _알림(err=error)
    변수JSON = json.dumps([[이름, 항목] for 이름, 항목 in 묶음], ensure_ascii=False)
    설명JSON = json.dumps(MAIL_VAR_NOTES, ensure_ascii=False)
    그림카드 = _mail_image_card(tpl)
    글꼴JSON = json.dumps(글꼴, ensure_ascii=False)
    크기JSON = json.dumps(크기, ensure_ascii=False)

    return _page(
        f"{tpl.이름} 템플릿",
        알림 + 경고
        + "<div class='card'><h2>템플릿 편집</h2>" + 오류
        + "<form method='post' action='/mail/template/save' id='tplform'>"
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
        f"<div class='rt-bar rt-tablebar' id='rttablebar' hidden>{_표도구}</div>"
        f"<div class='rt-body' id='rtbody' contenteditable='true'>{tpl.본문}</div></div>"
        f"<input type='hidden' name='body' id='bodyfield' data-orig=''>"
        + "<p class='muted'>자리표시자는 대부분 <b>표의 열 이름</b> 그대로입니다"
        " (<code>{{한글_이름}}</code> <code>{{박사_학교}}</code>). "
        "DB 에 없는데 쓸 수 있는 것은 아래 넷뿐이고, 보낼 때 이렇게 채워집니다:"
        "<br>"
        + " · ".join(f"<code>{{{{{k}}}}}</code> = {html.escape(v)}"
                     for k, v in MAIL_VAR_NOTES.items())
        + "</p>"
        "<p><label><input type='checkbox' name='reject' value='1'"
        f"{' checked' if tpl.탈락메일 else ''}> <b>탈락 메일</b> — 이걸 받은 지원자에게는"
        " 이후 어떤 메일도 나가지 않습니다</label></p>"
        "<p><button type='submit'>저장</button> "
        f"<a class='btn sec' href='/mail/test?id={tpl.id}'>확인·시험 발송</a> "
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
        "<p class='muted'>본문에 넣는 그림은 <b>그림</b> 단추를 쓰세요(2MB 까지). "
        "큰 파일은 여기에 붙입니다.</p></div>"
        + 그림카드
        + f"<script>window.템플릿ID = {tpl.id};"
        f"window.자리표시자 = {변수JSON};window.자리표시자설명 = {설명JSON};"
f"window.rtFonts = {글꼴JSON};window.rtSizes = {크기JSON};"
f"function rtColorMenuFore(b){{rtColorMenu(b, 'foreColor');}}"
f"function rtColorMenuBack(b){{rtColorMenu(b, 'hiliteColor');}}"
f"{_MAIL_JS}</script>",
        me=me,
    )


#: 본문 그림을 메일에 어떻게 실을지 — 화면에 그대로 적는다.
IMAGE_MODE_NOTE = {
    "본문": "본문에 그림을 박아 보냅니다. 메일 API 나 받는 쪽이 그림을 "
          "어디로 옮기는지 우리가 관여할 수 없습니다.",
    "본문+첨부": "본문에 박고 <b>같은 파일을 첨부로도</b> 보냅니다. 본문 그림이 "
              "나중에 깨져도 받은 사람 손에 파일은 남습니다. (권장)",
    "첨부만": "본문에서 그림을 빼고 첨부로만 보냅니다. 본문이 가볍고 깨질 그림이 "
           "아예 없습니다.",
}


def _mail_image_card(tpl) -> str:
    """본문 그림 — 어떻게 보낼지 고르고, 지금 쓰는 그림을 확인한다.

    본문에 박은 그림이 **시간이 지나 깨지는** 일이 있었다. 우리 DB 에는 원본이
    그대로 있으니, 무엇이 어떤 방식으로 나가는지 눈으로 볼 수 있어야 한다.
    """
    쓰는것 = mailing.used_body_images(tpl.본문)
    쓰는id = {i["id"] for i in 쓰는것}
    안쓰는것 = [i for i in mailing.body_images(tpl.id) if i["id"] not in 쓰는id]

    def 줄(img: dict, 쓴다: bool) -> str:
        있나 = mailing.body_image_bytes(img["id"]) is not None
        상태 = ("<span class='pill p-완료'>원본 있음</span>" if 있나
              else "<span class='pill p-실패'>파일 없음</span>")
        지우기 = (
            "<form method='post' action='/mail/image/delete' style='display:inline'"
            " onsubmit=\"return confirm('이 그림을 지웁니다.')\">"
            f"<input type='hidden' name='id' value='{img['id']}'>"
            f"<input type='hidden' name='template' value='{tpl.id}'>"
            "<button class='danger'>지우기</button></form>" if not 쓴다 else
            "<span class='muted'>본문에서 쓰는 중</span>"
        )
        미리 = (f"<img src='/mail/image?id={img['id']}' alt=''"
              " style='max-height:44px;max-width:80px;vertical-align:middle'>"
              if 있나 else "-")
        return (
            f"<tr><td>{미리}</td><td>{html.escape(img['파일명'])}"
            f"<br><span class='muted'>id {img['id']}</span></td>"
            f"<td>{img['크기'] // 1024}KB</td><td>{상태}</td>"
            f"<td class='muted'>{html.escape(img['올린일시'])}</td>"
            f"<td>{지우기}</td></tr>"
        )

    행 = ("".join(줄(i, True) for i in 쓰는것)
         + "".join(줄(i, False) for i in 안쓰는것)) or (
        "<tr><td colspan='6' class='muted'>본문에 넣은 그림이 없습니다.</td></tr>")

    고르기 = "".join(
        f"<label style='display:block;padding:3px 0'>"
        f"<input type='radio' name='imgmode' form='tplform' value='{html.escape(m)}'"
        f"{' checked' if tpl.그림보내기 == m else ''}> <b>{html.escape(m)}</b> — "
        f"{IMAGE_MODE_NOTE[m]}</label>"
        for m in IMAGE_MODES
    )
    return (
        "<div class='card'><h2>본문 그림</h2>"
        "<p class='muted'>그림 원본은 <b>이 시스템에 파일로</b> 보관됩니다. "
        "본문에는 짧은 참조만 들어가므로, 본문을 아무리 고쳐도 원본은 상하지 "
        "않습니다. 메일에 어떻게 실을지만 고르면 됩니다.</p>"
        f"<div style='margin:8px 0 12px'>{고르기}</div>"
        "<p class='muted'>고른 뒤 위 <b>저장</b> 을 누르세요.</p>"
        "<div class='scroll'><table data-name='본문 그림'>"
        "<tr><th style='width:90px'>미리보기</th><th>파일</th><th>크기</th>"
        "<th>원본</th><th>올린 일시</th><th></th></tr>" + 행 + "</table></div>"
        "<div class='warn' style='margin-top:8px'>메일 프로그램(특히 Outlook)은 "
        "<b>본문에 박은 그림을 막거나 주소로 바꿔</b> 두는 일이 있습니다. "
        "그 주소가 나중에 없어지면 <b>예전에 보낸 메일의 그림이 깨집니다.</b> "
        "우리가 어쩌지 못하는 구간이라, 꼭 봐야 하는 그림이면 "
        "<b>본문+첨부</b> 나 <b>첨부만</b> 으로 두세요 — 첨부는 메일 안에 "
        "남으므로 사라지지 않습니다.</div></div>"
    )


def _mail_targets(ids: list[str], tpl, me: User):
    """(보낼 수 있는 사람, 못 보내는 사람) 을 이유와 함께.

    화면과 실제 발송이 **같은 함수**를 쓴다. 다르면 화면에서 본 것과 나가는
    것이 어긋난다.
    """
    진행맵 = recruit.all()
    보이는과제 = auth.visible_project_ids(me)
    갈사람, 막힌사람 = [], []
    for cid in dict.fromkeys(ids):
        rec = store.get(cid)
        if rec is None:
            continue
        if 보이는과제 is not None and recruit.get(cid).project_id not in 보이는과제:
            continue
        값 = _mail_vars(rec, 진행맵)
        이름 = 값.get("한글_이름") or 값.get("영문_이름") or cid
        받는사람 = (값.get("이메일") or "").split(MULTI_SEP)[0].strip()
        제목, 빈1 = render(tpl.제목, 값)
        본문, 빈2 = render(tpl.본문, 값)
        빈칸 = list(dict.fromkeys(빈1 + 빈2))
        막힘 = mailing.blocked_reason(cid, tpl)
        if not 막힘 and not 받는사람:
            막힘 = "이메일 주소가 없습니다"
        # 빈 자리표시자는 **막지 않는다.** 예전에는 하나라도 비면 못 보냈는데,
        # 그러면 "빈칸을 채워서 보내 주세요" 라는 메일을 정작 빈칸이 있는
        # 사람에게 못 보낸다. 그게 이 기능이 제일 필요한 자리다.
        # 대신 눈에 띄게 알리고, 보내기 전 인원수 확인은 그대로 거친다.
        한줄 = {"cid": cid, "이름": 이름, "받는사람": 받는사람,
              "제목": 제목, "본문": 본문, "막힘": 막힘, "빈칸": 빈칸}
        (막힌사람 if 막힘 else 갈사람).append(한줄)
    return 갈사람, 막힌사람


def _mail_compose_page(ids: list[str], tid: int, me: User, 뒤로: str = "/",
                       error: str = "") -> bytes:
    """고른 사람에게 보낼 템플릿을 고르고, 나갈 내용을 확인하고, 보낸다.

    예전에는 `메일` 탭에서 템플릿을 열면 **지원자 전원**이 나왔다. 서류 합격
    안내를 보내려는데 누가 서류 합격인지 그 화면에서는 알 수가 없었다.
    이제 반대다 — 인재 Pool·채용 현황에서 **거른 뒤 고른 사람**을 데리고 온다.
    """
    templates = mailing.templates()
    고른수 = len(dict.fromkeys(ids))
    돌아가기 = f"<a class='btn sec' href='{html.escape(뒤로)}'>돌아가기</a>"
    오류 = _알림(err=error)

    if not ids:
        return _page("메일 보내기", 오류 + "<div class='card'><h2>고른 사람이 없습니다</h2>"
                     "<p class='muted'>표에서 보낼 사람을 체크한 뒤 다시 누르세요.</p>"
                     f"<p>{돌아가기}</p></div>", me=me)

    숨김 = "".join(
        f"<input type='hidden' name='ids' value='{html.escape(c)}'>"
        for c in dict.fromkeys(ids)
    ) + f"<input type='hidden' name='back' value='{html.escape(뒤로)}'>"

    tpl = mailing.template(tid) if tid else None
    if tpl is None:
        고르기 = "".join(
            f"<label style='display:block;padding:4px 0'>"
            f"<input type='radio' name='template' value='{t.id}'"
            f"{' checked' if i == 0 else ''}> <b>{html.escape(t.이름)}</b>"
            + (" <span class='pill p-미분류'>탈락 메일</span>" if t.탈락메일 else "")
            + f" <span class='muted'>{html.escape(t.제목)}</span></label>"
            for i, t in enumerate(templates)
        ) or "<p class='muted'>만들어 둔 템플릿이 없습니다.</p>"
        return _page(
            "메일 보내기",
            오류
            + f"<div class='card'><h2>고른 사람 {고른수}명</h2>"
            "<p class='muted'>보낼 템플릿을 고르세요. 다음 화면에서 "
            "<b>누구에게 무엇이 나가는지 하나씩 확인</b>한 뒤에 보냅니다.</p>"
            "<form method='post' action='/mail/compose'>" + 숨김 + 고르기
            + "<p><button type='submit'>다음 — 나갈 내용 확인</button> "
            + 돌아가기 + "</p></form>"
            + ("<p class='muted'><a href='/mail'>메일 탭</a>에서 템플릿을 "
               "만들고 고칠 수 있습니다.</p>")
            + "</div>",
            me=me,
        )

    갈사람, 막힌사람 = _mail_targets(ids, tpl, me)
    미리 = lambda b: (html_to_text(b) if tpl.html else b)
    def 빈칸칸(x: dict) -> str:
        빈 = x.get("빈칸") or []
        if not 빈:
            return "<td class='muted'>-</td>"
        글 = ", ".join(빈)
        return (f"<td class='flag' title='{html.escape(글)}'>"
                f"{len(빈)}개 <span class='muted'>{html.escape(글)}</span></td>")

    갈줄 = "".join(
        f"<tr><td>{html.escape(x['이름'])}</td><td>{html.escape(x['받는사람'])}</td>"
        f"<td title='{html.escape(x['제목'])}'>{html.escape(x['제목'])}</td>"
        f"<td class='muted' title='{html.escape(미리(x['본문'])[:400])}'>"
        f"{html.escape(미리(x['본문'])[:120])}"
        f"{'…' if len(미리(x['본문'])) > 120 else ''}</td>"
        + 빈칸칸(x) + "</tr>"
        for x in 갈사람
    ) or "<tr><td colspan='5' class='muted'>보낼 수 있는 사람이 없습니다.</td></tr>"
    막힌줄 = "".join(
        f"<tr class='dup'><td>{html.escape(x['이름'])}</td>"
        f"<td class='flag' title='{html.escape(x['막힘'])}'>"
        f"{html.escape(x['막힘'])}</td></tr>"
        for x in 막힌사람
    )

    첨부 = mailing.attachments(tpl.id)
    딸림 = []
    if tpl.cc():
        딸림.append("참조 <b>" + html.escape(", ".join(tpl.cc())) + "</b>")
    if 첨부:
        딸림.append("첨부 <b>"
                  + html.escape(", ".join(a["파일명"] for a in 첨부)) + "</b>")
    if mailing.used_body_images(tpl.본문):
        딸림.append(f"본문 그림 <b>{tpl.그림보내기}</b>")
    딸림칸 = f"<p class='muted'>{' · '.join(딸림)}</p>" if 딸림 else ""

    본문미리 = 갈사람[0]["본문"] if 갈사람 else ""
    미리보기 = (
        "<div class='card'><h2>본문 미리보기 "
        f"<span class='muted'>{html.escape(갈사람[0]['이름'])} 기준</span></h2>"
        f"<div class='mailbody'>{본문미리}</div></div>"
        if 본문미리 and tpl.html else ""
    )
    연습 = (
        "<div class='warn'><b>연습 모드 (MAIL_DRY_RUN=1)</b> — 실제로 나가지 않고 "
        "기록만 남습니다. 기록이 남으면 '이미 보냄' 으로 처리되니 주의하세요.</div>"
        if settings.mail_dry_run else ""
    )
    탈락표시 = (
        "<div class='warn'><b>이 템플릿은 탈락 메일입니다.</b> 보내고 나면 그 지원자에게는 "
        "이후 어떤 메일도 보낼 수 없습니다.</div>" if tpl.탈락메일 else ""
    )
    빠진것 = mailapi.missing_settings()
    설정경고 = (
        f"<div class='warn'>메일 설정이 비어 있어 보낼 수 없습니다: "
        f"<b>{html.escape(', '.join(빠진것))}</b></div>"
        if 빠진것 and not settings.mail_dry_run else ""
    )

    # 빈 자리표시자는 막지 않는다 — 오히려 그런 사람에게 보내려고 쓰는 기능이다.
    # 다만 실수로 흘려보내면 안 되니 보내기 칸 바로 위에서 크게 알린다.
    빈사람 = [x for x in 갈사람 if x.get("빈칸")]
    빈경고 = ""
    if 빈사람:
        모인빈칸 = dict.fromkeys(k for x in 빈사람 for k in x["빈칸"])
        빈경고 = (
            f"<div class='warn'><b>{len(빈사람)}명</b>은 값이 없는 자리표시자가 "
            f"있어 그 자리가 <b>빈 채로</b> 나갑니다 "
            f"({html.escape(', '.join(모인빈칸))}). "
            "빈칸을 채워 달라고 요청하는 메일이면 이대로 보내면 되고, "
            "그게 아니면 위 표의 <b>빈 항목</b> 칸을 확인하세요.</div>"
        )

    보내기 = ""
    if can(me, "메일_발송") and 갈사람:
        보내기 = (
            빈경고
            + "<div class='card' style='border-color:#fca5a5'>"
            "<h2>보내기 전 마지막 확인</h2>"
            "<p><b>이 작업은 되돌릴 수 없습니다.</b> 아래 표에 있는 "
            f"<b>{len(갈사람)}명</b>에게 지금 메일이 나갑니다.</p>"
            # window.confirm 을 그대로 부르면 안 된다. 이 폼 안에 name='confirm'
            # 입력칸이 있어서, 인라인 핸들러에서는 그 입력칸이 함수를 가린다.
            "<form method='post' action='/mail/send'"
            " onsubmit=\"return window.confirm('정말 보냅니다. 되돌릴 수 없습니다.')\">"
            + 숨김
            + f"<input type='hidden' name='template' value='{tpl.id}'>"
            "<p>보낼 인원수 <b>" + str(len(갈사람)) + "</b> 을 그대로 쳐 넣으세요: "
            "<input type='text' name='confirm' style='width:80px'"
            " placeholder='숫자' autocomplete='off' required> "
            "<button type='submit'>메일 보내기</button></p>"
            "<p class='muted'>숫자를 직접 치게 하는 이유는 하나입니다 — "
            "확인창은 안 읽고 누르지만 숫자는 화면을 봐야 칠 수 있습니다.</p>"
            "</form></div>"
        )
    elif not 갈사람:
        보내기 = ("<div class='card'><p class='muted'>보낼 수 있는 사람이 "
                "없습니다. 아래 이유를 확인하세요.</p></div>")

    return _page(
        "메일 보내기",
        오류 + 연습 + 설정경고 + 탈락표시
        + f"<div class='card'><h2>{html.escape(tpl.이름)} "
        f"<span class='muted'>{html.escape(tpl.제목)}</span></h2>" + 딸림칸
        + f"<p class='muted'>고른 {고른수}명 중 <b>{len(갈사람)}명</b>에게 나가고 "
        f"<b>{len(막힌사람)}명</b>은 못 나갑니다.</p>"
        "<p><form method='post' action='/mail/compose' style='display:inline'>"
        + 숨김 + "<button class='sec'>다른 템플릿 고르기</button></form> "
        + 돌아가기
        + f" <a class='btn sec' href='/mail/template?id={tpl.id}'>템플릿 고치기</a></p>"
        "</div>"
        + f"<div class='card'><h2>나갈 사람 {len(갈사람)}명</h2><div class='scroll'>"
        "<table data-name='나갈 사람'><tr><th>지원자</th><th>받는 주소</th>"
        "<th>제목</th><th>본문 미리보기</th>"
        "<th title='값이 없어 빈 채로 나가는 자리표시자'>빈 항목</th></tr>"
        + 갈줄 + "</table></div></div>"
        + (f"<div class='card'><h2>못 나가는 사람 {len(막힌사람)}명</h2>"
           "<div class='scroll'><table data-name='못 나가는 사람'>"
           "<tr><th>지원자</th><th>이유</th></tr>" + 막힌줄 + "</table></div></div>"
           if 막힌사람 else "")
        + 미리보기 + 보내기,
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


def _mail_test_page(tid: int, me: User, error: str = "", msg: str = "",
                    peek: bool = False) -> bytes:
    """메일 탭의 템플릿 확인 화면 — **시험 발송까지만** 한다.

    예전에는 여기서 지원자 전원을 늘어놓고 골라 보냈다. 서류 합격 안내를
    보내려는데 이 화면에서는 누가 서류 합격인지 알 수가 없었다. 실제 발송은
    **인재 Pool·채용 현황**에서 거른 뒤 고른 사람에게 하도록 옮겼다.
    """
    tpl = mailing.template(tid)
    if tpl is None:
        return _page("없음", "<div class='card'>템플릿을 찾을 수 없습니다.</div>", me=me)

    첨부 = mailing.attachments(tpl.id)
    딸림 = []
    if tpl.cc():
        딸림.append("참조 <b>" + html.escape(", ".join(tpl.cc())) + "</b>")
    if 첨부:
        딸림.append("첨부 <b>"
                  + html.escape(", ".join(a["파일명"] for a in 첨부)) + "</b>")
    if mailing.used_body_images(tpl.본문):
        딸림.append(f"본문 그림 <b>{tpl.그림보내기}</b>")
    딸림칸 = f"<p class='muted'>{' · '.join(딸림)}</p>" if 딸림 else ""

    # 보기용 값으로 한 사람 몫을 채워 본다 (자리표시자가 제대로 도는지 확인)
    진행맵 = recruit.all()
    records = store.list_all()
    값 = _mail_vars(records[0], 진행맵) if records else {}
    값 = {k: (v or f"(예시){k}") for k, v in 값.items()}
    for 변수 in _mail_var_names():
        값.setdefault(변수, f"(예시){변수}")
    제목미리, _ = render(tpl.제목, 값)
    본문미리, 빈칸 = render(tpl.본문, 값)

    알림 = _알림(msg=msg)
    오류 = _알림(err=error)
    연습 = (
        "<div class='warn'><b>연습 모드 (MAIL_DRY_RUN=1)</b> — 실제로 나가지 "
        "않습니다.</div>" if settings.mail_dry_run else ""
    )
    탈락표시 = (
        "<div class='warn'><b>이 템플릿은 탈락 메일입니다.</b> 보내고 나면 그 "
        "지원자에게는 이후 어떤 메일도 보낼 수 없습니다.</div>"
        if tpl.탈락메일 else ""
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
        + f" <a href='/mail/test?id={tpl.id}&peek=1'>보낼 요청 내용 보기</a></p></div>"
        if can(me, "메일_발송") else ""
    )
    점검 = _mail_request_preview(tpl) if peek else ""
    미리 = (
        f"<div class='card'><h2>보기용 미리보기</h2>"
        f"<p class='muted'>제목: {html.escape(제목미리)}</p>"
        + (f"<div class='mailbody'>{본문미리}</div>" if tpl.html
           else f"<pre class='rubric'>{html.escape(본문미리)}</pre>")
        + "<p class='muted'>실제 값이 아니라 <b>(예시)</b> 로 채운 화면입니다. "
        "누구에게 무엇이 나가는지는 보낼 때 하나씩 확인합니다.</p></div>"
    )
    return _page(
        f"{tpl.이름} 확인",
        알림 + 오류 + 연습 + 탈락표시
        + f"<div class='card'><h2>{html.escape(tpl.이름)} "
        f"<span class='muted'>{html.escape(tpl.제목)}</span></h2>" + 딸림칸
        + ("<p class='flag'>값이 빈 자리표시자: "
           + html.escape(", ".join(빈칸)) + "</p>" if 빈칸 else "")
        + "<div class='warn'><b>여기서는 실제 지원자에게 못 보냅니다.</b> "
        "보낼 사람은 <a href='/'>인재 Pool</a> 이나 "
        "<a href='/recruit'>채용 현황</a> 에서 고릅니다 — 거기서는 "
        "<b>누가 서류 합격인지 보면서</b> 고를 수 있습니다. "
        "표에서 체크한 뒤 <b>선택한 사람에게 메일</b> 을 누르세요.</div>"
        + f"<p><a class='btn sec' href='/mail/template?id={tpl.id}'>템플릿 고치기</a> "
        "<a class='btn sec' href='/mail'>목록</a></p></div>"
        + 시험 + 점검 + 미리,
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
        f"<td class='muted' title='{html.escape(r['오류'] or '')}'>"
        f"{html.escape(r['오류'] or '')}</td>"
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
    오류 = _알림(err=error)
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
    """부서 · 과제 편집. 과제는 부서에 속한다.

    예전에는 과제마다 입력칸과 단추가 글머리표 목록으로 늘어서서, 단추가
    줄바꿈되고 무엇이 무엇에 딸린 것인지 알기 어려웠다. 부서 하나를 카드
    하나로 두고, 그 안은 **표**로 정리했다.
    """
    depts = auth.departments()
    projects = auth.projects()

    # 배정된 지원자 수. 지우기 전에 무엇이 딸려 있는지 보이면 실수가 준다.
    배정수: dict[int, int] = {}
    for p in recruit.all().values():
        if p.project_id:
            배정수[p.project_id] = 배정수.get(p.project_id, 0) + 1

    카드 = []
    for d in depts:
        소속 = [p for p in projects if p["부서_id"] == d["id"]]
        사람 = sum(배정수.get(p["id"], 0) for p in 소속)
        줄 = "".join(
            "<tr>"
            f"<td class='ctl'><input type='text' name='name' form='pf{p['id']}'"
            f" value='{html.escape(p['이름'])}' style='width:240px'></td>"
            f"<td class='ctl'><input type='password' name='invite' form='pf{p['id']}'"
            " placeholder='바꿀 때만 입력' style='width:170px'"
            " autocomplete='new-password'>"
            + ("<br><span class='muted'>지금 걸려 있음</span>" if p["초대암호"]
               else "<br><span class='muted'>없음</span>")
            + "</td>"
            f"<td class='w-sm'>{배정수.get(p['id'], 0)}명</td>"
            "<td class='ctl' style='white-space:nowrap'>"
            f"<form method='post' action='/org/project/rename' id='pf{p['id']}'"
            " style='display:inline'>"
            f"<input type='hidden' name='id' value='{p['id']}'>"
            "<button type='submit'>저장</button></form> "
            "<form method='post' action='/org/project/delete' style='display:inline'"
            f" onsubmit=\"return confirm('과제 \\'{html.escape(p['이름'])}\\' 를 "
            f"삭제합니다. 배정된 지원자 {배정수.get(p['id'], 0)}명의 배정도 함께 "
            "풀립니다.')\">"
            f"<input type='hidden' name='id' value='{p['id']}'>"
            "<button class='danger'>삭제</button></form></td></tr>"
            for p in 소속
        ) or ("<tr><td colspan='4' class='muted'>아직 과제가 없습니다. "
              "아래에서 추가하세요.</td></tr>")

        카드.append(
            "<div class='card'>"
            # 폼 안에 폼을 넣으면 브라우저가 안쪽을 버린다. 나란히 둔다.
            "<div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;"
            "margin-bottom:10px'>"
            "<form method='post' action='/org/dept/rename'"
            " style='display:flex;gap:6px;align-items:center'>"
            f"<input type='hidden' name='id' value='{d['id']}'>"
            f"<input type='text' name='name' value='{html.escape(d['이름'])}'"
            " style='width:220px;font-weight:700'>"
            "<button type='submit'>부서명 저장</button></form>"
            "<form method='post' action='/org/dept/delete' style='display:inline'"
            f" onsubmit=\"return confirm('부서 \\'{html.escape(d['이름'])}\\' 와 "
            f"그 아래 과제 {len(소속)}개를 삭제합니다. 배정도 함께 풀립니다.')\">"
            f"<input type='hidden' name='id' value='{d['id']}'>"
            "<button class='danger'>부서 삭제</button></form>"
            f"<span class='muted'>과제 {len(소속)}개 · 배정된 지원자 {사람}명</span>"
            "</div>"
            "<table style='width:auto'><tr><th>과제 이름</th>"
            "<th>초대암호</th><th class='w-sm'>배정</th>"
            "<th></th></tr>" + 줄 + "</table>"
            "<form method='post' action='/org/project/add'"
            " style='display:flex;gap:8px;margin-top:10px;flex-wrap:wrap'>"
            f"<input type='hidden' name='dept' value='{d['id']}'>"
            "<input type='text' name='name' placeholder='새 과제 이름' required"
            " style='width:240px'>"
            "<input type='password' name='invite' placeholder='초대암호 (선택)'"
            " autocomplete='new-password' style='width:200px'>"
            "<button type='submit'>과제 추가</button></form>"
            "</div>"
        )

    오류 = _알림(err=error)
    본문 = (
        "<div class='card'><h2>부서 추가 "
        "<span class='muted'>과제는 부서에 속합니다</span></h2>" + 오류
        + "<form method='post' action='/org/dept/add'"
        " style='display:flex;gap:8px;flex-wrap:wrap'>"
        "<input type='text' name='name' placeholder='부서 이름' required"
        " style='width:240px'>"
        "<button type='submit'>추가</button>"
        "<a class='btn sec' href='/org'>부서·과제로</a></form>"
        "<p class='muted'>현업 계정은 <b>과제</b>에 배정됩니다. 초대암호를 걸면 "
        "그 암호를 아는 사람만 그 과제로 계정을 만들 수 있습니다.</p></div>"
        + ("".join(카드) or "<div class='card muted'>부서를 먼저 추가하세요.</div>")
    )
    return _page("부서·과제 편집", 본문, me=me)


def _history_page(me: User, 대상종류: str = "", limit: int = 300) -> bytes:
    entries = audit.recent(limit, 대상종류=대상종류)
    rows = "".join(
        f"<tr><td>{html.escape(e.일시)}</td><td>{html.escape(e.사용자)}</td>"
        f"<td>{html.escape(e.대상종류)}</td><td>{html.escape(e.대상)}</td>"
        f"<td title='{html.escape(e.summary())}'>{html.escape(e.summary())}</td></tr>"
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

    # **채용을 시작한 사람만** 채용 현황에 올라온다. 인재 Pool 에 등록만 된
    # 사람까지 여기 있으면, 지금 뽑고 있는 사람이 몇 명인지 알 수가 없다.
    시작한사람 = recruit.started()
    records = [r for r in store.list_all() if r.지원자_ID in 시작한사람]
    if 보이는과제 is not None:
        records = [
            r for r in records
            if (진행맵.get(r.지원자_ID) and 진행맵[r.지원자_ID].project_id in 보이는과제)
        ]

    사용자값맵 = store.custom_map()
    사용자열이름 = set(store.field_names())
    관리값맵 = store.meta_map()

    def 값(rec, col: str) -> str:
        p = 진행맵.get(rec.지원자_ID)
        if col in MANAGE_COLUMNS:
            return 관리값맵.get(rec.지원자_ID, {}).get(col, "")
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
        고칠 일이 있으면 인재 Pool 이나 상세 화면에서 한다.
    """
    records, 진행맵, 값 = _recruit_rows(me, sort)
    보이는과제 = auth.visible_project_ids(me)
    depts = auth.departments()
    projects = auth.projects()

    표열 = store.arrange(recruit.columns())
    이름표 = 라벨(표열)
    고를수있는상태 = recruit.statuses()
    메일가능 = can(me, "메일_발송")
    # '채용 현황' 으로 만든 추가 열은 **여기서** 고친다. 그 열의 자리가 여기니까.
    채용사용자열 = {f["이름"]: f for f in store.fields()
                if (f.get("구분") or "지원자 정보") == "채용 현황"}
    열번호 = {c: n for n, c in enumerate(표열) if c in 채용사용자열}
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
                    for st in 고를수있는상태
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
            elif col in 채용사용자열 and 수정가능:
                spec = custom_field_spec(채용사용자열[col])
                이름 = f"사용자_{열번호[col]}_{html.escape(cid)}"
                if spec.입력 == "select":
                    옵션 = "".join(
                        f"<option{' selected' if o == 값(rec, col) else ''}>"
                        f"{html.escape(o)}</option>" for o in spec.선택지
                    )
                    칸 = (f"<select form='recruitform' name='{이름}' data-orig='{v}'"
                         f" onchange='markDirty(this)'>{옵션}</select>")
                else:
                    칸 = (f"<input type='text' form='recruitform' name='{이름}'"
                         f" value='{v}' style='width:140px' data-orig='{v}'"
                         f" oninput='markDirty(this)'"
                         f" title='{html.escape(spec.도움말)}'>")
                cells.append(f"<td class='ctl'>{칸}</td>")
            elif col == "최종상태":
                cls = " class='flag'" if p and p.탈락 else ""
                cells.append(f"<td{cls}>{v}</td>")
            else:
                # 지원자 정보 열은 보기만 한다 (고치려면 인재 Pool/상세에서)
                cells.append(f"<td class='{열폭(col)}' title='{v}'>{v}</td>")
        체크 = (f"<td><input type='checkbox' form='mailform' name='ids'"
              f" value='{html.escape(cid)}'></td>" if 메일가능 else "")
        링크 = f"<td><a href='/candidate?id={urllib.parse.quote(cid)}'>상세</a></td>"
        묶음 = " class='dup'" if p and p.탈락 else ""
        rows.append(f"<tr{묶음}>{체크}{링크}{''.join(cells)}</tr>")

    체크머리 = ("<th><input type='checkbox' onclick='selectVisible(this)'"
             " title='보이는 줄만 선택합니다'></th>" if 메일가능 else "")
    머리 = 체크머리 + "<th class='w-xs'></th>" + "".join(
        f"<th class='{열폭(c)}'>{머리글(이름표[c])}</th>" for c in 표열)
    알림 = _알림(msg=msg)
    오류 = _알림(err=error)
    안내 = (
        "배정된 과제의 지원자만 보입니다."
        if 보이는과제 is not None
        else "열 제목을 눌러 정렬하고, 표 위 칸으로 걸러 봅니다. 불합격자는 항상 아래로 갑니다."
    )
    if not (수정가능 or 담당자):
        안내 += " 보기 전용입니다."
    else:
        안내 += " 지원자 정보 열은 여기서 고칠 수 없습니다 (인재 Pool 에서 고치세요)."
    열이름칸 = "".join(
        f"<input type='hidden' form='recruitform' name='사용자열_{n}'"
        f" value='{html.escape(c)}'>" for c, n in 열번호.items()
    )
    # 파란 띠를 두 개 쌓으면 화면이 무겁다. 띠는 하나로 두고 그 안에 단추를
    # 나란히 놓는다. 폼은 표 밖에 두고 form= 로 잇는다 (폼 중첩 금지).
    메일폼 = (
        "<form method='post' action='/mail/compose' id='mailform'>"
        "<input type='hidden' name='back' value='/recruit'></form>"
        if rows and 메일가능 else ""
    )
    저장폼 = (
        "<form method='post' action='/recruit/save' id='recruitform'>"
        + 열이름칸 + "</form>"
        if rows and (수정가능 or 담당자) else ""
    )
    단추들 = []
    if 저장폼:
        단추들.append("<button type='submit' form='recruitform'>고친 내용 저장</button>")
    if 메일폼:
        단추들.append("<button type='submit' form='mailform' class='sec'>"
                    "선택한 사람에게 메일</button>")
    저장바 = (
        메일폼 + 저장폼
        + "<div class='mergebar'>" + " ".join(단추들)
        + "<span class='muted'>"
        + ("여러 줄을 고친 뒤 <b>한 번만</b> 누르세요. 고친 칸은 노랗게 표시됩니다. "
           if 저장폼 else "")
        + ("메일은 체크한 사람에게만 갑니다 — 여기서는 <b>누가 어느 단계인지 "
           "보면서</b> 고를 수 있습니다." if 메일폼 else "")
        + "</span></div>"
        if 단추들 else ""
    )
    메일바 = ""
    열구성 = (
        "<a class='btn sec' href='/recruit/columns'>표 열 구성</a> "
        if can(me, "열_구성") else ""
    )
    빈화면 = (
        "<p class='muted'>여기에는 <b>채용을 시작한 사람만</b> 나옵니다. "
        + ("<a href='/'>인재 Pool</a> 에서 `채용 시작` 을 누르세요.</p>"
           if can(me, "지원자_목록") else "채용담당자가 시작하면 보입니다.</p>")
    )
    표 = (
        "<div class='scroll'><table data-name='채용현황'"
        " data-export='/recruit/export.xlsx'>"
        f"<tr>{머리}</tr>{''.join(rows)}</table></div>"
        if rows else 빈화면
    )
    # 표가 비어 있을 때는 빈 화면 안내가 같은 말을 하므로 두 번 쓰지 않는다.
    출처 = (
        " <a href='/'>인재 Pool</a> 에서 <b>채용 시작</b>을 누른 사람만 여기 올라옵니다."
        if rows and can(me, "지원자_목록") else ""
    )
    return _page(
        "채용 현황",
        f"""{알림}{오류}<div class='card'><h2>채용 현황 <span class='muted'>{len(records)}명</span></h2>
        <p class='muted'>{안내}{출처}</p>
        <p>{열구성}</p>
        {메일바}{저장바}{표}</div>
        <script>var 과제표 = {과제표};{_RECRUIT_JS}</script>""",
        me=me,
    )


def _recruit_columns_page(me: User) -> bytes:
    """관리자가 채용 현황 표에 보일 열과 순서를 정한다."""
    전체 = [c for _, c, _ in 열목록()]
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



#: 열이 어디에 쓰이는지 — 표 항목 탭에서 한눈에 보이게 적어 둔다.
COLUMN_GROUP_NOTE = {
    "지원자 정보": "인재 Pool · 엑셀 내려받기",
    "관리 정보": "언제 등록했고 원본이 무엇인지. 표에서는 보기만 합니다.",
    "채용 현황": "채용 현황 표. 어떤 열을 쓸지는 [표 열 구성] 에서 고릅니다.",
}


def _선택지편집(col: str, 지금: list[str], action: str, 고정: tuple[str, ...] = (),
             도움말: str = "") -> str:
    """선택지를 그 자리에서 고치는 작은 폼.

    형식 검사·추출 스키마를 건드리지 않는 열에만 붙인다.
    """
    보임 = " | ".join(v for v in 지금 if v)
    잠긴것 = (
        "<br><span class='muted'>못 빼는 값: "
        + html.escape(", ".join(v or "(빈칸)" for v in 고정)) + "</span>"
        if 고정 else ""
    )
    return (
        f"<form method='post' action='{action}' style='display:flex;gap:6px;"
        "align-items:center;flex-wrap:wrap;margin-top:4px'>"
        f"<input type='hidden' name='col' value='{html.escape(col)}'>"
        f"<input type='text' name='choices' value='{html.escape(보임)}'"
        " style='width:230px' placeholder='| 로 구분'>"
        "<button type='submit' class='sec'>선택지 저장</button>"
        + (f"<span class='muted'>{도움말}</span>" if 도움말 else "")
        + f"</form>{잠긴것}"
    )


#: 열 순서를 **끌어서** 정한다.
#:
#: 예전에는 칸에 숫자를 쳐서 자리를 매겼다. 열이 쉰 개가 넘으면 사람이 할 일이
#: 아니다 — 하나를 앞으로 보내려고 나머지 번호를 전부 다시 세야 했다.
#: 이제 순서는 **줄의 위치**가 정하고, 숨은 칸이 그 위치를 그대로 받아 적는다.
_COLORDER_JS = """
<script>
(function(){
  var 표 = document.getElementById('colorder');
  if(!표) return;
  var 끄는줄 = null;

  function 번호다시(){
    /* 화면에 보이는 차례가 곧 순서다. 사람이 세지 않는다. */
    var n = 0;
    표.querySelectorAll('tr[data-col]').forEach(function(tr){
      var f = tr.querySelector('.ordfield');
      if(f){ f.value = String(++n); }
    });
    var 저장 = document.querySelector('#colform button[type=submit]');
    if(저장) 저장.classList.add('dirty');
  }

  window.colMove = function(btn, 어디){
    var tr = btn.closest('tr');
    var 형제 = 어디 < 0 ? tr.previousElementSibling : tr.nextElementSibling;
    /* '숨긴 열' 머리줄은 건너뛴다 — 그 위/아래로 넘어가면 숨김도 같이 바뀐다 */
    while(형제 && !형제.dataset.col){
      형제 = 어디 < 0 ? 형제.previousElementSibling : 형제.nextElementSibling;
    }
    if(!형제) return;
    if(어디 < 0) tr.parentNode.insertBefore(tr, 형제);
    else tr.parentNode.insertBefore(형제, tr);
    번호다시();
    tr.scrollIntoView({block: 'nearest'});
  };

  표.addEventListener('dragstart', function(e){
    var tr = e.target.closest('tr[data-col]');
    if(!tr) return;
    끄는줄 = tr;
    tr.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    /* 파이어폭스는 데이터를 넣어야 끌기가 시작된다 */
    try { e.dataTransfer.setData('text/plain', tr.dataset.col); } catch(_){}
  });
  표.addEventListener('dragend', function(){
    if(끄는줄) 끄는줄.classList.remove('dragging');
    끄는줄 = null;
    표.querySelectorAll('.dropmark').forEach(function(x){
      x.classList.remove('dropmark');
    });
  });
  표.addEventListener('dragover', function(e){
    if(!끄는줄) return;
    e.preventDefault();
    var tr = e.target.closest && e.target.closest('tr[data-col]');
    if(!tr || tr === 끄는줄) return;
    표.querySelectorAll('.dropmark').forEach(function(x){
      x.classList.remove('dropmark');
    });
    tr.classList.add('dropmark');
    var r = tr.getBoundingClientRect();
    var 위쪽 = (e.clientY - r.top) < r.height / 2;
    tr.parentNode.insertBefore(끄는줄, 위쪽 ? tr : tr.nextSibling);
  });
  표.addEventListener('drop', function(e){
    if(!끄는줄) return;
    e.preventDefault();
    번호다시();
  });
})();
</script>"""


def _fields_page(me: User, error: str = "", msg: str = "") -> bytes:
    """표에 나갈 열을 관리한다 — **이 시스템이 아는 모든 열을 한 자리에서.**

    예전에는 지원자 정보 열과 직접 추가한 열만 보였다. 그래서 채용 현황 열
    이름을 바꿀 수 없었고, 등록년도·원본 파일명 같은 관리 정보 열은 아예
    표에 올릴 수도 없었다. 이제 세 묶음을 모두 보여주고, 추가한 열도 어느
    묶음에 속하는지 달아서 그 자리에 끼워 넣는다.

    고칠 수 있는 것과 없는 것의 경계는 하나다 — **형식 검사와 추출 스키마.**
    거기 걸려 있지 않은 것(단계 상태 목록, 추가한 열의 선택지·유형·이름)은
    고칠 수 있고, 걸려 있는 것(지원자 정보 열의 선택지)은 못 고친다.
    """
    사용자열 = {f["이름"]: f for f in store.fields()}
    cfg = store.column_config()
    유형옵션 = "".join(f"<option>{t}</option>" for t in CUSTOM_TYPES)
    구분옵션 = "".join(f"<option>{g}</option>" for g in CUSTOM_SCOPES)
    쓰는채용열 = set(recruit.columns())
    지금상태 = recruit.statuses()

    def 설명(구분: str, col: str, 추가열: bool) -> str:
        if 추가열:
            f = 사용자열[col]
            머리 = f"{f['유형']}"
            꼬리 = (f"<br><span class='muted'>{html.escape(f['만든일시'])}"
                  + (f" ({html.escape(f['만든이'])})" if f["만든이"] else "")
                  + "</span>")
            if f["유형"] == "선택":
                고를것 = [o.strip() for o in (f["선택지"] or "").split("|") if o.strip()]
                return 머리 + _선택지편집(col, 고를것, "/fields/choices") + 꼬리
            return 머리 + 꼬리
        if 구분 == "관리 정보":
            return "<span class='muted'>자동 기록 (고치려면 지원자 상세 화면)</span>"
        if 구분 == "채용 현황":
            if col in STAGES:
                return "선택" + _선택지편집(
                    col, 지금상태, "/recruit/statuses", 고정=FIXED_STATUSES,
                    도움말="네 단계가 같은 목록을 씁니다",
                )
            if col in ("부서", "과제"):
                return "<span class='muted'>조직에서 고른 값 (부서·과제 화면에서 관리)</span>"
            if col == "최종상태":
                return "<span class='muted'>계산 결과 (단계 상태에서 정함)</span>"
            return "텍스트"
        if col in CHOICE_FIELDS:
            return ("선택 · " + html.escape(", ".join(v or "(빈칸)" for v in CHOICE_FIELDS[col]))
                    + "<br><span class='muted'>추출 스키마에 걸려 있어 못 바꿉니다</span>")
        if col in REGISTRY_FIELDS:
            return "명칭 사전 " + html.escape(NAME_COLUMNS[col])
        if col.startswith(TIER_COLUMN_PREFIX):
            return "<span class='muted'>계산 결과 (논문 목록에서 셈)</span>"
        spec = field_spec(col)
        return html.escape(spec.도움말 or "텍스트")

    전체 = 열목록()
    묶음수: dict[str, int] = {}
    for 구분, _c, _a in 전체:
        묶음수[구분] = 묶음수.get(구분, 0) + 1

    # **지금 표에 나오는 순서 그대로** 늘어놓는다.
    #
    # 예전에는 지원자 정보 / 관리 정보 / 채용 현황으로 묶어서 보여줬다. 그러면
    # 화면에서 실제로 몇 번째에 있는 열인지 알 수가 없고, 순서를 바꾸려면 머릿속
    # 으로 세 묶음을 합쳐 봐야 했다. 보이는 대로 늘어놓으면 그럴 일이 없다.
    구분맵 = {col: (구분, 추가) for 구분, col, 추가 in 전체}
    보이는순서 = [c for c in 표열() if c in 구분맵]
    숨은것 = [c for _g, c, _a in 전체 if c not in set(보이는순서)]
    차례 = [(구분맵[c][0], c, 구분맵[c][1]) for c in 보이는순서 + 숨은것]

    rows = []
    첫숨김 = len(보이는순서)
    for i, (구분, col, 추가열) in enumerate(차례, start=1):
        if i == 첫숨김 + 1:
            rows.append(
                "<tr class='grouphead'><td colspan='7'><b>숨긴 열</b> "
                f"<span class='muted'>{len(숨은것)}개 — 표에 안 나옵니다. "
                "숨김을 풀면 여기 순서대로 뒤에 붙습니다.</span></td></tr>"
            )
        c = cfg.get(col, {})
        숨김중 = c.get("숨김") or 기본숨김(col, cfg)
        꼬리 = ""
        if 구분 == "채용 현황" and col not in 쓰는채용열:
            꼬리 = " <span class='muted'>(지금 표에 없음)</span>"
        이름칸 = (
            # 이름을 입력칸에만 두면 표의 찾기·복사가 못 잡는다. 글자로도 남긴다.
            f"<div class='muted' style='font-size:11px'>{html.escape(col)}</div>"
            f"<input type='text' form='colform' name='rename_{i}'"
            f" value='{html.escape(col)}' style='width:150px'"
            f" data-orig='{html.escape(col)}' oninput='markDirty(this)'>"
            f"<select form='colform' name='scope_{i}' onchange='markDirty(this)'"
            f" data-orig='{html.escape(사용자열[col].get('구분') or '지원자 정보')}'"
            " style='width:110px;margin-top:3px'>"
            + "".join(
                f"<option{' selected' if g == (사용자열[col].get('구분') or '지원자 정보') else ''}>"
                f"{g}</option>" for g in CUSTOM_SCOPES)
            + "</select>"
            if 추가열 else f"{html.escape(col)}{꼬리}"
        )
        rows.append(
            f"<tr draggable='true' data-col='{html.escape(col)}'>"
            # 끌어서 옮기는 손잡이 + 한 칸씩 옮기는 단추. 숫자를 쳐서 자리를
            # 매기는 건 열이 쉰 개가 넘으면 사람이 할 일이 아니다.
            "<td class='ctl grip' title='끌어서 옮기세요'>"
            "<span class='griph'>⠿</span> "
            "<button type='button' class='sec tiny' onclick='colMove(this,-1)'"
            " title='위로'>↑</button>"
            "<button type='button' class='sec tiny' onclick='colMove(this,1)'"
            " title='아래로'>↓</button></td>"
            f"<td>{이름칸}</td>"
            f"<td><span class='pill {'p-완료' if 추가열 else 'p-대기중'}'>"
            f"{'추가한 열' if 추가열 else html.escape(구분)}</span></td>"
            f"<td style='white-space:normal'>{설명(구분, col, 추가열)}</td>"
            f"<td class='ctl'><input type='hidden' form='colform' name='col_{i}'"
            f" value='{html.escape(col)}'>"
            f"<input type='text' form='colform' name='label_{i}'"
            f" value='{html.escape(c.get('표시이름') or '')}'"
            f" placeholder='{html.escape(col)}' style='width:150px'"
            f" data-orig='{html.escape(c.get('표시이름') or '')}' oninput='markDirty(this)'></td>"
            f"<input type='hidden' form='colform' name='order_{i}' value='{i}'"
            " class='ordfield'>"
            f"<td><label><input type='checkbox' form='colform' name='hide_{i}'"
            f"{' checked' if 숨김중 else ''} onchange='markDirty(this)'"
            f" data-orig=''> 숨김</label></td>"
            f"<td>" + (
                "<form method='post' action='/fields/delete' style='display:inline'"
                " onsubmit=\"return confirm('이 열과 여기 들어있던 모든 값이 지워집니다.')\">"
                f"<input type='hidden' name='name' value='{html.escape(col)}'>"
                "<button class='danger'>삭제</button></form>" if 추가열
                else "<span class='muted'>-</span>"
            ) + "</td></tr>"
        )

    알림 = _알림(msg=msg)
    오류 = _알림(err=error)
    묶음요약 = " · ".join(f"{k} {v}개" for k, v in 묶음수.items())
    return _page(
        "표 항목",
        알림
        + "<div class='card'><h2>열 추가</h2>" + 오류
        + "<form method='post' action='/fields/add' style='display:flex;gap:8px;flex-wrap:wrap'>"
        "<input type='text' name='name' placeholder='열 이름' required>"
        f"<select name='scope'>{구분옵션}</select>"
        f"<select name='type'>{유형옵션}</select>"
        "<input type='text' name='choices' placeholder=\"선택지 (선택 유형만, | 로 구분)\""
        " style='width:280px'>"
        "<button type='submit'>추가</button></form>"
        "<p class='muted'><b>구분</b>을 고르면 그 표에 붙습니다 — "
        "<b>지원자 정보</b>는 인재 Pool·엑셀에, <b>채용 현황</b>은 채용 현황 표에. "
        "유형에 따라 입력칸이 달라지고 형식이 강제됩니다. "
        "<b>값은 사람이 채웁니다</b> — LLM 이 자동으로 채우지 않습니다.</p></div>"

        + "<div class='card'><h2>표에 나갈 열 "
        f"<span class='muted'>{html.escape(묶음요약)}</span></h2>"
        "<form method='post' action='/fields/columns' id='colform' class='mergebar'>"
        "<button type='submit'>고친 내용 저장</button>"
        "<span class='muted'><b>표에 나오는 순서 그대로</b> 늘어놨습니다. "
        "줄을 <b>끌어서</b> 옮기거나 <b>↑ ↓</b> 를 누르고, 이름·숨김을 고친 뒤 "
        "<b>한 번만</b> 누르세요.</span></form>"
        "<div class='scroll'><table data-name='표 항목' id='colorder'>"
        "<tr><th class='ctl' style='width:86px'>순서</th>"
        "<th>열 이름</th><th>구분</th><th>입력 형식</th>"
        "<th class='ctl'>표에 보일 이름</th><th>숨김</th><th></th></tr>"
        + "".join(rows) + "</table></div>"
        + _COLORDER_JS +
        "<p class='muted'>여기서 정한 이름·순서·숨김은 <b>화면과 엑셀에 함께</b> 적용됩니다. "
        "고칠 수 있는 것과 없는 것의 경계는 하나입니다 — <b>형식 검사와 추출 스키마</b>. "
        "단계 상태 목록과 추가한 열의 선택지·유형·이름은 고칠 수 있고, 지원자 정보 열의 "
        "선택지는 추출 스키마에 걸려 있어 못 고칩니다. 안 쓰는 열은 <b>숨김</b>으로 두세요.</p>"
        "</div>",
        me=me,
    )


# ---------------------------------------------------------------------------
# 대시보드 화면
# ---------------------------------------------------------------------------
#: 처음 만들 때 넣어 주는 프로필 양식. 빈 화면에서 시작하면 아무도 안 만든다.
기본_프로필틀 = [
    ["학력", "{박사_학교} {박사_전공}({기간:박사_시작~박사_졸업}) {박사_학위상태}"],
    ["", "{석사_학교} {석사_전공}({기간:석사_시작~석사_졸업})"],
    ["현재", "{현재_소속} {현재_소속_상세}"],
    ["경력", "{경력_회사}/{직책}({기간:경력_시작~경력_종료})"],
    ["실적", "저널 {수:저널_수}편(주저자 {수:저널_주저자_수}) · "
            "학회 {수:학회_수}편(주저자 {수:학회_주저자_수}) · "
            "특허 등록 {수:특허_등록_수}/출원 {수:특허_출원_수}"],
    ["채용", "{부서} {과제} — {최종상태}"],
    ["매칭", "{매칭_과제} ({수:매칭_점수}점)"],
]


def _프로필값(cid: str) -> dict[str, str]:
    """문장 틀에 넣을 한 사람의 값. 표에 보이는 것과 같은 값을 쓴다."""
    rec = store.get(cid)
    if rec is None:
        return {}
    행 = {k: str(v or "") for k, v in rec.to_row(registry).items()}
    행["지원자_ID"] = cid
    행["검토_사유"] = review.display(rec.검토_사유, store.review_done(cid))
    행.update(store.meta_map().get(cid, {}))
    행.update(store.custom_values(cid))
    p = recruit.get(cid)
    부서명 = {d["id"]: d["이름"] for d in auth.departments()}
    과제명 = {pr["id"]: pr["이름"] for pr in auth.projects()}
    행["부서"] = 부서명.get(p.부서_id, "")
    행["과제"] = 과제명.get(p.project_id, "")
    행["최종상태"] = p.최종상태
    행["비고"] = p.비고
    for 단계 in STAGES:
        행[단계] = p.단계상태.get(단계, "")
    m = store.top_matches().get(cid) or {}
    행["매칭_과제"] = m.get("과제명", "")
    행["매칭_점수"] = str(m.get("점수", "") or "")
    return 행


def _쉼표목록(글: str) -> list[str]:
    return [x.strip() for x in (글 or "").split(",") if x.strip()]


def _수식검사(수식: str, 아는열: set[str]) -> str:
    """수식이면 검사하고, 틀리면 사람이 읽을 오류를 돌려준다. 괜찮으면 빈 문자열.

    **저장 단계에서 막는 게 핵심이다.** 없는 열 이름이 그대로 저장되면 화면에는
    그냥 0 이 뜨고 아무도 틀린 줄 모른다.
    """
    수식 = (수식 or "").strip()
    if not 수식 or not F.is_formula(수식):
        return ""
    try:
        F.validate(수식, 아는열)
    except F.FormulaError as exc:
        return f"{수식} → {exc}"
    return ""


def _표모양설정(설정: dict, data: dict) -> None:
    """표 모양 고르개에서 온 값. 안 보낸 칸은 건드리지 않는다."""
    테두리 = (data.get("border") or [""])[0]
    if 테두리 in ("가로줄", "격자", "없음"):
        설정["테두리"] = 테두리
    폭 = (data.get("tablewidth") or [""])[0]
    if 폭 in ("창에 맞춤", "내용에 맞춤"):
        설정["표너비"] = 폭
    if "border" in data:            # 이 폼이 표 모양을 담고 있었다는 뜻
        설정["줄무늬"] = bool(data.get("zebra"))
        설정["촘촘히"] = bool(data.get("tight"))
        # '기본색' 을 켜 두면 색을 저장하지 않는다 — 화면 테마를 따라가게.
        색 = (data.get("headbg") or [""])[0].strip()
        설정["머리배경"] = "" if data.get("headbgoff") else 색


def _블록설정(b, data: dict) -> tuple[dict, str]:
    """폼에서 온 값을 블록 설정으로. (설정, 오류메시지)"""
    아는열 = 대시보드_열()
    설정 = dict(b.설정)
    _표모양설정(설정, data)
    형식 = (data.get("format") or [""])[0]
    if 형식 in CELL_FORMATS:
        설정["형식"] = 형식

    if b.종류 == "글":
        설정["글"] = (data.get("text") or [""])[0]
        return 설정, ""

    if b.종류 == "숫자":
        수식 = (data.get("formula") or [""])[0].strip()
        오류 = _수식검사(수식, 아는열)
        if 오류:
            return 설정, 오류
        설정["수식"] = 수식
        return 설정, ""

    if b.종류 == "축표":
        설정["행축"] = (data.get("rowaxis") or [""])[0]
        설정["열축"] = (data.get("colaxis") or [""])[0]
        설정["행"] = _쉼표목록((data.get("rows") or [""])[0])
        설정["열"] = _쉼표목록((data.get("cols") or [""])[0])
        칸수식 = (data.get("cellformula") or [""])[0].strip()
        # {행}{열} 을 실제 값으로 한 번 바꿔 놓고 검사한다. 그대로 검사하면
        # '{행}' 이 열 이름인 줄 알고 엉뚱한 오류가 난다.
        축값 = 대시보드_축()
        본보기 = (칸수식.replace("{행}", (축값.get(설정["행축"]) or 설정["행"] or [""])[0])
                       .replace("{열}", (축값.get(설정["열축"]) or 설정["열"] or [""])[0]))
        오류 = _수식검사(본보기, 아는열)
        if 오류:
            return 설정, 오류.replace(본보기, 칸수식)
        설정["칸수식"] = 칸수식
        return 설정, ""

    if b.종류 == "목록":
        설정["목록대상"] = ((data.get("listtarget") or ["지원자"])[0]
                        if (data.get("listtarget") or ["지원자"])[0] in ("지원자", "채용")
                        else "지원자")
        설정["목록조건"] = (data.get("listwhere") or [""])[0].strip()
        설정["목록정렬"] = (data.get("listsort") or [""])[0].strip()
        설정["목록내림차순"] = bool(data.get("listdesc"))
        try:
            설정["목록최대"] = max(0, int((data.get("listmax") or ["0"])[0] or 0))
        except ValueError:
            설정["목록최대"] = 0
        머리들 = data.get("colhead") or []
        식들 = data.get("colformula") or []
        폭들 = (data.get("colwidth") or []) + [""] * len(식들)
        열 = [[머리.strip(), 식.strip(), 폭.strip()]
             for 머리, 식, 폭 in zip(머리들, 식들, 폭들) if 식.strip()]
        if not 열:
            return 설정, "열이 하나도 없습니다. 머리글과 수식을 적으세요."
        # 문법부터 틀렸으면 저장을 막는다. 조용히 넘어가면 화면에서 ? 만 보인다.
        볼것 = [("행 고르기", 설정["목록조건"]), ("정렬", 설정["목록정렬"])]
        볼것 += [(머리 or 식, 식) for 머리, 식, _폭 in 열]
        for 자리, 식 in 볼것:
            if not expr.is_formula(식):
                continue
            try:
                expr.validate(식, 아는열)
            except expr.ExprError as exc:
                return 설정, f"'{자리}' 수식이 잘못됐습니다 — {exc}"
        설정["목록열"] = 열

        # 값에 따라 칠하기. 조건도 수식이라 저장 전에 검사한다.
        조건들 = data.get("cfwhen") or []
        어디들 = (data.get("cfwhere") or []) + [ROW_TARGET] * len(조건들)
        배경들 = (data.get("cfbg") or []) + [""] * len(조건들)
        # 색 고르개는 항상 값을 보내므로, '기본' 체크는 그 자리의 색을 지우는 뜻이다.
        # 체크박스는 켠 것만 오기 때문에 순서로 짝을 지을 수 없다 — 그래서 글자색은
        # 켠 규칙 수만큼만 받고, 나머지는 기본색으로 둔다.
        글자들 = (data.get("cffg") or []) + [""] * len(조건들)
        글자쓰나 = (data.get("cffgmode") or []) + ["기본"] * len(조건들)
        서식 = []
        for i, 조건 in enumerate(조건들):
            조건 = 조건.strip()
            if not 조건:
                continue
            if not expr.is_formula(조건):
                조건 = "=" + 조건
            try:
                expr.validate(조건, 아는열)
            except expr.ExprError as exc:
                return 설정, f"색칠 조건이 잘못됐습니다 — {exc}"
            서식.append({
                "조건": 조건, "대상": 어디들[i], "배경": 배경들[i],
                "글자": 글자들[i] if 글자쓰나[i] == "직접" else "",
            })
        설정["조건서식"] = 서식
        return 설정, ""

    if b.종류 == "프로필":
        대상 = (data.get("target") or [""])[0].strip() or "=LIST(지원자)"
        오류 = _수식검사(대상, 아는열)
        if 오류:
            return 설정, 오류
        설정["대상"] = 대상
        설정["머리"] = (data.get("head") or [""])[0]
        라벨들 = data.get("label") or []
        틀들 = data.get("line") or []
        줄 = [[라벨.strip(), 틀.strip()]
             for 라벨, 틀 in zip(라벨들, 틀들) if 틀.strip()]
        # 수식이 문법부터 틀렸으면 **저장을 막는다.** 조용히 넘어가면 나중에
        # 화면에서 빈칸으로 나오는데, 값이 없는 건지 잘못 쓴 건지 알 수가 없다.
        for 라벨, 틀 in 줄 + [["머리", 설정["머리"]]]:
            if not expr.is_formula(틀):
                continue
            try:
                expr.parse(틀)
            except expr.ExprError as exc:
                자리 = f"'{라벨}' 줄" if 라벨 else "문장"
                return 설정, f"{자리}의 수식이 잘못됐습니다 — {exc}"
        모르는 = sorted({c for _l, 틀 in 줄 for c in P.columns(틀)
                       if c and c not in 아는열}
                      | {c for c in P.columns(설정["머리"]) if c and c not in 아는열})
        if 모르는:
            return 설정, ("표에 없는 열입니다: " + ", ".join(모르는)
                        + " — 표 항목 탭에 있는 이름을 그대로 쓰세요.")
        설정["줄"] = 줄
        return 설정, ""

    # 자유 표
    행 = _쉼표목록((data.get("rows") or [""])[0])
    열 = _쉼표목록((data.get("cols") or [""])[0])
    칸값 = data.get("cell") or []
    칸 = {}
    if 칸값 and len(칸값) == len(b.행이름) * len(b.열이름):
        # 폼은 옛 행·열 순서대로 왔다. 그 짝으로 읽고 새 행·열에 맞춰 남긴다.
        i = 0
        for r in b.행이름:
            for c in b.열이름:
                if 칸값[i].strip():
                    칸[_칸키(r, c)] = 칸값[i].strip()
                i += 1
    else:
        칸 = dict(b.칸)
    for 키, 수식 in 칸.items():
        오류 = _수식검사(수식, 아는열)
        if 오류:
            return 설정, f"[{키.replace(chr(9), ' / ')}] {오류}"
    설정["행"], 설정["열"], 설정["칸"] = 행, 열, 칸
    return 설정, ""


def _예시블록(did: int) -> None:
    """새 대시보드에 바로 볼 수 있는 블록을 넣어 준다.

    빈 화면에서 시작하면 아무도 안 만든다. 예시가 곧 사용법이다.
    """
    boards.add_block(did, "숫자", 제목="채용 중",
                     설정={"수식": "=COUNT(채용)", "형식": "명"})
    boards.add_block(did, "축표", 제목="부서 × 단계",
                     설정={"행축": "부서", "열축": "단계",
                          "칸수식": '=COUNT(채용, 부서="{행}", {열}="합격")',
                          "형식": "그대로"})
    boards.add_block(did, "축표", 제목="등록년도 × 현재 신분",
                     설정={"행축": "등록년도", "열축": "현재_신분",
                          "칸수식": '=COUNT(지원자, 등록년도="{행}", 현재_신분="{열}")'})
    boards.add_block(did, "프로필", 제목="채용 중인 사람",
                     설정={"대상": "=LIST(채용)",
                          "머리": "{한글_이름} ({현재_신분})",
                          "줄": 기본_프로필틀})


def _dash_list_page(me: User, error: str = "", msg: str = "") -> bytes:
    보드 = boards.all()
    편집 = can(me, "대시보드_조회")
    rows = "".join(
        f"<tr><td><a href='/dash/view?id={d.id}'>{html.escape(d.이름)}</a></td>"
        f"<td class='muted' title='{html.escape(d.설명)}'>{html.escape(d.설명)}</td>"
        f"<td class='muted'>{len(boards.blocks(d.id))}개</td>"
        f"<td class='muted'>{html.escape(d.수정일시)}</td>"
        f"<td><a class='btn' href='/dash/view?id={d.id}'>보기</a> "
        + (f"<a class='btn sec' href='/dash/edit?id={d.id}'>편집</a> "
           "<form method='post' action='/dash/copy' style='display:inline'>"
           f"<input type='hidden' name='id' value='{d.id}'>"
           "<button class='sec'>복제</button></form> "
           "<form method='post' action='/dash/delete' style='display:inline'"
           " onsubmit=\"return confirm('이 대시보드를 지웁니다.')\">"
           f"<input type='hidden' name='id' value='{d.id}'>"
           "<button class='danger'>삭제</button></form>" if 편집 else "")
        + "</td></tr>"
        for d in 보드
    ) or "<tr><td colspan='5' class='muted'>아직 만든 대시보드가 없습니다.</td></tr>"

    알림 = _알림(msg=msg)
    오류 = _알림(err=error)
    return _page(
        "대시보드",
        알림
        + "<div class='card'><h2>대시보드 만들기</h2>" + 오류
        + "<form method='post' action='/dash/add' style='display:flex;gap:8px;flex-wrap:wrap'>"
        "<input type='text' name='name' placeholder='이름 (예: 주간 채용 현황판)'"
        " required style='width:260px'>"
        "<input type='text' name='desc' placeholder='설명 (선택)' style='width:320px'>"
        "<label class='muted'><input type='checkbox' name='sample' value='1' checked>"
        " 예시 블록 넣기</label>"
        "<button type='submit'>만들기</button></form>"
        "<p class='muted'>만든 뒤 <b>편집</b> 에서 블록을 쌓습니다. "
        "예시 블록을 켜 두면 바로 볼 수 있는 것부터 들어갑니다.</p></div>"
        + f"<div class='card'><h2>대시보드 {len(보드)}개</h2><div class='scroll'>"
        "<table data-name='대시보드'><tr><th>이름</th><th>설명</th><th>블록</th>"
        "<th>수정</th><th></th></tr>" + rows + "</table></div></div>",
        me=me,
    )


def _블록그리기(b, rows, 축값, 아는열) -> str:
    """블록 하나를 보기 화면용 HTML 로."""
    if b.종류 == "글":
        본문 = "<br>".join(html.escape(x) for x in (b.글 or "").splitlines())
        return (f"<div class='card'><h2>{html.escape(b.제목)}</h2>{본문}</div>"
                if (b.제목 or 본문) else "")

    if b.종류 == "숫자":
        try:
            글, 값 = F.run(b.수식, rows, 아는열)
            보임 = format_cell(글, 값, b.설정.get("형식") or "그대로")
            아래 = f"<div class='muted'>{html.escape(b.수식)}</div>"
        except F.FormulaError as exc:
            보임, 아래 = "?", f"<div class='flag'>{html.escape(str(exc))}</div>"
        return (
            f"<div class='card'><h2>{html.escape(b.제목)}</h2>"
            f"<div style='font-size:38px;font-weight:800;line-height:1.2'>{html.escape(보임)}</div>"
            f"{아래}</div>"
        )

    if b.종류 == "목록":
        결과 = render_list(b, rows, 아는열)
        경고 = "".join(f"<p class='flag'>{html.escape(x)}</p>" for x in 결과.오류)

        def 폭스타일(i: int) -> str:
            """정한 너비가 있으면 그 폭으로 못박는다. 없으면 열 이름으로 짐작."""
            정한것 = 결과.폭[i] if i < len(결과.폭) else ""
            if 정한것:
                return f" style='width:{html.escape(정한것)}px'"
            return ""

        def 폭클래스(i: int) -> str:
            if i < len(결과.폭) and 결과.폭[i]:
                return ""                       # 직접 정했으면 짐작하지 않는다
            return 열폭(결과.머리[i] if i < len(결과.머리) else "")

        머리 = "".join(
            f"<th class='{폭클래스(i)}'{폭스타일(i)}>{머리글(c)}</th>"
            for i, c in enumerate(결과.머리)
        )

        def 칸(줄번호: int, i: int, v: str) -> str:
            # 줄바꿈(CHAR(10))을 일부러 넣은 칸은 **줄을 바꿔서** 보여준다.
            # 다른 칸은 그대로 한 줄로 잘린다 — 줄 높이가 들쭉날쭉해지면 표를
            # 훑기 어려우니, 바꾸는 건 그러라고 적은 칸뿐이다.
            cls = 폭클래스(i) + (" multi" if "\n" in v else "")
            속 = ("<br>".join(html.escape(줄) for 줄 in v.split("\n"))
                 if "\n" in v else html.escape(v))
            # 조건서식. 칸 규칙이 줄 규칙을 이긴다 — 더 좁게 가리킨 쪽이 이긴다.
            #
            # 줄 색도 **칸마다** 칠한다. <tr> 에 걸고 물려받게 하면 얼룩말 무늬나
            # hover 가 덮어써서 어떤 줄은 칠해지고 어떤 줄은 안 칠해진다.
            칸색 = ""
            if 줄번호 < len(결과.칸색) and i < len(결과.칸색[줄번호]):
                칸색 = 결과.칸색[줄번호][i]
            줄색 = 결과.행색[줄번호] if 줄번호 < len(결과.행색) else ""
            색 = 칸색 or 줄색
            폭 = 폭스타일(i)
            안쪽 = ";".join(x for x in (색, 폭[8:-1] if 폭 else "") if x)
            스타일 = f" style='{안쪽}'" if 안쪽 else ""
            칠함 = " painted" if 색 else ""
            return (f"<td class='{(cls + 칠함).strip()}'{스타일}"
                    f" title='{html.escape(v)}'>{속}</td>")

        몸 = "".join(
            "<tr>" + "".join(칸(n, i, v) for i, v in enumerate(칸들)) + "</tr>"
            for n, 칸들 in enumerate(결과.행)
        ) or (f"<tr><td colspan='{max(1, len(결과.머리))}' class='muted'>"
              "조건에 맞는 사람이 없습니다.</td></tr>")
        센것 = (f"{len(결과.행)}줄"
              + (f" <span class='muted'>/ 전체 {결과.전체}</span>"
                 if 결과.전체 != len(결과.행) else ""))
        return (
            f"<div class='card'><h2>{html.escape(b.제목)} "
            f"<span class='muted'>{센것}</span></h2>{경고}"
            f"<div class='scroll'><table {_표모양(b)}"
            f" data-name='{html.escape(b.제목 or '목록')}'>"
            f"<tr>{머리}</tr>{몸}</table></div></div>"
        )

    if b.종류 == "프로필":
        결과 = render_profile(b, rows, _프로필값, 아는열)
        경고 = "".join(f"<p class='flag'>{html.escape(x)}</p>" for x in 결과.오류)
        카드 = []
        for 머리, 줄들 in 결과.사람:
            줄 = "".join(
                f"<tr><th style='width:80px'>{html.escape(라벨)}</th>"
                f"<td style='white-space:pre-wrap;max-width:none'>{html.escape(값)}</td></tr>"
                for 라벨, 값 in 줄들
            )
            카드.append(
                "<div style='border:1px solid var(--line);border-radius:8px;"
                "padding:12px 14px;margin-bottom:10px'>"
                f"<div style='font-weight:800;margin-bottom:6px'>{html.escape(머리)}</div>"
                f"<table {_표모양(b)}>{줄}</table></div>"
            )
        몸 = "".join(카드) or "<p class='muted'>조건에 맞는 사람이 없습니다.</p>"
        return (f"<div class='card'><h2>{html.escape(b.제목)} "
                f"<span class='muted'>{len(결과.사람)}명</span></h2>{경고}{몸}</div>")

    결과 = render_table(b, rows, 축값, 아는열)
    경고 = "".join(f"<p class='flag'>{html.escape(x)}</p>" for x in 결과.오류)
    머리 = "<th></th>" + "".join(f"<th>{html.escape(c)}</th>" for c in 결과.머리)
    몸 = "".join(
        f"<tr><th style='text-align:left'>{html.escape(r)}</th>"
        + "".join(f"<td>{html.escape(v)}</td>" for v in 칸들) + "</tr>"
        for r, 칸들 in 결과.행
    ) or f"<tr><td colspan='{len(결과.머리) + 1}' class='muted'>줄이 없습니다.</td></tr>"
    return (
        f"<div class='card'><h2>{html.escape(b.제목)}</h2>{경고}"
        f"<div class='scroll'><table {_표모양(b)}"
        f" data-name='{html.escape(b.제목 or '표')}'>"
        f"<tr>{머리}</tr>{몸}</table></div></div>"
    )


def _표모양(b) -> str:
    """블록에 정한 모양을 표 태그의 class·style 로.

    기본은 가로줄만 있는 조용한 표다. 그런데 **줄이 길어지면 칸 구분이 안 된다**
    — 이름 옆의 학력이 어디까지인지 눈으로 못 자른다. 그럴 때 격자를 켠다.
    """
    cls = ["dtbl", f"b-{ {'격자': 'grid', '없음': 'none'}.get(b.테두리, 'row') }"]
    if b.줄무늬:
        cls.append("zebra")
    if b.촘촘히:
        cls.append("tight")
    if b.표너비 != "창에 맞춤":
        cls.append("fit")
    style = ""
    if b.머리배경:
        # 머리글 배경은 CSS 변수로 넘긴다 (인라인 스타일은 th 에 못 닿는다)
        style += f";--headbg:{b.머리배경}"
    style = style.strip(";")
    return (f"class='{' '.join(cls)}'"
            + (f" style='{html.escape(style)}'" if style else ""))


def _dash_view_page(did: int, me: User) -> bytes:
    d = boards.get(did)
    if d is None:
        return _page("없음", "<div class='card'>대시보드를 찾을 수 없습니다.</div>", me=me)
    rows = 대시보드_행()
    축값 = 대시보드_축()
    아는열 = 대시보드_열()
    블록들 = boards.blocks(did)
    몸 = "".join(_블록그리기(b, rows, 축값, 아는열) for b in 블록들)
    if not 블록들:
        몸 = ("<div class='card'><p class='muted'>블록이 없습니다. "
              f"<a href='/dash/edit?id={did}'>편집</a> 에서 추가하세요.</p></div>")
    return _page(
        d.이름,
        f"<div class='card'><h2>{html.escape(d.이름)}"
        f"<span class='muted'> {html.escape(d.설명)}</span></h2>"
        f"<p><a class='btn sec' href='/dash'>목록</a> "
        f"<a class='btn sec' href='/dash/edit?id={did}'>편집</a> "
        f"<span class='muted'>인재 Pool {len(rows.지원자)}명 · 채용 중 "
        f"{len(rows.채용)}명 기준 · {html.escape(now_kst().strftime('%Y-%m-%d %H:%M'))}"
        "</span></p></div>" + 몸,
        me=me,
        폭=d.폭,
    )


def _칸키(행: str, 열: str) -> str:
    """자유 표 칸 하나를 가리키는 키. 탭으로 잇는다 (열 이름에 쉼표가 있어도 안전)."""
    return f"{행}\t{열}"


def _수식도움() -> str:
    """수식을 처음 보는 사람이 읽을 안내. 화면에 늘 붙여 둔다."""
    return (
        "<details><summary class='muted'>수식 쓰는 법 (누르면 펼쳐집니다)</summary>"
        "<div style='margin-top:8px'>"
        "<p class='muted'>모양은 하나뿐입니다 — <code>=함수(대상, 조건...)</code>. "
        "<b>SQL 이 아닙니다.</b> 할 수 있는 일이 정해져 있어 안전합니다.</p>"
        "<table><tr><th>함수</th><th>뜻</th><th>예</th></tr>"
        "<tr><td>COUNT</td><td>몇 명</td><td><code>=COUNT(채용, 부서=\"차세대공정\")</code></td></tr>"
        "<tr><td>PCT</td><td>같은 대상 대비 비율(%)</td><td><code>=PCT(채용, 최종상태~\"*합격\")</code></td></tr>"
        "<tr><td>AVG SUM MIN MAX</td><td>숫자 열을 셈</td><td><code>=AVG(지원자, 저널_수)</code></td></tr>"
        "<tr><td>LIST</td><td>이름 나열</td><td><code>=LIST(채용, 부서=\"소재분석\")</code></td></tr>"
        "</table>"
        "<p class='muted' style='margin-top:8px'><b>대상</b>은 "
        "<code>지원자</code>(인재 Pool 전체) 또는 <code>채용</code>(채용 시작한 사람).<br>"
        "<b>조건</b>은 <code>열=\"값\"</code> <code>열~\"패턴*\"</code> "
        "<code>열!~\"패턴*\"</code> <code>열&gt;숫자</code> <code>열!=\"값\"</code> — "
        "쉼표로 이으면 전부 만족(AND).<br>"
        "<b>열 이름</b>은 <a href='/fields'>표 항목</a> 에 있는 그대로 씁니다. "
        "없는 이름을 쓰면 저장할 때 막습니다.</p>"
        "<p class='muted'><b>와일드카드</b>는 <code>*</code>(아무 글자 몇 개든) 와 "
        "<code>?</code>(한 글자) 입니다. <code>=COUNT(채용, 부서=\"*\")</code> 는 "
        "부서를 안 가리고 전부 셉니다. 별표 그 글자를 찾을 때는 "
        "<code>~*</code> 로 적습니다.</p>"
        "<p class='muted'><code>=</code> 로 시작하지 않으면 그냥 글자로 들어갑니다.</p>"
        "<div class='warn'><b>패턴 하나만 조심하세요.</b> "
        '<code>최종상태~"*합격"</code> 은 <b>불합격도 맞습니다</b> '
        "(글자 그대로 '합격' 으로 끝나니까요). 합격만 세려면 "
        '<code>=COUNT(채용, 최종상태~"*합격", 최종상태!~"*불합격")</code> '
        "처럼 빼는 조건을 같이 쓰세요.</div>"
        "</div></details>"
    )


#: 수식 도움말에 넣을 보기. (수식, 나오는 모양, 설명)
_수식보기: list[tuple[str, str, str]] = [
    ('=박사_학교 & " " & 박사_전공', "서울대학교 기계공학", "& 로 잇습니다"),
    ('=TEXT(박사_졸업,"\'yy.m")', "'26.2", "m 은 한 자리 — <b>08 이 8 로</b>"),
    ('=TEXT(박사_졸업,"\'yy.mm")', "'26.02", "mm 은 두 자리"),
    ('=TEXT(박사_졸업,"yyyy.mm")', "2026.02", "연도를 네 자리로"),
    ('=TEXT(박사_졸업,"yyyy년 m월")', "2026년 2월", "서식 밖의 글자는 그대로"),
    ('=TEXT(박사_시작,"\'yy.m") & "~" & TEXT(박사_졸업,"\'yy.m")',
     "'22.2~'26.2", "기간은 두 번 써서 잇습니다"),
    ('=IF(석사_학교="","",석사_학교)', "(석사가 없으면 빈칸)", "IF 로 갈라 씁니다"),
    ('=TEXTJOIN(" / ", TRUE, 박사_학교, 석사_학교, 학사_학교)',
     "서울대학교 / 포항공대", "<b>TRUE 가 빈 값을 건너뜁니다</b>"),
    ('=IF(박사_석박통합="석박통합","석/박)","박)")', "석/박)", "값에 따라 앞말을 바꿉니다"),
    ('=YEAR(TODAY())-VALUE(LEFT(생년월일,4))', "27", "나이"),
    ('=한글_이름 & "(" & 저널_주저자_수 & "편)"', "홍길동(4편)", "숫자도 그냥 이어집니다"),
    ('=부서="*"', "TRUE", "<b>* 는 아무거나</b> — 부서를 안 가리고 전부"),
    ('=최종상태="*합격"', "TRUE", "'합격' 으로 끝나는 것 (불합격도 맞습니다)"),
    ('=한글_이름="김?"', "TRUE", "<b>? 는 딱 한 글자</b> — 김＊ 두 글자 이름"),
    ('=박사_학교 & CHAR(10) & 석사_학교', "서울대학교↵포항공대",
     "<b>줄바꿈은 CHAR(10)</b> — 입력칸이 한 줄이라 엔터는 안 됩니다"),
    ('=TEXTJOIN(CHAR(10), TRUE, 박사_학교, 석사_학교, 학사_학교)',
     "서울대학교↵포항공대", "여러 줄로 쌓되 빈 건 건너뜁니다"),
]


def _틀도움() -> str:
    """수식 쓰는 법. **엑셀 함수 이름 그대로**라 새로 외울 게 없다.

    예전에는 `{열}` 자리표시자 틀뿐이었다. 배우기는 쉬웠지만 형식을 바꾸려면
    그때마다 새 조각(`{날짜2:…}`)을 만들어 붙여야 해서, 쓰는 사람이 스스로
    넓힐 수가 없었다. 옛 틀도 그대로 돌아가니 쓰던 건 안 고쳐도 된다.
    """
    보기 = "".join(
        f"<tr><td><code>{html.escape(수식)}</code></td>"
        f"<td>{html.escape(결과)}</td><td class='muted'>{설명}</td></tr>"
        for 수식, 결과, 설명 in _수식보기
    )
    함수들 = [
        ("글자", "TEXT TEXTJOIN CONCAT LEFT RIGHT MID LEN TRIM "
                "SUBSTITUTE UPPER LOWER REPT CHAR CODE"),
        ("판단", "IF IFS AND OR NOT IFERROR ISBLANK"),
        ("숫자", "VALUE ROUND INT ABS MIN MAX SUM"),
        ("날짜", "TEXT YEAR MONTH DAY TODAY DATEDIF"),
    ]
    목록 = "".join(
        f"<tr><td>{갈래}</td><td><code>{html.escape(이름들)}</code></td></tr>"
        for 갈래, 이름들 in 함수들
    )
    서식 = "".join(
        f"<tr><td><code>{코드}</code></td><td>{보임}</td></tr>"
        for 코드, 보임 in [("yyyy", "2026"), ("yy", "26"), ("mm", "02"),
                          ("m", "2"), ("dd", "03"), ("d", "3")]
    )
    return (
        "<details><summary class='muted'>수식 쓰는 법 — 엑셀과 같습니다</summary>"
        "<div style='margin-top:10px'>"
        "<p class='muted'><code>=</code> 로 시작하면 <b>엑셀 수식</b>입니다. "
        "함수 이름도 규칙도 엑셀 그대로라 새로 외울 게 없습니다. "
        "열 이름은 <b>표 항목</b> 탭에 있는 그 이름을 그대로 적습니다 "
        "(띄어쓰기가 있으면 <code>[이름]</code> 처럼 대괄호로).</p>"
        "<table><tr><th style='width:44%'>이렇게 쓰면</th><th>이렇게 나옵니다</th>"
        f"<th></th></tr>{보기}</table>"
        "<h3 style='font-size:13px;margin:14px 0 6px'>TEXT 서식 코드 "
        "<span class='muted'>202602 기준</span></h3>"
        f"<table><tr><th style='width:80px'>코드</th><th>나오는 모양</th></tr>{서식}</table>"
        "<h3 style='font-size:13px;margin:14px 0 6px'>쓸 수 있는 함수</h3>"
        f"<table><tr><th style='width:80px'>갈래</th><th>이름</th></tr>{목록}</table>"
        "<p class='muted' style='margin-top:10px'><b>줄바꿈은 "
        "<code>CHAR(10)</code> 입니다.</b> 수식 입력칸이 한 줄짜리라 엔터를 칠 수 "
        "없어서, 엑셀과 같이 글자를 번호로 넣습니다. 줄바꿈을 넣은 칸만 표에서 "
        "줄이 바뀌고, 나머지 칸은 한 줄로 잘린 채 둡니다 — 줄 높이가 들쭉날쭉해지면 "
        "표를 훑기 어렵습니다.</p>"
        "<p class='muted'><b>빈 줄은 사라집니다.</b> "
        "한 줄이 통째로 빈 글자면 그 줄은 안 나옵니다 — 석사를 안 한 사람 "
        "프로필에 빈 석사 줄이 남지 않습니다. 줄 안에서 일부만 비게 하려면 "
        "<code>IF</code> 나 <code>TEXTJOIN(…, TRUE, …)</code> 을 쓰세요.</p>"
        "<p class='muted'><b>와일드카드는 <code>*</code> 와 <code>?</code> 입니다.</b> "
        "엑셀과 같습니다 — <code>*</code> 는 아무 글자 몇 개든(0개도), "
        "<code>?</code> 는 딱 한 글자. 행 고르기에 "
        "<code>=부서=&quot;*&quot;</code> 라고 적으면 부서를 안 가리고 전부 "
        "나옵니다(부서가 비어 있어도 나옵니다). 별표나 물음표 <b>그 글자</b>를 "
        "찾을 때는 <code>~*</code> <code>~?</code> 로 적습니다. "
        "대소문자는 가리지 않습니다.</p>"
        "<p class='muted'><b>예전 방식도 그대로 됩니다.</b> "
        "<code>{박사_학교} {박사_전공}({기간:박사_시작~박사_졸업})</code> 처럼 "
        "<code>=</code> 없이 <code>{}</code> 로 적으면 빈 값이 붙은 괄호까지 "
        "알아서 빠집니다. 쓰던 양식은 안 고쳐도 됩니다.</p>"
        "</div></details>"
    )


#: 블록 종류마다 다른 보기. 빈 칸에 "무엇을 적으라는 거지" 가 없어야 한다.
_초안예시들 = {
    "목록": "예) 채용 중인 사람, 이름과 학력과 주저자 논문 수. 논문 많은 순으로",
    "축표": "예) 부서별로 단계마다 몇 명인지",
    "숫자": "예) 최종 합격한 사람 수",
    "프로필": "예) 공정 부서 지원자마다 학력·경력·논문 실적 한 장씩",
    "표": "예) 첫 줄은 전체 인원, 둘째 줄은 합격 인원. 열은 부서별로",
    "글": "예) 이 대시보드는 매주 월요일 채용 회의에 쓴다는 안내",
}


def _초안예시(종류: str) -> str:
    return _초안예시들.get(종류, "예) 무엇을 만들지 적으세요")


def _조건서식편집(b, 미리볼사람: str = "") -> str:
    """값에 따라 칠하기. **엑셀의 조건부 서식과 같은 감각**이다.

    규칙을 여러 개 둘 수 있고 위에서부터 보다가 처음 맞는 것을 쓴다. 칸 규칙이
    줄 규칙을 이긴다 — 더 좁게 가리킨 쪽이 이긴다.
    """
    열이름 = [머리 or 식 for 머리, 식, _폭 in b.목록열 if str(식).strip()]
    고를것 = [ROW_TARGET] + 열이름

    def 줄(r: dict) -> str:
        대상 = r.get("대상") or ROW_TARGET
        옵션 = "".join(
            f"<option{' selected' if v == 대상 else ''}>{html.escape(v)}</option>"
            for v in dict.fromkeys(고를것 + ([대상] if 대상 not in 고를것 else []))
        )
        return (
            "<tr><td><input type='text' name='cfwhen'"
            f" value='{html.escape(r.get('조건') or '')}' style='width:100%'"
            " class='fx' oninput='fxPreview(this)'"
            f" data-cid='{html.escape(미리볼사람)}'"
            ' placeholder=\'=최종상태="불합격"\'>'
            "<span class='fxout muted'></span></td>"
            f"<td class='ctl'><select name='cfwhere'>{옵션}</select></td>"
            "<td class='ctl'><input type='color' name='cfbg'"
            f" value='{html.escape(r.get('배경') or '#fee2e2')}'></td>"
            # 체크박스는 켠 것만 전송돼서 줄과 짝을 지을 수 없다. 고르개는
            # 언제나 값을 보내므로 줄 차례가 그대로 유지된다.
            "<td class='ctl'><select name='cffgmode'>"
            + f"<option{'' if r.get('글자') else ' selected'}>기본</option>"
            + f"<option{' selected' if r.get('글자') else ''}>직접</option>"
            + "</select> <input type='color' name='cffg'"
            f" value='{html.escape(r.get('글자') or '#16191d')}'></td>"
            "<td class='ctl'><button type='button' class='sec tiny'"
            " onclick='rowMove(this,-1)' title='위로'>↑</button> "
            "<button type='button' class='sec tiny' onclick='rowMove(this,1)'"
            " title='아래로'>↓</button> "
            "<button type='button' class='danger ghost tiny'"
            " onclick='rowDrop(this)' title='이 규칙 빼기'>×</button></td></tr>"
        )

    빈줄 = {"조건": "", "대상": ROW_TARGET, "배경": "#fee2e2", "글자": ""}
    return (
        "<details class='draft'><summary>값에 따라 칠하기</summary>"
        "<div class='scroll' style='margin-top:8px'><table>"
        "<tr><th>이 조건이 참이면</th><th class='ctl' style='width:150px'>어디를</th>"
        "<th class='ctl' style='width:70px'>배경</th>"
        "<th class='ctl' style='width:160px'>글자색</th>"
        "<th class='ctl' style='width:120px'></th></tr>"
        + "".join(줄(r) for r in (b.조건서식 + [빈줄]))
        + "</table></div>"
        "<p class='muted'>규칙은 <b>위에서부터</b> 보다가 처음 맞는 것을 씁니다. "
        "칸 규칙이 줄 규칙을 이깁니다. 조건은 문장 수식과 같은 문법이고 "
        "<b>참/거짓</b>을 냅니다 — "
        "<code>=최종상태=&quot;불합격&quot;</code>, "
        "<code>=저널_주저자_수&gt;=5</code>, "
        "<code>=AND(검토_필요=&quot;Y&quot;, 부서=&quot;공정&quot;)</code>. "
        "맨 아래 빈 줄에 적으면 규칙이 늘어납니다.</p>"
        "</details>"
    )


def _표모양편집(b) -> str:
    """표 모양 고르개. 목록·축표·자유표가 함께 쓴다.

    기본은 가로줄만 있는 조용한 표인데, 줄이 길어지면 **칸 구분이 안 된다** —
    이름 옆의 학력이 어디까지인지 눈으로 못 자른다. 그럴 때 격자를 켠다.
    """
    고르기 = lambda 이름, 값들, 지금: (
        f"<select name='{이름}'>" + "".join(
            f"<option{' selected' if v == 지금 else ''}>{html.escape(v)}</option>"
            for v in 값들) + "</select>"
    )
    return (
        "<details class='draft'><summary>표 모양</summary>"
        "<p class='bar' style='margin-top:8px'>"
        "<label class='rt-lbl'>테두리 "
        + 고르기("border", ("가로줄", "격자", "없음"), b.테두리) + "</label>"
        "<label class='rt-lbl'>표 너비 "
        + 고르기("tablewidth", ("창에 맞춤", "내용에 맞춤"), b.표너비) + "</label>"
        "<label class='rt-lbl'><input type='checkbox' name='zebra'"
        + (" checked" if b.줄무늬 else "") + "> 줄무늬</label>"
        "<label class='rt-lbl'><input type='checkbox' name='tight'"
        + (" checked" if b.촘촘히 else "") + "> 촘촘히</label>"
        "<label class='rt-lbl'>머리글 배경 "
        f"<input type='color' name='headbg' value='{html.escape(b.머리배경 or '#f7f8fa')}'>"
        "</label>"
        "<label class='rt-lbl'><input type='checkbox' name='headbgoff'"
        + ("" if b.머리배경 else " checked") + "> 기본색</label>"
        "</p>"
        "<p class='muted'>칸 구분이 안 되면 <b>격자</b>를 켜세요 (진한 검정 실선). "
        "줄이 많으면 <b>줄무늬</b>가, 한 화면에 더 담고 싶으면 <b>촘촘히</b>가 "
        "도움이 됩니다.</p>"
        "<p class='muted'><b>내용에 맞춤</b>은 칸을 억지로 줄이지 않고, 넘치면 "
        "<b>가로로 스크롤</b>합니다 — 열이 많을 때 이쪽이 읽힙니다 (목록은 기본). "
        "<b>창에 맞춤</b>은 화면 폭을 나눠 갖느라 글자가 잘립니다.</p>"
        "</details>"
    )


def _블록편집(b, 축값, 미리볼사람: str = "") -> str:
    """블록 하나의 설정 폼.

    문장 칸에는 **실제 지원자 한 명의 값으로 미리보기**가 붙는다. 저장하고
    대시보드로 가서 확인하는 왕복이 없으면 수식을 고칠 엄두가 안 난다.
    """
    옵션 = lambda 값들, 지금: "".join(
        f"<option{' selected' if v == 지금 else ''}>{html.escape(v)}</option>"
        for v in 값들
    )
    # 빈 화면에서 시작하는 건 어느 블록이든 부담스럽다. 초안이 있으면 **고치는
    # 일**이 되고, 고치는 건 훨씬 쉽다. 그래서 **모든 종류**에 붙인다.
    #
    # 이 폼은 블록 폼 **밖에** 둔다. 폼 안에 폼을 넣으면 브라우저가 안쪽을
    # 버리면서 바깥 폼도 그 자리에서 끊겨, 아래 칸들이 통째로 안 넘어간다.
    앞머리 = (
        "<details class='draft'><summary>말로 적어서 초안 만들기</summary>"
        "<form method='post' action='/dash/block/draft' style='margin-top:8px'>"
        f"<input type='hidden' name='id' value='{b.id}'>"
        "<p><input type='text' name='말' style='width:100%'"
        f" placeholder='{html.escape(_초안예시(b.종류))}'></p>"
        "<p><button type='submit'>초안 만들기</button> "
        "<span class='muted'>지금 내용을 <b>덮어씁니다.</b> 만든 뒤 보고 고쳐서 "
        "저장하세요 — 저장하기 전에는 아무것도 바뀌지 않습니다.</span></p>"
        "</form></details>"
    )
    머리 = (
        f"<form method='post' action='/dash/block/save' id='bf{b.id}'>"
        f"<input type='hidden' name='id' value='{b.id}'>"
        f"<p><b>{html.escape(b.종류)}</b> "
        f"<input type='text' name='title' value='{html.escape(b.제목)}'"
        " placeholder='블록 제목' style='width:280px'></p>"
    )
    꼬리 = (
        "<p><button type='submit'>이 블록 저장</button></form> "
        "<form method='post' action='/dash/block/move' style='display:inline'>"
        f"<input type='hidden' name='id' value='{b.id}'>"
        "<button class='sec' name='dir' value='-1'>↑</button> "
        "<button class='sec' name='dir' value='1'>↓</button></form> "
        "<form method='post' action='/dash/block/delete' style='display:inline'"
        " onsubmit=\"return confirm('이 블록을 지웁니다.')\">"
        f"<input type='hidden' name='id' value='{b.id}'>"
        "<button class='danger'>블록 삭제</button></form></p>"
    )

    if b.종류 == "글":
        가운데 = (f"<textarea name='text' rows='4' style='width:100%'>"
               f"{html.escape(b.글)}</textarea>")
    elif b.종류 == "숫자":
        가운데 = (
            f"<p><input type='text' name='formula' value='{html.escape(b.수식)}'"
            " style='width:100%' class='fx' data-kind='agg'"
            " oninput='fxPreview(this)' placeholder='=COUNT(채용)'>"
            "<span class='fxout muted'></span></p>"
            f"<p>형식 <select name='format'>{옵션(CELL_FORMATS, b.설정.get('형식') or '그대로')}"
            "</select></p>"
        )
    elif b.종류 == "축표":
        가운데 = (
            f"<p>행 축 <select name='rowaxis'>{옵션(AXIS_SOURCES, b.행축)}</select> "
            f"열 축 <select name='colaxis'>{옵션(AXIS_SOURCES, b.열축)}</select> "
            f"형식 <select name='format'>{옵션(CELL_FORMATS, b.설정.get('형식') or '그대로')}"
            "</select></p>"
            "<p class='muted'>축을 <b>직접 입력</b> 으로 두면 아래 칸에 쉼표로 적은 값을 씁니다.</p>"
            f"<p><input type='text' name='rows' value='{html.escape(', '.join(b.행이름))}'"
            " placeholder='행 (직접 입력일 때만)' style='width:48%'> "
            f"<input type='text' name='cols' value='{html.escape(', '.join(b.열이름))}'"
            " placeholder='열 (직접 입력일 때만)' style='width:48%'></p>"
            f"<p>칸 수식<br><input type='text' name='cellformula'"
            f" value='{html.escape(b.칸수식)}' style='width:100%'"
            f" class='fx' data-kind='agg' data-bid='{b.id}'"
            " oninput='fxPreview(this)'"
            " placeholder='=COUNT(채용, 부서=\"{행}\", 최종상태~\"{열}*\")'>"
            "<span class='fxout muted'></span></p>"
            "<p class='muted'><code>{행}</code> <code>{열}</code> 이 축 값으로 바뀝니다. "
            "칸을 하나하나 안 적어도 되고, 부서가 늘면 표가 알아서 늘어납니다.</p>"
            + _표모양편집(b)
        )
    elif b.종류 == "목록":
        # **한 사람이 한 줄, 열은 만드는 사람이 정한다.** 축표(피벗)로는 만들 수
        # 없는 표가 대부분인데, 사람들이 실제로 만들려는 건 대개 이 목록이다.
        열줄 = "".join(
            f"<tr><td><input type='text' name='colhead' value='{html.escape(머리)}'"
            " style='width:100%' placeholder='머리글'></td>"
            f"<td><input type='text' name='colformula' value='{html.escape(식)}'"
            " style='width:100%' class='fx' oninput='fxPreview(this)'"
            f" data-cid='{html.escape(미리볼사람)}' placeholder='=한글_이름'>"
            "<span class='fxout muted'></span></td>"
            f"<td class='ctl'><input type='number' name='colwidth'"
            f" value='{html.escape(폭)}' min='30' max='900' step='10'"
            " style='width:72px' placeholder='자동'></td>"
            "<td class='ctl'><button type='button' class='sec tiny'"
            " onclick='rowMove(this,-1)' title='위로'>↑</button> "
            "<button type='button' class='sec tiny' onclick='rowMove(this,1)'"
            " title='아래로'>↓</button> "
            "<button type='button' class='danger ghost tiny' onclick='rowDrop(this)'"
            " title='이 열 빼기'>×</button></td></tr>"
            for 머리, 식, 폭 in (b.목록열 + [("", "", "")])
        )
        가운데 = (
            "<p class='bar'>누구를 "
            f"<select name='listtarget'>{옵션(('지원자', '채용'), b.목록대상)}</select>"
            "<input type='text' name='listwhere' class='fx' oninput='fxPreview(this)'"
            f" data-cid='{html.escape(미리볼사람)}'"
            f" value='{html.escape(b.목록조건)}' style='flex:1;min-width:280px'"
            " placeholder=\'=최종상태=&quot;최종 합격&quot;  (*로 아무거나, 비우면 전부)\'>"
            "<span class='fxout muted'></span></p>"
            "<p class='bar'>정렬 "
            "<input type='text' name='listsort' class='fx' oninput='fxPreview(this)'"
            f" data-cid='{html.escape(미리볼사람)}'"
            f" value='{html.escape(b.목록정렬)}' style='flex:1;min-width:240px'"
            " placeholder='=저널_주저자_수  (비우면 그대로)'>"
            "<label class='rt-lbl'><input type='checkbox' name='listdesc'"
            + (" checked" if b.목록내림차순 else "")
            + "> 큰 값부터</label>"
            "<label class='rt-lbl'>최대 <input type='number' name='listmax'"
            f" value='{b.목록최대 or ''}' min='0' style='width:70px'"
            " placeholder='전부'>줄</label>"
            "<span class='fxout muted'></span></p>"
            "<div class='scroll'><table><tr><th style='width:150px'>머리글</th>"
            "<th>열 수식</th>"
            "<th class='ctl' style='width:84px' title='칸 너비 (px). 비우면 알아서'>"
            "너비</th>"
            f"<th class='ctl' style='width:120px'></th></tr>"
            f"{열줄}</table></div>"
            "<p class='muted'>맨 아래 빈 줄에 적으면 열이 늘어납니다. "
            "머리글을 비우면 수식이 그대로 머리글이 됩니다. "
            "<b>수식 대신 그냥 글자</b>를 적으면 모든 줄에 그 글자가 들어갑니다.</p>"
            + _표모양편집(b) + _조건서식편집(b, 미리볼사람)
        )
    elif b.종류 == "프로필":
        줄 = "".join(
            f"<tr><td><input type='text' name='label' value='{html.escape(라벨)}'"
            " style='width:90px'></td>"
            f"<td><input type='text' name='line' value='{html.escape(틀)}'"
            " style='width:100%' class='fx' oninput='fxPreview(this)'"
            f" data-cid='{html.escape(미리볼사람)}'>"
            "<span class='fxout muted'></span></td></tr>"
            for 라벨, 틀 in (b.줄틀 + [("", "")])
        )
        가운데 = (
            f"<p>누구를 <input type='text' name='target'"
            f" value='{html.escape(b.대상조건)}' style='width:100%'"
            " class='fx' data-kind='agg' oninput='fxPreview(this)'"
            " placeholder='=LIST(채용, 부서=\"차세대공정\")'>"
            "<span class='fxout muted'></span></p>"
            "<p class='muted'>조건에 맞는 사람마다 아래 양식이 한 장씩 나옵니다.</p>"
            f"<p>머리 <input type='text' name='head' value='{html.escape(b.머리틀)}'"
            " style='width:100%' class='fx' oninput='fxPreview(this)'"
            f" data-cid='{html.escape(미리볼사람)}'"
            " placeholder='{한글_이름} ({현재_신분})'>"
            "<span class='fxout muted'></span></p>"
            "<div class='scroll'><table><tr><th style='width:90px'>라벨</th>"
            f"<th>문장 틀</th></tr>{줄}</table></div>"
            "<p class='muted'>빈 줄은 저장할 때 없어집니다. 맨 아래 빈 칸에 적으면 줄이 늘어납니다.</p>"
            + _표모양편집(b)
        )
    else:  # 자유 표
        칸입력 = []
        for r in b.행이름:
            칸 = "".join(
                "<td><input type='text' name='cell' value='"
                + html.escape(b.칸.get(_칸키(r, c), ""))
                + "' style='width:150px'></td>"
                for c in b.열이름
            )
            칸입력.append(f"<tr><th style='text-align:left'>{html.escape(r)}</th>{칸}</tr>")
        머리칸 = "<th></th>" + "".join(f"<th>{html.escape(c)}</th>" for c in b.열이름)
        가운데 = (
            f"<p><input type='text' name='rows' value='{html.escape(', '.join(b.행이름))}'"
            " placeholder='행 이름 (쉼표로 구분)' style='width:48%'> "
            f"<input type='text' name='cols' value='{html.escape(', '.join(b.열이름))}'"
            " placeholder='열 이름 (쉼표로 구분)' style='width:48%'></p>"
            f"<p>형식 <select name='format'>{옵션(CELL_FORMATS, b.설정.get('형식') or '그대로')}"
            "</select> <span class='muted'>행·열을 먼저 저장하면 아래 칸이 생깁니다.</span></p>"
            + (f"<div class='scroll'><table><tr>{머리칸}</tr>"
               + "".join(칸입력) + "</table></div>" if b.행이름 and b.열이름 else "")
            + _표모양편집(b)
        )
    return f"<div class='card'>{앞머리}{머리}{가운데}{꼬리}</div>"


def _dash_edit_page(did: int, me: User, error: str = "", msg: str = "") -> bytes:
    d = boards.get(did)
    if d is None:
        return _page("없음", "<div class='card'>대시보드를 찾을 수 없습니다.</div>", me=me)
    축값 = 대시보드_축()
    블록들 = boards.blocks(did)
    종류단추 = "".join(
        f"<button name='kind' value='{k}'>{k}</button> " for k in BLOCK_KINDS
    )
    알림 = _알림(msg=msg)
    오류 = _알림(err=error)

    # 수식 미리보기에 쓸 사람. 실제 값이 보여야 형식을 고칠 수 있다.
    사람들 = store.list_all()[:50]
    미리볼사람 = 사람들[0].지원자_ID if 사람들 else ""
    미리보기고르기 = (
        "<p class='muted'>미리보기 기준 "
        "<select id='fxwho' onchange='fxWhoChanged(this)'>"
        + "".join(
            f"<option value='{html.escape(r.지원자_ID)}'"
            f"{' selected' if r.지원자_ID == 미리볼사람 else ''}>"
            f"{html.escape(r.한글_이름 or r.영문_이름 or r.지원자_ID)}</option>"
            for r in 사람들
        )
        + "</select> — 문장 칸 아래에 <b>이 사람의 값으로</b> 결과가 바로 뜹니다.</p>"
        if 사람들 else
        "<p class='muted'>지원자가 없어 미리보기를 보여줄 수 없습니다.</p>"
    )
    return _page(
        f"{d.이름} 편집",
        알림 + 오류
        + "<div class='card'>"
        f"<h2>{html.escape(d.이름)} 편집</h2>"
        "<form method='post' action='/dash/rename' style='display:flex;gap:8px;flex-wrap:wrap'>"
        f"<input type='hidden' name='id' value='{did}'>"
        f"<input type='text' name='name' value='{html.escape(d.이름)}' style='width:260px'>"
        f"<input type='text' name='desc' value='{html.escape(d.설명)}'"
        " placeholder='설명' style='width:320px'>"
        "<label class='rt-lbl'>화면 폭 <select name='width'>"
        + "".join(f"<option{' selected' if w == (d.너비 or '보통') else ''}>{w}</option>"
                  for w in WIDTHS)
        + "</select></label>"
        "<button type='submit'>이름·설명 저장</button></form>"
        f"<p style='margin-top:10px'><a class='btn' href='/dash/view?id={did}'>보기</a> "
        "<a class='btn sec' href='/dash'>목록</a></p>"
        "<form method='post' action='/dash/block/add' style='margin-top:10px'>"
        f"<input type='hidden' name='dash' value='{did}'>"
        f"<p>블록 추가: {종류단추}</p></form>"
        + _수식도움() + _틀도움() + 미리보기고르기 + "</div>"
        + ("".join(_블록편집(b, 축값, 미리볼사람) for b in 블록들)
           or "<div class='card'><p class='muted'>블록이 없습니다. 위에서 추가하세요.</p></div>")
        + _FX_JS,
        me=me,
    )


#: 문장 칸 아래에 **실제 값으로** 결과를 바로 보여 준다.
#:
#: 저장하고 대시보드로 가서 확인하고 다시 돌아오는 왕복이 있으면, 수식 하나
#: 고치는 데 세 화면이 든다. 그러면 아무도 안 고친다. 서버에 물어보는 이유는
#: 하나다 — **화면과 대시보드가 같은 계산기를 써야** 미리보기를 믿을 수 있다.
_FX_JS = """
<script>
function fxWhoChanged(sel){
  document.querySelectorAll('.fx').forEach(function(el){
    el.dataset.cid = sel.value;
    fxPreview(el);
  });
}
function fxPreview(el){
  /* 타이머를 **칸마다** 둔다. 하나로 두면 화면을 처음 그릴 때 칸들이 서로를
     취소해서 마지막 하나만 살아남는다 (실제로 그랬다 — 미리보기가 통째로
     비어 보였다). */
  clearTimeout(el.__fxt);
  el.__fxt = setTimeout(function(){
    var out = el.parentNode.querySelector('.fxout');
    if(!out) return;
    var 틀 = el.value.trim();
    if(!틀){ out.textContent = ''; out.className = 'fxout muted'; return; }
    var q = '/dash/preview?id=' + encodeURIComponent(el.dataset.cid || '')
          + '&kind=' + (el.dataset.kind || 'row')
          + '&bid=' + (el.dataset.bid || '')
          + '&line=' + encodeURIComponent(틀);
    fetch(q, {credentials: 'same-origin'})
      .then(function(r){ return r.json(); })
      .then(function(res){
        if(res.error){ out.textContent = res.error; out.className = 'fxout flag'; }
        else {
          out.textContent = res.text ? '→ ' + res.text
                                     : '→ (빈 값 — 이 줄은 안 나옵니다)';
          out.className = 'fxout ' + (res.text ? 'fxok' : 'muted');
        }
      })
      .catch(function(){ /* 잠깐 끊긴 것뿐이다 */ });
  }, 250);
}
document.addEventListener('DOMContentLoaded', function(){
  document.querySelectorAll('.fx').forEach(fxPreview);
});
/* 열 순서 바꾸기·빼기. 저장하러 갔다 오지 않고 여기서 끝낸다 — 열 하나 옮기려고
   페이지를 왕복하면 표를 만들 엄두가 안 난다. */
function rowMove(btn, 어디){
  var tr = btn.closest('tr'), 형제 = 어디 < 0 ? tr.previousElementSibling
                                            : tr.nextElementSibling;
  if(!형제 || !형제.querySelector('input')) return;   /* 머리글 줄은 건너뛴다 */
  if(어디 < 0) tr.parentNode.insertBefore(tr, 형제);
  else tr.parentNode.insertBefore(형제, tr);
}
function rowDrop(btn){
  var tr = btn.closest('tr'), 몸 = tr.parentNode;
  tr.querySelectorAll('input').forEach(function(el){ el.value = ''; });
  if(몸.querySelectorAll('tr').length > 2) tr.remove();   /* 빈 줄 하나는 남긴다 */
}
</script>"""


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
        # 헤더는 latin-1 로만 나간다. 한글이 그대로 들어가면 서버가 터진다
        # (`#검토` 같은 조각을 붙였을 때 실제로 그랬다). % 를 안전 문자로 둬서
        # 이미 인코딩된 부분은 두 번 인코딩되지 않게 한다.
        self.send_response(303)
        self.send_header("Location",
                         urllib.parse.quote(location, safe="/?&=#%+:,"))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # -- GET ----------------------------------------------------------------
    def _경로기억(self) -> str:
        """지금 요청 경로를 기억해 둔다 (탭에 불 켜는 데 쓴다)."""
        path = urllib.parse.urlparse(self.path).path
        현재경로.set(path)
        return path

    def do_GET(self) -> None:  # noqa: N802
        path = self._경로기억()

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
                    msg=(params.get("msg") or [""])[0],
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
            return self._send(_candidate_page(cid, me, (params.get("err") or [""])[0],
                                              (params.get("msg") or [""])[0]))
        if path == "/users":
            if not can(me, "계정_현업추가"):
                return self._deny()
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(_users_page(me, (params.get("err") or [""])[0]))
        if path == "/org":
            if not can(me, "부서과제_관리"):
                return self._deny()
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(_org_hub_page(me))
        if path == "/org/edit":
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
            # 이름을 표열 로 두면 do_GET 안에서 모듈 함수 표열() 을 가린다
            # (파이썬은 함수 어디서든 대입이 있으면 그 이름을 지역으로 본다).
            채용열 = store.arrange(recruit.columns())
            이름표 = 라벨(채용열)
            records, _진행, 값 = _recruit_rows(me, (urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).get("sort") or [""])[0])
            rows = [{이름표[c]: 값(rec, c) for c in 채용열} for rec in records]
            stamp = now_kst().strftime("%Y%m%d_%H%M")
            return self._send(
                build_xlsx(rows, [이름표[c] for c in 채용열]),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                extra={"Content-Disposition": f'attachment; filename="recruit_{stamp}.xlsx"'},
            )
        if path == "/match/curate":
            if not can(me, "지원자_등록"):
                return self._deny("과제 파일을 다듬는 건 채용담당자 이상만 할 수 있습니다.")
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(_curate_page(me, (params.get("err") or [""])[0],
                                           (params.get("msg") or [""])[0]))
        if path == "/match":
            # 과제 정보 관리. 부서·과제 탭 아래 화면이다.
            if not can(me, "과제매칭_조회"):
                return self._deny("과제 정보는 채용담당자 이상만 볼 수 있습니다.")
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(_projects_page(me, (params.get("err") or [""])[0],
                                             (params.get("msg") or [""])[0]))
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
        if path == "/mail/test":
            # 메일 탭에서는 **시험 발송까지만** 한다. 실제 발송은 인재 Pool·
            # 채용 현황에서 대상을 고른 뒤에 한다.
            if not can(me, "메일_템플릿"):
                return self._deny()
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                tid = int((params.get("id") or ["0"])[0])
            except ValueError:
                return self._redirect("/mail")
            return self._send(_mail_test_page(tid, me, (params.get("err") or [""])[0],
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
        if path == "/mail/image":
            # 본문 그림. 편집기·미리보기·발송 이력이 이 주소로 그림을 본다.
            if not can(me, "메일_템플릿"):
                return self._deny()
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                img = mailing.body_image(int((params.get("id") or ["0"])[0]))
            except (ValueError, TypeError):
                img = None
            내용 = mailing.body_image_bytes(img["id"]) if img else None
            if 내용 is None:
                return self._send(b"", "image/png", code=404)
            return self._send(
                내용,
                CONTENT_TYPES.get(Path(img["저장명"]).suffix.lower(), "image/png"),
                extra={"Cache-Control": "private, max-age=600"},
            )
        if path == "/mail/log":
            if not can(me, "메일_템플릿"):
                return self._deny()
            return self._send(_mail_log_page(me))
        if path in ("/dash", "/dash/view", "/dash/edit"):
            if not can(me, "대시보드_조회"):
                return self._deny("대시보드는 채용담당자 이상만 볼 수 있습니다.")
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            err = (params.get("err") or [""])[0]
            msg = (params.get("msg") or [""])[0]
            if path == "/dash":
                return self._send(_dash_list_page(me, err, msg))
            try:
                did = int((params.get("id") or ["0"])[0])
            except ValueError:
                return self._redirect("/dash")
            if path == "/dash/view":
                return self._send(_dash_view_page(did, me))
            return self._send(_dash_edit_page(did, me, err, msg))
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
                안본것만=bool((params.get("todo") or [""])[0]),
            ))
        if path == "/dash/preview":
            # 문장 칸 아래 미리보기. 대시보드와 **같은 계산기**를 써야 믿을 수 있다.
            if not can(me, "대시보드_편집"):
                return self._deny()
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            틀 = (params.get("line") or [""])[0]
            cid = (params.get("id") or [""])[0]
            # 집계 문맥(=COUNT(...)) 과 행 문맥(=한글_이름) 은 계산기가 다르다.
            # 화면이 어느 쪽인지 알려 준다 — 글만 보고 맞히려 들면 틀린다.
            if (params.get("kind") or ["row"])[0] == "agg":
                # 축표 칸 수식의 {행}{열} 은 그대로는 계산이 안 된다. 그 블록의
                # **첫 축 값**을 넣어서 한 칸만 미리 계산해 본다.
                if "{행}" in 틀 or "{열}" in 틀:
                    try:
                        b = boards.block(int((params.get("bid") or ["0"])[0]))
                    except (TypeError, ValueError):
                        b = None
                    if b is None:
                        return self._json(
                            {"text": "", "error": "{행}{열} 은 축을 정해야 계산됩니다"})
                    축값 = 대시보드_축()
                    첫행 = (축값.get(b.행축) or b.행이름 or ["(행)"])[0]
                    첫열 = (축값.get(b.열축) or b.열이름 or ["(열)"])[0]
                    보임 = 틀.replace("{행}", 첫행).replace("{열}", 첫열)
                    try:
                        글, _값 = F.run(보임, 대시보드_행(), 대시보드_열())
                    except F.FormulaError as exc:
                        return self._json({"text": "", "error": str(exc)})
                    return self._json(
                        {"text": f"{글}   ({첫행} × {첫열} 칸)", "error": ""})
                try:
                    글, _값 = F.run(틀, 대시보드_행(), 대시보드_열())
                except F.FormulaError as exc:
                    return self._json({"text": "", "error": str(exc)})
                return self._json({"text": 글, "error": ""})
            # 여기부터는 행 문맥 — 한 사람의 값이 있어야 계산할 수 있다.
            if not cid or store.get(cid) is None:
                return self._json({"text": "", "error": "미리볼 지원자가 없습니다"})
            값들 = _프로필값(cid)
            if expr.is_formula(틀):
                글, 오류 = expr.render(틀, 값들)
                return self._json({"text": 글, "error": 오류})
            return self._json({"text": P.render_line(틀, 값들), "error": ""})
        if path == "/status/rows":
            # 현황 표 조각만. 페이지를 통째로 다시 그리면 고르던 파일이 풀린다.
            if not can(me, "지원자_등록"):
                return self._deny()
            return self._send(_status_table().encode("utf-8"))
        if path == "/favicon.ico":
            return self._send(b"", "image/x-icon", code=204)
        if path == "/export.xlsx":
            if not can(me, "엑셀_다운로드"):
                return self._deny()
            # 화면에 걸어 둔 검색 조건을 **그대로** 따른다. 걸러 놓고 받았는데
            # 전체가 나오면 엉뚱한 사람에게 자료가 나간다.
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            records = store.list_filtered(
                (params.get("q") or [""])[0],
                bool(params.get("review")),
                (params.get("year") or [""])[0],
            )
            열 = 표열()
            data = records_to_xlsx(records, registry,
                                   (store.field_names(), _표값맵()),
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
        path = self._경로기억()

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
                    _set_status(safe_name, "대기중", cid=cid)
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

                # 3) '채용 현황' 으로 만든 추가 열
                for 키, 열이름들 in data.items():
                    if not 키.startswith("사용자열_"):
                        continue
                    n = 키[len("사용자열_"):]
                    열이름 = 열이름들[0]
                    정의 = store.field(열이름)
                    if 정의 is None or (정의.get("구분") or "지원자 정보") != "채용 현황":
                        continue
                    앞머리 = f"사용자_{n}_"
                    for k2, 값들2 in data.items():
                        if not k2.startswith(앞머리):
                            continue
                        cid = k2[len(앞머리):]
                        if not 볼수있나(cid):
                            continue
                        try:
                            새값 = validate_custom(정의, 값들2[0])
                        except ValidationError as exc:
                            return self._redirect(
                                "/recruit?err=" + urllib.parse.quote(str(exc)))
                        이전 = store.set_custom(cid, 열이름, 새값)
                        if 이전 != 새값:
                            audit.record(me.아이디, "채용현황", cid, 항목=열이름,
                                         이전값=이전, 새값=새값)
                            바뀐것.append(f"{이름맵.get(cid, cid)} {열이름}")

            # 4) 부서 / 과제 (지원자 수정 권한이 있어야 배정할 수 있다)
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

        if path == "/match/curate":
            if not can(me, "지원자_등록"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            고른키 = set(data.get("keys") or [])
            고른필드 = set(data.get("fields") or [])
            if not 고른키:
                return self._redirect("/match/curate?err=" + urllib.parse.quote(
                    "남길 과제를 하나 이상 고르세요."))
            try:
                항목 = projectsmod.raw_items(projectsmod.read_json(settings.projects_json))
            except projectsmod.ProjectsError as exc:
                return self._redirect("/match/curate?err="
                                      + urllib.parse.quote(str(exc)))
            고른것 = projectsmod.curate(항목, 고른키, 고른필드)
            if not 고른것:
                return self._redirect("/match/curate?err=" + urllib.parse.quote(
                    "고른 조건으로 남는 과제가 없습니다. 필드를 더 고르세요."))
            원본 = projectsmod.resolve_path(settings.projects_json)
            저장위치 = projectsmod.save_curated(
                다듬은파일(), 고른것, 출처=str(원본 or ""), 만든이=me.아이디,
            )
            과제목록(다시=True)
            audit.record(me.아이디, "과제", str(저장위치), 항목="과제 파일 다듬기",
                         새값=f"과제 {len(고른것)}개 · 필드 {len(고른필드)}종")
            return self._redirect("/match/curate?msg=" + urllib.parse.quote(
                f"과제 {len(고른것)}개를 남겨 저장했습니다. 이제 매칭은 이 파일을 씁니다. "
                f"이미 맞춰본 지원자는 '과제 매칭' 에서 다시 돌리세요."))

        if path == "/match/curate/reset":
            if not can(me, "지원자_등록"):
                return self._deny()
            다듬 = 다듬은파일()
            있었나 = 다듬.is_file()
            다듬.unlink(missing_ok=True)
            과제목록(다시=True)
            if 있었나:
                audit.record(me.아이디, "과제", str(다듬), 비고="다듬은 과제 파일 삭제")
            return self._redirect("/match/curate?msg=" + urllib.parse.quote(
                "다듬은 파일을 지웠습니다. 매칭은 다시 원본을 씁니다."
                if 있었나 else "지울 파일이 없습니다."))

        if path == "/match/one":
            if not can(me, "지원자_등록"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            cid = (data.get("id") or [""])[0]
            뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
            rec = store.get(cid)
            if rec is None:
                return self._redirect("/")
            개수, 오류 = 매칭실행(rec, 사용자=me.아이디)
            if 오류:
                return self._redirect(f"{뒤로}&err=" + urllib.parse.quote(오류))
            return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote(
                f"과제 {개수}건과 맞춰봤습니다." if 개수 else "맞춰볼 과제가 없습니다."))

        if path == "/match/all":
            if not can(me, "지원자_등록"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            다시 = bool(data.get("again"))
            목록, 파일오류 = 과제목록(다시=True)
            if 파일오류:
                return self._redirect("/match?err=" + urllib.parse.quote(파일오류))
            이미 = set() if 다시 else set(store.top_matches())
            한것, 실패, 첫오류 = 0, 0, ""
            for rec in store.list_all():
                if rec.지원자_ID in 이미:
                    continue
                개수, 오류 = 매칭실행(rec, 사용자=me.아이디)
                if 오류:
                    실패 += 1
                    첫오류 = 첫오류 or 오류
                elif 개수:
                    한것 += 1
            조각 = [f"{한것}명을 과제와 맞춰봤습니다"]
            if 실패:
                조각.append(f"{실패}명 실패 ({첫오류[:80]})")
            if not 한것 and not 실패:
                조각 = ["새로 맞춰볼 지원자가 없습니다"]
            return self._redirect("/match?msg=" + urllib.parse.quote(" / ".join(조각)))

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
                    그림방식=(data.get("imgmode") or [None])[0],
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
            뒤로 = f"/mail/test?id={tid}"
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

        if path == "/mail/image/add":
            # 편집기가 그림을 넣을 때 부른다. 본문에 base64 를 박는 대신
            # 파일로 보관하고 짧은 참조만 돌려준다.
            if not can(me, "메일_템플릿"):
                return self._json({"ok": False, "error": "권한이 없습니다."}, code=403)
            form = parse_multipart(self._read_body(), self.headers.get("Content-Type", ""))
            try:
                tid = int(form.fields.get("template", "0"))
            except (ValueError, TypeError):
                tid = 0
            if mailing.template(tid) is None:
                return self._json({"ok": False, "error": "템플릿을 찾을 수 없습니다."},
                                  code=404)
            f = form.files[0] if form.files else None
            if f is None or not f.filename:
                return self._json({"ok": False, "error": "그림 파일이 없습니다."}, code=400)
            try:
                img_id = mailing.add_body_image(tid, f.filename, f.content,
                                                올린이=me.아이디)
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, code=400)
            audit.record(me.아이디, "메일", str(tid), 항목="본문 그림 추가",
                         새값=f.filename)
            return self._json({"ok": True, "id": img_id,
                               "src": f"/mail/image?id={img_id}"})

        if path == "/mail/image/delete":
            if not can(me, "메일_템플릿"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            tid = (data.get("template") or ["0"])[0]
            try:
                이름 = mailing.delete_body_image(int((data.get("id") or ["0"])[0]))
            except (ValueError, TypeError):
                이름 = ""
            if 이름:
                audit.record(me.아이디, "메일", tid, 항목="본문 그림 삭제", 이전값=이름)
            return self._redirect(f"/mail/template?id={tid}&msg="
                                  + urllib.parse.quote("본문에서 쓰지 않는 그림을 지웠습니다."
                                                       if 이름 else "그림을 찾을 수 없습니다."))

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

        if path == "/mail/compose":
            # 인재 Pool·채용 현황에서 고른 사람을 데리고 오는 입구.
            if not can(me, "메일_발송"):
                return self._deny("메일 발송 권한이 없습니다.")
            data = urllib.parse.parse_qs(
                self._read_body().decode("utf-8", "replace"), keep_blank_values=True
            )
            ids = [x for x in (data.get("ids") or []) if x.strip()]
            뒤로 = (data.get("back") or ["/"])[0] or "/"
            if not 뒤로.startswith("/"):
                뒤로 = "/"
            try:
                tid = int((data.get("template") or ["0"])[0] or 0)
            except ValueError:
                tid = 0
            return self._send(_mail_compose_page(ids, tid, me, 뒤로))

        if path == "/mail/send":
            if not can(me, "메일_발송"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            try:
                tid = int((data.get("template") or data.get("id") or ["0"])[0])
            except ValueError:
                return self._redirect("/mail")
            tpl = mailing.template(tid)
            if tpl is None:
                return self._redirect("/mail")
            돌아갈곳 = (data.get("back") or ["/"])[0] or "/"
            if not 돌아갈곳.startswith("/"):
                돌아갈곳 = "/"
            ids = [x for x in (data.get("ids") or []) if x.strip()]
            if not ids:
                return self._send(_mail_compose_page(
                    ids, tid, me, 돌아갈곳, "보낼 지원자를 하나 이상 고르세요."))

            # 안전장치: 보낼 인원수를 사람이 직접 쳐야 한다. 화면을 그린 뒤에
            # 상황이 바뀌었을 수 있으니 **지금 다시 세어** 그 수와 맞춰 본다.
            갈사람, _막힌 = _mail_targets(ids, tpl, me)
            친것 = (data.get("confirm") or [""])[0].strip()
            if 친것 != str(len(갈사람)):
                return self._send(_mail_compose_page(
                    ids, tid, me, 돌아갈곳,
                    f"보낼 인원수({len(갈사람)})를 그대로 쳐 넣어야 나갑니다. "
                    f"넣은 값: {친것 or '(빈칸)'}"
                    + ("" if 친것 else "")
                ))
            뒤로 = 돌아갈곳

            진행맵 = recruit.all()
            보이는 = auth.visible_project_ids(me)
            참조 = tpl.cc()
            첨부파일 = mailing.attachment_bytes(tpl.id)
            그림이름 = [f"{i['id']}_{i['파일명']}"
                     for i in mailing.used_body_images(tpl.본문)
                     if tpl.그림보내기 in ("본문+첨부", "첨부만")]
            첨부이름 = ", ".join([이름 for 이름, _ in 첨부파일] + 그림이름)
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
                if not 받는사람:
                    건너뜀 += 1
                    continue
                # 빈 자리표시자는 빈 채로 나간다 (화면에서 이미 알렸다).
                # 본문 그림을 실제로 실을 모양으로 바꾼다. 이력에는 **참조가 든
                # 본문**을 남긴다 — 나중에 다시 열어도 우리 DB 로 그림이 보인다.
                보낼본문, 그림첨부 = mailing.prepare_body(본문, tpl.그림보내기)
                try:
                    결과 = mailapi.send(받는사람, 제목, 보낼본문, html=tpl.html,
                                      참조=참조, 첨부=첨부파일 + 그림첨부)
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

            조각 = [f"'{tpl.이름}' 을 {성공}명에게 보냈습니다"]
            if 실패:
                조각.append(f"{실패}명 실패 ({첫오류[:80]})")
            if 건너뜀:
                조각.append(f"{건너뜀}명은 보낼 수 없어 건너뛰었습니다")
            이음 = "&" if "?" in 뒤로 else "?"
            return self._redirect(뒤로 + 이음 + "msg="
                                  + urllib.parse.quote(" / ".join(조각)))

        if path.startswith("/dash"):
            if not can(me, "대시보드_조회"):
                return self._deny("대시보드는 채용담당자 이상만 다룰 수 있습니다.")
            data = urllib.parse.parse_qs(
                self._read_body().decode("utf-8", "replace"), keep_blank_values=True
            )

            def 정수(키: str, 기본: int = 0) -> int:
                try:
                    return int((data.get(키) or [str(기본)])[0])
                except ValueError:
                    return 기본

            if path == "/dash/add":
                이름 = (data.get("name") or [""])[0]
                try:
                    did = boards.add(이름, 만든이=me.아이디,
                                     설명=(data.get("desc") or [""])[0])
                except ValueError as exc:
                    return self._redirect("/dash?err=" + urllib.parse.quote(str(exc)))
                if data.get("sample"):
                    _예시블록(did)
                audit.record(me.아이디, "대시보드", str(did), 항목="만들기", 새값=이름)
                return self._redirect(f"/dash/edit?id={did}")

            if path == "/dash/rename":
                did = 정수("id")
                try:
                    boards.rename(did, (data.get("name") or [""])[0],
                                  (data.get("desc") or [""])[0])
                except ValueError as exc:
                    return self._redirect(f"/dash/edit?id={did}&err="
                                          + urllib.parse.quote(str(exc)))
                boards.set_width(did, (data.get("width") or [""])[0])
                return self._redirect(f"/dash/edit?id={did}&msg="
                                      + urllib.parse.quote("저장했습니다."))

            if path == "/dash/copy":
                did = 정수("id")
                옛 = boards.get(did)
                if 옛 is None:
                    return self._redirect("/dash")
                for n in range(2, 50):
                    새이름 = f"{옛.이름} 복사본{'' if n == 2 else n}"
                    if not boards.by_name(새이름):
                        break
                새id = boards.copy(did, 새이름, 만든이=me.아이디)
                audit.record(me.아이디, "대시보드", str(새id), 항목="복제", 새값=새이름)
                return self._redirect(f"/dash/edit?id={새id}")

            if path == "/dash/delete":
                이름 = boards.delete(정수("id"))
                if 이름:
                    audit.record(me.아이디, "대시보드", 이름, 비고="대시보드 삭제")
                return self._redirect("/dash?msg=" + urllib.parse.quote(
                    f"'{이름}' 을 지웠습니다." if 이름 else "없는 대시보드입니다."))

            if path == "/dash/block/add":
                did = 정수("dash")
                종류 = (data.get("kind") or [""])[0]
                try:
                    설정 = {"줄": 기본_프로필틀, "머리": "{한글_이름} ({현재_신분})"} \
                        if 종류 == "프로필" else {}
                    boards.add_block(did, 종류, 제목=종류, 설정=설정)
                except ValueError as exc:
                    return self._redirect(f"/dash/edit?id={did}&err="
                                          + urllib.parse.quote(str(exc)))
                return self._redirect(f"/dash/edit?id={did}")

            if path == "/dash/block/move":
                bid = 정수("id")
                b = boards.block(bid)
                boards.move_block(bid, 정수("dir", 1))
                return self._redirect(f"/dash/edit?id={b.dashboard_id if b else 0}")

            if path == "/dash/block/delete":
                bid = 정수("id")
                b = boards.block(bid)
                did = b.dashboard_id if b else 0
                이름 = boards.delete_block(bid)
                if 이름:
                    audit.record(me.아이디, "대시보드", str(did), 항목="블록 삭제",
                                 이전값=이름)
                return self._redirect(f"/dash/edit?id={did}")

            if path == "/dash/block/draft":
                # 말 -> 블록 정의 초안. **LLM 은 값을 만들지 않는다** — 정의만
                # 내고, 표는 언제나 우리 계산기가 그린다.
                bid = 정수("id")
                b = boards.block(bid)
                if b is None:
                    return self._redirect("/dash")
                말 = (data.get("말") or [""])[0]
                설정, 메모 = dash_draft.draft(
                    말, 대시보드_열(), 종류=b.종류,
                    축목록=[a for a in AXIS_SOURCES if a != "직접 입력"],
                )
                뒤로 = f"/dash/edit?id={b.dashboard_id}"
                if not 설정:
                    return self._redirect(
                        f"{뒤로}&err=" + urllib.parse.quote(" / ".join(메모)))
                제목 = 설정.pop("_제목", "") or b.제목 or b.종류
                boards.save_block(bid, 제목=제목, 설정={**b.설정, **설정})
                audit.record(me.아이디, "대시보드", str(b.dashboard_id),
                             항목=f"{b.종류} 초안", 새값=말[:80])
                return self._redirect(
                    f"{뒤로}&msg=" + urllib.parse.quote(" / ".join(메모)))

            if path == "/dash/block/save":
                bid = 정수("id")
                b = boards.block(bid)
                if b is None:
                    return self._redirect("/dash")
                설정, 오류 = _블록설정(b, data)
                if 오류:
                    return self._redirect(f"/dash/edit?id={b.dashboard_id}&err="
                                          + urllib.parse.quote(오류))
                boards.save_block(bid, 제목=(data.get("title") or [""])[0], 설정=설정)
                audit.record(me.아이디, "대시보드", str(b.dashboard_id),
                             항목=f"{b.종류} 블록", 새값=(data.get("title") or [""])[0])
                return self._redirect(f"/dash/edit?id={b.dashboard_id}&msg="
                                      + urllib.parse.quote("블록을 저장했습니다."))
            return self._redirect("/dash")

        if path == "/fields/add":
            if not can(me, "열_구성"):
                return self._deny("표 항목 추가는 관리자만 할 수 있습니다.")
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            이름 = (data.get("name") or [""])[0]
            try:
                구분 = (data.get("scope") or ["지원자 정보"])[0]
                store.add_field(
                    이름,
                    (data.get("type") or ["텍스트"])[0],
                    (data.get("choices") or [""])[0],
                    만든이=me.아이디,
                    구분=구분,
                )
            except ValueError as exc:
                return self._redirect("/fields?err=" + urllib.parse.quote(str(exc)))
            audit.record(me.아이디, "표항목", 이름, 항목="구분", 새값=구분, 비고="열 추가")
            return self._redirect("/fields")

        if path == "/fields/choices":
            # 추가한 열의 선택지 고치기. 형식 검사·추출 스키마와 무관한 열이라
            # 고칠 수 있다. 이미 쓰고 있는 값을 빼면 store 가 거부한다.
            if not can(me, "열_구성"):
                return self._deny("표 항목 설정은 관리자만 바꿀 수 있습니다.")
            data = urllib.parse.parse_qs(
                self._read_body().decode("utf-8", "replace"), keep_blank_values=True
            )
            col = (data.get("col") or [""])[0]
            새선택지 = (data.get("choices") or [""])[0]
            옛것 = store.field(col)
            try:
                store.update_field(col, 선택지=새선택지)
            except ValueError as exc:
                return self._redirect("/fields?err=" + urllib.parse.quote(str(exc)))
            audit.record(me.아이디, "표항목", col, 항목="선택지",
                         이전값=(옛것 or {}).get("선택지", ""), 새값=새선택지.strip())
            return self._redirect("/fields?msg=" + urllib.parse.quote(
                f"'{col}' 선택지를 바꿨습니다."))

        if path == "/recruit/statuses":
            # 단계에서 고를 수 있는 상태 목록. 네 단계가 같은 목록을 쓴다.
            if not can(me, "열_구성"):
                return self._deny("표 항목 설정은 관리자만 바꿀 수 있습니다.")
            data = urllib.parse.parse_qs(
                self._read_body().decode("utf-8", "replace"), keep_blank_values=True
            )
            목록 = [v.strip() for v in (data.get("choices") or [""])[0].split("|")]
            try:
                이전 = recruit.set_statuses(목록)
            except ValueError as exc:
                return self._redirect("/fields?err=" + urllib.parse.quote(str(exc)))
            지금 = recruit.statuses()
            if 지금 != 이전:
                audit.record(me.아이디, "표항목", "단계 상태", 항목="선택지",
                             이전값=" | ".join(x for x in 이전 if x),
                             새값=" | ".join(x for x in 지금 if x))
            return self._redirect("/fields?msg=" + urllib.parse.quote(
                "단계 상태 목록을 바꿨습니다: "
                + ", ".join(x or "(빈칸)" for x in 지금)))

        if path == "/fields/columns":
            if not can(me, "열_구성"):
                return self._deny("표 열 설정은 관리자만 바꿀 수 있습니다.")
            data = urllib.parse.parse_qs(
                self._read_body().decode("utf-8", "replace"), keep_blank_values=True
            )
            # 줄을 끌어 옮기면 폼 칸이 오는 **차례가 바뀐다.** 차례로 짝을 맞추면
            # 5번 줄의 순서 값이 1번 줄에 붙는다 (실제로 그랬다). 그래서 열 이름도
            # 번호를 달아 보내고, 번호로만 짝을 맞춘다.
            열들 = [(int(k.split("_")[1]), v[0])
                  for k, v in data.items()
                  if k.startswith("col_") and k.split("_")[1].isdigit() and v]
            열들.sort()
            이전 = store.column_config()
            바뀐것: list[str] = []
            for i, col in 열들:
                # 추가한 열은 이름과 구분(어느 표에 속하는지)까지 고칠 수 있다.
                # 기본 열은 이 칸을 아예 안 그리므로 여기 걸리지 않는다.
                옛필드 = store.field(col)
                if 옛필드 is not None:
                    새이름 = (data.get(f"rename_{i}") or [col])[0].strip()
                    새구분 = (data.get(f"scope_{i}")
                            or [옛필드.get("구분") or "지원자 정보"])[0]
                    옛구분 = 옛필드.get("구분") or "지원자 정보"
                    if 새이름 != col or 새구분 != 옛구분:
                        try:
                            store.update_field(col, 새이름=새이름, 구분=새구분)
                        except ValueError as exc:
                            return self._redirect(
                                "/fields?err=" + urllib.parse.quote(str(exc)))
                        if 새이름 != col:
                            audit.record(me.아이디, "표항목", 새이름, 항목="열 이름",
                                         이전값=col, 새값=새이름)
                            바뀐것.append(f"{col}→{새이름}")
                        if 새구분 != 옛구분:
                            audit.record(me.아이디, "표항목", 새이름, 항목="구분",
                                         이전값=옛구분, 새값=새구분)
                            바뀐것.append(f"{새이름}(구분 {새구분})")
                        col = 새이름
                새라벨 = (data.get(f"label_{i}") or [""])[0].strip()
                순서값 = (data.get(f"order_{i}") or [""])[0].strip()
                숨김 = f"hide_{i}" in data
                # 관리 정보 열은 설정이 없으면 "숨김" 이 기본이다. 그 상태에서
                # 체크를 풀었으면 바뀐 것으로 봐야 설정이 저장된다.
                옛 = 이전.get(
                    col, {"표시이름": "", "숨김": 기본숨김(col, 이전), "순서": 0}
                )
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
                return self._redirect("/org/edit?err=" + urllib.parse.quote(str(exc)))
            if 옛이름 != 새이름:
                audit.record(me.아이디, "과제", 새이름, 항목="부서명",
                             이전값=옛이름, 새값=새이름)
            return self._redirect("/org/edit")

        if path == "/org/dept/delete":
            if not can(me, "부서과제_관리"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            try:
                auth.delete_department(int((data.get("id") or ["0"])[0]))
            except (ValueError, TypeError):
                pass
            return self._redirect("/org/edit")

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
                return self._redirect("/org/edit?err=" + urllib.parse.quote(str(exc)))
            if 암호.strip():          # 비우면 기존 암호를 그대로 둔다
                auth.set_project_password(pid, 암호)
                audit.record(me.아이디, "과제", 새이름, 비고="초대암호 변경")
            if 옛이름 != 새이름:
                audit.record(me.아이디, "과제", 새이름, 항목="과제명",
                             이전값=옛이름, 새값=새이름)
            return self._redirect("/org/edit")

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

        if path == "/candidate/save":
            # 상세 화면 한 폼 전체. 줄마다 저장 단추가 있으면 하나 고치고
            # 다른 칸으로 넘어갈 때 앞의 수정이 조용히 날아간다.
            if not can(me, "지원자_수정"):
                return self._deny()
            data = urllib.parse.parse_qs(
                self._read_body().decode("utf-8", "replace"), keep_blank_values=True
            )
            cid = (data.get("id") or [""])[0]
            rec = store.get(cid)
            뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
            if rec is None:
                return self._redirect("/")
            try:
                끝 = int((data.get("끝") or ["0"])[0])
            except ValueError:
                끝 = 0

            바뀐것: list[str] = []
            문제: list[str] = []
            레코드바뀜 = False
            for i in range(1, 끝 + 1):
                항목 = (data.get(f"항목_{i}") or [""])[0]
                if not 항목:
                    continue
                새값 = (data.get(f"값_{i}") or [""])[0]
                이전값 = (data.get(f"이전_{i}") or [""])[0]
                if 새값 == 이전값:
                    continue
                구분 = (data.get(f"구분_{i}") or [""])[0]
                if 구분 == "년도":
                    옛 = store.year_of(cid)
                    try:
                        store.set_year(cid, 새값)
                    except ValueError as exc:
                        문제.append(str(exc))
                        continue
                    if 옛 != 새값.strip():
                        바뀐것.append("등록년도")
                        audit.record(me.아이디, "지원자", cid, 항목="등록년도",
                                     이전값=옛, 새값=새값.strip())
                    continue
                if 구분 == "추가":
                    field = store.field(항목)
                    if field is None:
                        continue
                    try:
                        저장값 = validate_custom(field, 새값)
                    except ValidationError as exc:
                        문제.append(str(exc))
                        continue
                    옛값 = store.set_custom(cid, 항목, 저장값)
                    if 옛값 != 저장값:
                        바뀐것.append(항목)
                        audit.record(me.아이디, "지원자", cid, 항목=항목,
                                     이전값=옛값, 새값=저장값)
                    continue
                try:
                    옛값, 저장값 = apply_edit(rec, 항목, 새값, 기대_이전값=이전값,
                                            registry=registry)
                except (ValidationError, ConflictError) as exc:
                    문제.append(str(exc))
                    continue
                if 옛값 != 저장값:
                    레코드바뀜 = True
                    바뀐것.append(항목)
                    audit.record(me.아이디, "지원자", cid, 항목=항목,
                                 이전값=옛값, 새값=저장값)
            if 레코드바뀜:
                store.save(rec)

            if 문제:
                return self._redirect(
                    f"{뒤로}&err=" + urllib.parse.quote(" / ".join(문제[:3]))
                    + "#추출결과")
            if not 바뀐것:
                return self._redirect(f"{뒤로}&msg="
                                      + urllib.parse.quote("바뀐 내용이 없습니다.")
                                      + "#추출결과")
            보임 = ", ".join(바뀐것[:6]) + (" 외" if len(바뀐것) > 6 else "")
            return self._redirect(
                f"{뒤로}&msg=" + urllib.parse.quote(f"{len(바뀐것)}개 저장했습니다 — {보임}")
                + "#추출결과")

        if path in ("/candidate/review/done", "/candidate/review/undo"):
            if not can(me, "지원자_수정"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            cid = (data.get("id") or [""])[0]
            사유 = (data.get("사유") or [""])[0]
            rec = store.get(cid)
            뒤로 = f"/candidate?id={urllib.parse.quote(cid)}#검토"
            if rec is None or not 사유:
                return self._redirect(뒤로)
            끝냄 = path.endswith("/done")
            if 끝냄:
                store.mark_reviewed(cid, 사유, 본사람=me.아이디)
            else:
                store.unmark_reviewed(cid, 사유)
            # 남은 게 없으면 검토_필요를 내린다. 화면·표·엑셀이 같이 따라온다.
            남은 = review.flagged(rec.검토_사유, store.review_done(cid))
            if rec.검토_필요 != 남은:
                rec.검토_필요 = 남은
                store.save(rec)
            audit.record(me.아이디, "지원자", cid, 항목="검토",
                         이전값="" if 끝냄 else "확인함",
                         새값="확인함" if 끝냄 else "",
                         비고=review.short(사유, 80))
            return self._redirect(뒤로)

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
                return self._redirect("/org/edit?err=" + urllib.parse.quote(str(exc)))
            audit.record(me.아이디, "과제", (data.get("name") or [""])[0], 비고="부서 추가")
            return self._redirect("/org/edit")

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
                return self._redirect("/org/edit?err=" + urllib.parse.quote(str(exc)))
            audit.record(me.아이디, "과제", (data.get("name") or [""])[0], 비고="과제 추가")
            return self._redirect("/org/edit")

        if path == "/org/project/delete":
            if not can(me, "부서과제_관리"):
                return self._deny()
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            try:
                auth.delete_project(int((data.get("id") or ["0"])[0]))
            except (ValueError, TypeError):
                pass
            return self._redirect("/org/edit")

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

        if path in ("/candidates/start", "/candidates/stop"):
            # 인재 Pool 에 있는 사람을 채용 현황으로 올리고 내린다.
            # 줄마다 있는 단추는 id 하나, 묶음 단추는 ids 여럿을 보낸다.
            if not can(me, "채용현황_수정"):
                return self._deny("채용 시작은 채용담당자 이상만 할 수 있습니다.")
            data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
            ids = (data.get("ids") or []) + [
                i for i in (data.get("id") or []) if i
            ]
            시작 = path.endswith("/start")
            보이는 = auth.visible_project_ids(me)
            한것: list[str] = []
            for cid in dict.fromkeys(ids):
                if store.get(cid) is None:
                    continue
                if 보이는 is not None and recruit.get(cid).project_id not in 보이는:
                    continue
                바뀜 = (recruit.start(cid, me.아이디) if 시작
                      else recruit.stop(cid, me.아이디))
                if 바뀜:
                    한것.append(cid)
                    audit.record(me.아이디, "채용현황", cid, 항목="채용 절차",
                                 이전값="" if 시작 else "채용 중",
                                 새값="채용 중" if 시작 else "",
                                 비고="채용 시작" if 시작 else "채용 현황에서 내림")
            if not 한것:
                return self._redirect("/?msg=" + urllib.parse.quote(
                    "고를 사람을 먼저 체크하세요." if not ids else "이미 그 상태입니다."))
            말 = (f"{len(한것)}명 채용을 시작했습니다. 채용 현황에서 이어서 관리하세요."
                 if 시작 else f"{len(한것)}명을 채용 현황에서 내렸습니다. "
                             "진행 상황은 지우지 않았습니다.")
            return self._redirect("/?msg=" + urllib.parse.quote(말))

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
            # 재분석하면 사유가 새로 나온다. 옛 '확인함' 기록은 무효다.
            store.clear_reviews(cid)
            _set_status(name, "대기중", "재분석", cid=cid)
            _jobs.put((name, cid, meta["저장_파일명"]))
            return self._redirect("/upload")

        if path == "/status/clear":
            if not can(me, "지원자_등록"):
                return self._deny()
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
            if (data.get("todo") or [""])[0]:
                뒤로 += "&todo=1"

            # 화면에 있던 줄 전부가 들어온다. 실제로 값이 달라진 것만 저장한다.
            바뀐것: list[str] = []
            확인바뀜 = 0
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

                # 확인 표시. 체크칸을 켰거나, **값을 실제로 고쳤으면** 본 것이다.
                # 고쳐 놓고 체크를 깜박하면 그 줄이 영영 '안 본 것' 으로 남는다.
                켬 = bool(data.get(f"확인_{nid}")) or bool(변경)
                if 켬 and not 이전.확인:
                    registry.confirm(nid, 사람=me.아이디)
                    확인바뀜 += 1
                    audit.record(me.아이디, "명칭", f"{kind}:{이후.원표기}",
                                 항목="확인", 이전값="", 새값="확인함")
                elif not 켬 and 이전.확인:
                    registry.unconfirm(nid)
                    확인바뀜 += 1
                    audit.record(me.아이디, "명칭", f"{kind}:{이후.원표기}",
                                 항목="확인", 이전값="확인함", 새값="")

            if not 바뀐것 and 확인바뀜:
                return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote(
                    f"{확인바뀜}줄의 확인 표시를 바꿨습니다."))
            if not 바뀐것:
                return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote("바뀐 내용이 없습니다."))
            보임 = ", ".join(바뀐것[:5]) + (" 외" if len(바뀐것) > 5 else "")
            꼬리 = f" (확인 표시 {확인바뀜}줄)" if 확인바뀜 else ""
            return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote(
                f"{len(바뀐것)}건 저장했습니다 — {보임}{꼬리}"))

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
