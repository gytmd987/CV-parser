"""외부 탭 — 다른 시스템 화면을 이 시스템 탭 줄에 붙인다 (기능은 합치지 않는다).

관리자가 «설정 → 외부 탭» 에서 이름 · 주소 · 열기 방식을 정한다.

    화면 안 : 위쪽 메뉴는 그대로 두고 아래에 그 시스템을 띄운다 (iframe)
    새 창   : 탭을 누르면 그 주소를 새 창으로 연다

탭은 관리자 · 채용담당자에게만 보인다. 저장은 데이터 폴더의 외부탭.json.
"""

from __future__ import annotations

import contextvars
import html
import json
import re
import urllib.parse

from ...auth import can, User
from ..layout import _page, _알림, _없는주소
from ..router import 라우트
from ..state import audit, DATA_DIR

#: 이 권한이 있는 사람에게 탭이 보인다 (관리자 · 채용담당자)
보는권한 = "지원자_목록"
#: 지금 화면 안에 띄운 외부 탭 id (탭에 불 켜는 데 쓴다)
지금외부탭: contextvars.ContextVar[str] = contextvars.ContextVar("지금외부탭", default="")


def _파일():
    return DATA_DIR / "외부탭.json"


def 외부탭들() -> list[dict]:
    """[{id, 이름, 주소, 열기('안' | '새창')}] — 순서대로."""
    try:
        목록 = json.loads(_파일().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [t for t in 목록 if isinstance(t, dict) and t.get("주소")] if isinstance(목록, list) else []


def _저장(목록: list[dict]) -> None:
    _파일().write_text(json.dumps(목록, ensure_ascii=False, indent=1), encoding="utf-8")


def 탭줄(me: User | None) -> list[tuple[str, str, tuple[str, ...], tuple, str]]:
    """layout 이 탭 줄에 붙일 것: (라벨, 주소, 소속, 하위, 새창이면 '_blank')."""
    if not can(me, 보는권한):
        return []
    나온것 = []
    for t in 외부탭들():
        if t.get("열기") == "새창":
            나온것.append((html.escape(t["이름"]) + " ↗", html.escape(t["주소"]), (), (), "_blank"))
        else:
            나온것.append((html.escape(t["이름"]), f"/ext?id={t['id']}", (f"/ext#{t['id']}",), (), ""))
    return 나온것


def _주소확인(주소: str) -> str:
    주소 = (주소 or "").strip()
    if not re.match(r"^https?://[^\s/]+", 주소):
        raise ValueError("주소는 http:// 또는 https:// 로 시작해야 합니다 (예: http://10.0.0.5:8080).")
    return 주소


# --- 보기 ------------------------------------------------------------------------------
@라우트("GET", '/ext', 권한=보는권한, 거부말='외부 탭은 관리자 · 채용담당자만 볼 수 있습니다.')
def get_ext(self, me, path):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    tid = (q.get("id") or [""])[0]
    t = next((t for t in 외부탭들() if str(t["id"]) == tid), None)
    if t is None:
        return _없는주소(self)
    지금외부탭.set(str(t["id"]))
    # 위쪽 메뉴 아래를 꽉 채운다. 메뉴 높이는 화면마다 달라질 수 있어 그려진 뒤 잰다.
    본문 = (
        f"<iframe id='외부' src='{html.escape(t['주소'])}' title='{html.escape(t['이름'])}'"
        " style='position:fixed;left:0;right:0;bottom:0;width:100%;border:0;background:#fff'"
        " allow='clipboard-read; clipboard-write; fullscreen'></iframe>"
        "<script>(function(){var f=document.getElementById('외부'),h=document.querySelector('header');"
        "function 맞춤(){var t=h?h.getBoundingClientRect().bottom:0;f.style.top=t+'px';"
        "f.style.height=(innerHeight-t)+'px';}맞춤();addEventListener('resize',맞춤);})();</script>"
        f"<noscript><a href='{html.escape(t['주소'])}'>{html.escape(t['이름'])} 열기</a></noscript>"
    )
    return self._send(_page(t["이름"], 본문, me=me))
    return _없는주소(self)


# --- 관리 (관리자) --------------------------------------------------------------------
def _폼(t: dict) -> str:
    return f" form='t{t['id']}'"


def _관리화면(me: User, msg: str = "", err: str = "") -> bytes:
    목록 = 외부탭들()

    def 열기고르개(현재: str, 폼: str = "") -> str:
        return (f"<select name='열기'{폼}>"
                + "".join(f"<option value='{v}'{' selected' if v == 현재 else ''}>{l}</option>"
                          for v, l in (("안", "화면 안에 띄우기"), ("새창", "새 창으로 열기")))
                + "</select>")

    줄 = "".join(
        f"<tr><td class='ctl'><form id='t{t['id']}' method='post' action='/admin/tabs/save'>"
        f"<input type='hidden' name='id' value='{t['id']}'></form>"
        f"<input name='이름' form='t{t['id']}' value='{html.escape(t['이름'])}' style='width:140px'></td>"
        f"<td class='ctl'><input name='주소' form='t{t['id']}' value='{html.escape(t['주소'])}' style='width:100%;min-width:260px'></td>"
        f"<td class='ctl'>{열기고르개(t.get('열기', '안'), _폼(t))}</td>"
        f"<td class='ctl'><button form='t{t['id']}'>저장</button> "
        f"<form method='post' action='/admin/tabs/move' style='display:inline'>"
        f"<input type='hidden' name='id' value='{t['id']}'><button class='sec' name='쪽' value='위'"
        f"{' disabled' if i == 0 else ''}>▲</button><button class='sec' name='쪽' value='아래'"
        f"{' disabled' if i == len(목록) - 1 else ''}>▼</button></form> "
        "<form method='post' action='/admin/tabs/delete' style='display:inline'"
        " onsubmit=\"return confirm('이 탭을 지웁니다. 그 시스템은 그대로입니다.')\">"
        f"<input type='hidden' name='id' value='{t['id']}'><button class='danger'>지우기</button></form>"
        + (f" <a href='/ext?id={t['id']}'>열어 보기</a>" if t.get("열기") != "새창" else "")
        + "</td></tr>"
        for i, t in enumerate(목록)) or "<tr><td colspan='4' class='muted'>아직 외부 탭이 없습니다.</td></tr>"
    return _page(
        "외부 탭",
        _알림(msg=msg, err=err)
        + "<div class='card'><h2>외부 탭</h2>"
        "<p class='muted'>다른 시스템 화면을 이 시스템 탭 줄에 붙입니다. <b>기능 · 데이터는 합치지 않고 "
        "보이는 것만</b> 같이 보입니다. 탭은 <b>관리자 · 채용담당자</b>에게만 보입니다.</p>"
        "<p class='muted'><b>화면 안에 띄우기</b>는 위쪽 메뉴를 그대로 두고 아래에 그 시스템을 띄웁니다. "
        "그 시스템이 다른 화면 안에 띄워지는 것을 막아 두었으면(X-Frame-Options · frame-ancestors) 빈 화면이 "
        "나옵니다 — 그때는 «새 창으로 열기» 로 바꾸거나 그 시스템 설정을 고치세요.</p>"
        "<form method='post' action='/admin/tabs/save' class='mergebar'>"
        "<input name='이름' placeholder='탭 이름 (예: 과제 관리)' required style='width:180px'>"
        "<input name='주소' placeholder='http://10.0.0.5:8080' required style='min-width:260px'>"
        + 열기고르개("안") + "<button>탭 추가</button></form></div>"
        + "<div class='card'><h2>탭 목록</h2><table><tr><th>이름</th><th>주소</th><th>열기</th><th></th></tr>"
        + 줄 + "</table></div>",
        me=me)


def _돌아가기(self, msg: str = "", err: str = ""):
    return self._redirect("/admin/tabs?" + ("msg=" + urllib.parse.quote(msg) if msg
                                            else "err=" + urllib.parse.quote(err)))


@라우트("GET", '/admin/tabs', 권한='운영_설정', 거부말='외부 탭은 관리자만 바꿀 수 있습니다.')
def get_admin_tabs(self, me, path):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_관리화면(me, (q.get("msg") or [""])[0], (q.get("err") or [""])[0]))
    return _없는주소(self)


@라우트("POST", '/admin/tabs/save', 권한='운영_설정')
def post_admin_tabs_save(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    값 = {k: (data.get(k) or [""])[0].strip() for k in ("id", "이름", "주소", "열기")}
    if not 값["이름"]:
        return _돌아가기(self, err="탭 이름을 적어 주세요.")
    try:
        주소 = _주소확인(값["주소"])
    except ValueError as exc:
        return _돌아가기(self, err=str(exc))
    열기 = 값["열기"] if 값["열기"] in ("안", "새창") else "안"
    목록 = 외부탭들()
    t = next((t for t in 목록 if str(t["id"]) == 값["id"]), None)
    if t is None:
        t = {"id": max([int(x["id"]) for x in 목록] or [0]) + 1}
        목록.append(t)
        전 = ""
    else:
        전 = f"{t['이름']} · {t['주소']} · {t.get('열기', '안')}"
    t.update(이름=값["이름"], 주소=주소, 열기=열기)
    _저장(목록)
    audit.record(me.아이디, "설정", f"외부탭:{t['id']}", 항목="외부 탭",
                 이전값=전, 새값=f"{t['이름']} · {t['주소']} · {열기}")
    return _돌아가기(self, msg=f"탭 «{t['이름']}» 을 {'고쳤' if 전 else '추가했'}습니다.")
    return _없는주소(self)


@라우트("POST", '/admin/tabs/move', 권한='운영_설정')
def post_admin_tabs_move(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    tid, 쪽 = (data.get("id") or [""])[0], (data.get("쪽") or [""])[0]
    목록 = 외부탭들()
    i = next((i for i, t in enumerate(목록) if str(t["id"]) == tid), None)
    j = None if i is None else (i - 1 if 쪽 == "위" else i + 1)
    if i is not None and j is not None and 0 <= j < len(목록):
        목록[i], 목록[j] = 목록[j], 목록[i]
        _저장(목록)
    return _돌아가기(self, msg="순서를 바꿨습니다.")
    return _없는주소(self)


@라우트("POST", '/admin/tabs/delete', 권한='운영_설정')
def post_admin_tabs_delete(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    tid = (data.get("id") or [""])[0]
    목록 = 외부탭들()
    t = next((t for t in 목록 if str(t["id"]) == tid), None)
    if t is None:
        return _돌아가기(self, err="없는 탭입니다.")
    _저장([x for x in 목록 if x is not t])
    audit.record(me.아이디, "설정", f"외부탭:{t['id']}", 항목="외부 탭",
                 이전값=f"{t['이름']} · {t['주소']}", 새값="", 비고="외부 탭 지움")
    return _돌아가기(self, msg=f"탭 «{t['이름']}» 을 지웠습니다.")
    return _없는주소(self)
