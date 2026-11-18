"""새 지원자 알림 메일 — 평일 아침 9시에 전날까지 새로 등록된 지원자를 한 통으로.

- 사람이 올린 것 · 사내 코워크가 등록한 것 모두.
- 새로 등록된 사람이 없으면 보내지 않는다. 주말 · 휴일(관리자가 고치는 목록)에는 쉬고
  다음 근무일 9시에 그동안 쌓인 것을 보낸다.
- 보내는 길은 메일 기능 그대로 (`clients.mail.send`, 운영 설정의 «메일 연습 모드» 를 따른다).
  지원자별 메일 이력에는 남기지 않는다 (지원자에게 간 메일이 아니다).
- 관리자가 «설정 → 알림 메일» 에서 켜고 끄고, 받는 사람 · 제목 · 본문 · 휴일을 정한다.

저장은 데이터 폴더의 알림메일.json. 서버가 9시에 꺼져 있었으면 켜진 뒤 그날 안에 보낸다.
"""

from __future__ import annotations

import html
import json
import threading
import time
import urllib.parse
from datetime import date, datetime

from ...auth import User
from ...clients import mail as mailapi
from ...mailing import split_addresses
from ...matching import 점수등급
from ...timeutil import now_kst
from ..layout import _page, _알림, _없는주소
from ..router import 라우트
from ..state import audit, DATA_DIR, registry, store

보낼시각 = (9, 0)

#: 기본 휴일 (관리자가 화면에서 고친다 — 해마다 확인하세요). 주말은 따로 쉰다.
기본휴일 = """2026-12-25 성탄절
2027-01-01 신정
2027-02-08 설날 연휴
2027-02-09 설날 대체공휴일
2027-03-01 삼일절
2027-05-05 어린이날
2027-05-13 부처님오신날
2027-08-16 광복절 대체공휴일
2027-09-14 추석 연휴
2027-09-15 추석
2027-09-16 추석 연휴
2027-10-04 개천절 대체공휴일
2027-10-11 한글날 대체공휴일
2027-12-27 성탄절 대체공휴일"""

기본제목 = "[지원자 관리] 새 지원자 {건수}명 ({날짜})"

기본본문 = """<p>지난 알림 뒤로 새로 등록된 지원자 <b>{건수}명</b>입니다
(검토 필요 {검토필요}명 · 중복 의심 {중복의심}명).</p>
{지원자표}
<p style="color:#64748b;font-size:12px">이 메일은 지원자 관리 시스템이 평일 오전 9시에 보냅니다.
자세한 내용은 <a href="{시스템주소}/">인재 Pool</a>에서 확인하세요.</p>"""

자리표시 = ("건수", "날짜", "검토필요", "중복의심", "지원자표", "시스템주소")

_잠금 = threading.Lock()


def _파일():
    return DATA_DIR / "알림메일.json"


def 설정() -> dict:
    기본 = {"켜짐": False, "받는사람": "", "제목": 기본제목, "본문": 기본본문, "휴일": 기본휴일,
          "시스템주소": "", "마지막기준": "", "마지막실행일": "", "마지막결과": ""}
    try:
        저장 = json.loads(_파일().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        저장 = {}
    return {**기본, **(저장 if isinstance(저장, dict) else {})}


def _저장(값: dict) -> None:
    _파일().write_text(json.dumps(값, ensure_ascii=False, indent=1), encoding="utf-8")


def 휴일들(설: dict) -> set[date]:
    나온것 = set()
    for 줄 in (설.get("휴일") or "").splitlines():
        글 = 줄.strip()[:10]
        try:
            나온것.add(date.fromisoformat(글))
        except ValueError:
            continue
    return 나온것


def 근무일(날: date, 설: dict) -> bool:
    return 날.weekday() < 5 and 날 not in 휴일들(설)


# --- 내용 만들기 ----------------------------------------------------------------------
def 새지원자(기준: str, 끝: str) -> list:
    """기준 < 등록일시 <= 끝 인 지원자 (등록 순)."""
    return store.registered_between(기준, 끝)


def _상태(rec) -> str:
    사유 = rec.검토_사유 or ""
    if "중복" in 사유:
        return "중복 의심"
    return "검토 필요" if rec.검토_필요 == "Y" else "완료"


def 지원자표(목록: list, 시스템주소: str) -> str:
    칸 = "border:1px solid #e2e8f0;padding:6px 8px;vertical-align:top;font-size:13px"
    머리 = "".join(f"<th style='{칸};background:#f1f5f9;text-align:left'>{h}</th>" for h in (
        "이름", "현재 소속", "박사 학교 · 전공", "상태", "검토 사유", "과제 매칭 1~3순위"))
    줄 = []
    for rec, _등록 in 목록:
        row = rec.to_row(registry)
        이름 = html.escape(row.get("한글_이름") or row.get("영문_이름") or rec.지원자_ID)
        if 시스템주소:
            이름 = (f"<a href='{html.escape(시스템주소)}/candidate?id="
                  f"{urllib.parse.quote(rec.지원자_ID)}'>{이름}</a>")
        박사 = " · ".join(x for x in (row.get("박사_학교"), row.get("박사_전공")) if x)
        매칭 = "<br>".join(
            f"{i}. {html.escape(m['과제명'])} ({점수등급(m['점수'], m['평가됨'])})"
            for i, m in enumerate(store.matches(rec.지원자_ID)[:3], 1)) or "-"
        사유 = (rec.검토_사유 or "").strip()
        사유 = html.escape(사유[:120] + ("…" if len(사유) > 120 else "")) or "-"
        줄.append("<tr>" + "".join(f"<td style='{칸}'>{v}</td>" for v in (
            이름, html.escape(row.get("현재_소속") or "-"), html.escape(박사 or "-"),
            _상태(rec), 사유, 매칭)) + "</tr>")
    return ("<table style='border-collapse:collapse;margin:8px 0'>"
            f"<tr>{머리}</tr>{''.join(줄)}</table>")


def 메일만들기(설: dict, 목록: list, 오늘: date) -> tuple[str, str]:
    값 = {
        "건수": str(len(목록)),
        "날짜": f"{오늘.month}/{오늘.day}",
        "검토필요": str(sum(1 for r, _ in 목록 if _상태(r) == "검토 필요")),
        "중복의심": str(sum(1 for r, _ in 목록 if _상태(r) == "중복 의심")),
        "지원자표": 지원자표(목록, (설.get("시스템주소") or "").rstrip("/")),
        "시스템주소": html.escape((설.get("시스템주소") or "").rstrip("/")),
    }

    def 채우기(글: str) -> str:
        for k, v in 값.items():
            글 = 글.replace("{" + k + "}", v)
        return 글

    return 채우기(설.get("제목") or 기본제목), 채우기(설.get("본문") or 기본본문)


# --- 보내기 ---------------------------------------------------------------------------
def 보내기(지금: datetime | None = None, *, 누가: str = "시스템") -> str:
    """쌓인 새 지원자를 한 통으로 보낸다. 결과 한 줄. (보낼 게 없으면 안 보낸다)"""
    with _잠금:
        설 = 설정()
        지금 = 지금 or now_kst()
        끝 = 지금.strftime("%Y-%m-%d %H:%M:%S")
        받는사람 = split_addresses(설.get("받는사람") or "")
        if not 받는사람:
            결과 = "받는 사람이 없어 보내지 않았습니다."
        else:
            목록 = 새지원자(설.get("마지막기준") or 끝, 끝)
            if not 목록:
                결과 = "새로 등록된 지원자가 없어 보내지 않았습니다."
                설["마지막기준"] = 끝
            else:
                제목, 본문 = 메일만들기(설, 목록, 지금.date())
                try:
                    r = mailapi.send(받는사람[0], 제목, 본문, html=True, 참조=받는사람[1:])
                    결과 = (f"{len(목록)}명 알림을 보냈습니다 → {', '.join(받는사람)}" if r.보냄
                          else f"{len(목록)}명 알림 — 메일 연습 모드라 실제로는 보내지 않았습니다.")
                    설["마지막기준"] = 끝
                except mailapi.MailError as exc:
                    결과 = f"보내지 못했습니다 (다음 근무일에 다시): {exc}"
        설["마지막결과"] = f"{지금.strftime('%Y-%m-%d %H:%M')} {결과}"
        _저장(설)
    if 누가 != "시스템":
        audit.record(누가, "설정", "알림메일", 항목="알림 메일 지금 보내기", 새값=결과)
    return 결과


def 때가됐나(지금: datetime, 설: dict) -> bool:
    오늘 = 지금.date()
    return (bool(설.get("켜짐")) and 근무일(오늘, 설)
            and (지금.hour, 지금.minute) >= 보낼시각
            and 설.get("마지막실행일") != 오늘.isoformat())


def 한번_보기(지금: datetime | None = None) -> str:
    """시계가 부른다: 때가 됐으면 보내고 오늘 했다고 적는다."""
    지금 = 지금 or now_kst()
    설 = 설정()
    if not 때가됐나(지금, 설):
        return ""
    결과 = 보내기(지금)
    설 = 설정()
    설["마지막실행일"] = 지금.date().isoformat()
    _저장(설)
    return 결과


_시계 = {"돌고있음": False}


def 알림시작() -> None:
    """서버가 뜰 때 한 번. 1분마다 시계를 본다."""
    if _시계["돌고있음"]:
        return
    _시계["돌고있음"] = True

    def 돌기():
        while True:
            try:
                한번_보기()
            except Exception as exc:  # noqa: BLE001 - 시계가 죽으면 안 된다
                print(f"알림 메일 오류: {type(exc).__name__}: {exc}")
            time.sleep(60)

    threading.Thread(target=돌기, daemon=True).start()


# --- 관리 화면 ------------------------------------------------------------------------
def _화면(me: User, msg: str = "", err: str = "", 미리: str = "") -> bytes:
    설 = 설정()
    켜짐 = bool(설.get("켜짐"))
    단추 = (
        "<form method='post' action='/admin/notify/toggle' style='display:inline'>"
        f"<button class='{'sec' if 켜짐 else ''}'>{'끄기' if 켜짐 else '켜기'}</button></form>")
    return _page(
        "알림 메일",
        _알림(msg=msg, err=err)
        + "<div class='card'><h2>새 지원자 알림 메일 "
        + ("<span class='pill p-완료'>켜짐</span>" if 켜짐 else "<span class='pill p-실패'>꺼짐</span>")
        + f" {단추}</h2>"
        "<p class='muted'><b>평일 오전 9시</b>(휴일 빼고)에 지난 알림 뒤로 새로 등록된 지원자 — 사람이 올린 것 · "
        "사내 코워크가 등록한 것 모두 — 를 한 통으로 보냅니다. 새로 등록된 사람이 없으면 보내지 않습니다. "
        "메일 연습 모드(운영 설정)가 켜져 있으면 실제로는 나가지 않습니다. 지원자별 메일 이력에는 남기지 않습니다.</p>"
        f"<p class='muted'>마지막: {html.escape(설.get('마지막결과') or '아직 없음')}</p>"
        "<form method='post' action='/admin/notify/save'>"
        "<table>"
        "<tr><th style='width:150px'>받는 사람</th><td class='ctl'>"
        f"<textarea name='받는사람' rows='2' style='width:100%' placeholder='a@corp.com, b@corp.com'>"
        f"{html.escape(설.get('받는사람') or '')}</textarea>"
        "<span class='muted'>쉼표 · 줄바꿈으로 구분. 이름 · 소속이 들어가니 사내 주소만.</span></td></tr>"
        "<tr><th>시스템 주소</th><td class='ctl'>"
        f"<input name='시스템주소' value='{html.escape(설.get('시스템주소') or '')}' style='width:100%'"
        " placeholder='http://10.0.0.3:8600 — 메일 속 이름을 누르면 지원자 상세로'></td></tr>"
        "<tr><th>제목</th><td class='ctl'>"
        f"<input name='제목' value='{html.escape(설.get('제목') or '')}' style='width:100%'></td></tr>"
        "<tr><th>본문 (HTML)</th><td class='ctl'>"
        f"<textarea name='본문' rows='8' style='width:100%;font-family:monospace'>{html.escape(설.get('본문') or '')}"
        "</textarea><span class='muted'>자리표시: "
        + " · ".join("{" + x + "}" for x in 자리표시)
        + " — {지원자표} 는 이름 · 현재 소속 · 박사 학교·전공 · 상태 · 검토 사유 · 과제 매칭 1~3순위 표입니다.</span>"
        "</td></tr>"
        "<tr><th>휴일</th><td class='ctl'>"
        f"<textarea name='휴일' rows='6' style='width:100%;font-family:monospace'>{html.escape(설.get('휴일') or '')}"
        "</textarea><span class='muted'>줄마다 <code>YYYY-MM-DD 이름</code>. 주말은 따로 쉽니다. "
        "대체공휴일 · 임시공휴일은 해마다 확인해 넣어 주세요.</span></td></tr>"
        "</table>"
        "<div class='row' style='margin-top:10px'><button>저장</button> "
        "<button class='sec' formaction='/admin/notify/reset' "
        "onclick=\"return confirm('제목 · 본문을 처음 모양으로 되돌립니다.')\">제목 · 본문 처음으로</button></div>"
        "</form></div>"
        + "<div class='card'><h2>미리 보기 · 지금 보내기</h2>"
        "<form method='post' action='/admin/notify/preview' style='display:inline'>"
        "<button class='sec'>지금 쌓인 것 미리 보기</button></form> "
        "<form method='post' action='/admin/notify/send' style='display:inline'"
        " onsubmit=\"return confirm('지금 쌓인 새 지원자를 받는 사람에게 보냅니다.')\">"
        "<button>지금 보내기</button></form>"
        + (f"<div style='margin-top:12px;border:1px solid var(--line);border-radius:8px;padding:14px'>{미리}</div>"
           if 미리 else "")
        + "</div>",
        me=me)


def _뒤로(self, msg: str = "", err: str = ""):
    return self._redirect("/admin/notify?" + ("msg=" + urllib.parse.quote(msg) if msg
                                              else "err=" + urllib.parse.quote(err)))


@라우트("GET", '/admin/notify', 권한='운영_설정', 거부말='알림 메일은 관리자만 바꿀 수 있습니다.')
def get_admin_notify(self, me, path):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_화면(me, (q.get("msg") or [""])[0], (q.get("err") or [""])[0]))
    return _없는주소(self)


@라우트("POST", '/admin/notify/toggle', 권한='운영_설정')
def post_admin_notify_toggle(self, me, path):
    설 = 설정()
    설["켜짐"] = not 설.get("켜짐")
    if 설["켜짐"] and not 설.get("마지막기준"):
        # 처음 켤 때는 지금부터 — 그동안 등록된 사람 전부가 첫 메일에 실리지 않게
        설["마지막기준"] = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    _저장(설)
    audit.record(me.아이디, "설정", "알림메일", 항목="새 지원자 알림 메일",
                 이전값="꺼짐" if 설["켜짐"] else "켜짐", 새값="켜짐" if 설["켜짐"] else "꺼짐")
    return _뒤로(self, msg="알림 메일을 " + ("켰습니다. 평일 오전 9시에 보냅니다." if 설["켜짐"] else "껐습니다."))
    return _없는주소(self)


@라우트("POST", '/admin/notify/save', 권한='운영_설정')
def post_admin_notify_save(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"), keep_blank_values=True)
    설 = 설정()
    잘못 = [a for a in split_addresses((data.get("받는사람") or [""])[0]) if "@" not in a]
    if 잘못:
        return _뒤로(self, err=f"메일 주소가 아닙니다: {', '.join(잘못)}")
    주소 = (data.get("시스템주소") or [""])[0].strip().rstrip("/")
    if 주소 and not 주소.startswith(("http://", "https://")):
        return _뒤로(self, err="시스템 주소는 http:// 또는 https:// 로 시작해야 합니다.")
    전 = dict(설)
    for k in ("받는사람", "제목", "본문", "휴일"):
        설[k] = (data.get(k) or [""])[0].strip()
    설["시스템주소"] = 주소
    _저장(설)
    for k in ("받는사람", "시스템주소", "제목", "본문", "휴일"):
        if (전.get(k) or "") != (설.get(k) or ""):
            audit.record(me.아이디, "설정", "알림메일", 항목=f"알림 메일 {k}",
                         이전값=(전.get(k) or "")[:300], 새값=(설.get(k) or "")[:300])
    return _뒤로(self, msg="저장했습니다.")
    return _없는주소(self)


@라우트("POST", '/admin/notify/reset', 권한='운영_설정')
def post_admin_notify_reset(self, me, path):
    self._read_body()
    설 = 설정()
    설["제목"], 설["본문"] = 기본제목, 기본본문
    _저장(설)
    audit.record(me.아이디, "설정", "알림메일", 항목="알림 메일 제목 · 본문", 새값="처음 모양으로")
    return _뒤로(self, msg="제목 · 본문을 처음 모양으로 되돌렸습니다.")
    return _없는주소(self)


@라우트("POST", '/admin/notify/preview', 권한='운영_설정', 읽기전용=True)
def post_admin_notify_preview(self, me, path):
    설 = 설정()
    지금 = now_kst()
    끝 = 지금.strftime("%Y-%m-%d %H:%M:%S")
    목록 = 새지원자(설.get("마지막기준") or 끝, 끝)
    if not 목록:
        return self._send(_화면(me, msg="지금 쌓인 새 지원자가 없습니다 (마지막 알림 뒤로 등록된 사람 없음)."))
    제목, 본문 = 메일만들기(설, 목록, 지금.date())
    return self._send(_화면(me, 미리=f"<p><b>제목:</b> {html.escape(제목)}</p>{본문}"))
    return _없는주소(self)


@라우트("POST", '/admin/notify/send', 권한='운영_설정')
def post_admin_notify_send(self, me, path):
    self._read_body()
    return _뒤로(self, msg=보내기(누가=me.아이디))
    return _없는주소(self)
