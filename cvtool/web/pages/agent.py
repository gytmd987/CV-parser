"""운영 설정 · 사내 클코(에이전트) 연결 — 관리자만.

- `/admin/settings` : .env 를 고치지 않고 화면에서 켜고 끄는 운영 항목 (AI 분석 방식,
  메일 연습 모드, 자동 매칭 …). 바꾸면 바로 반영되고 변경 이력에 남는다.
- `/admin/agent`    : 에이전트 토큰 발급 · 끄기 · 삭제, MCP 연결 방법, 클코용 설명서.
- `/mcp`            : MCP (Streamable HTTP, JSON 응답). 토큰으로만 들어온다.
- `/api/agent/call` : 같은 도구를 JSON 으로 ({"tool": …, "args": {…}}) — MCP 가 안 될 때.
"""

from __future__ import annotations

import html
import json
import urllib.parse

from ...config import settings, 운영항목
from ...auth import User
from ..agent_tools import 도구들, 도구오류, 부르기, 설명서
from ..jobs import _resume_jobs
from ..layout import _page, _알림, _없는주소
from ..router import 라우트, 공개
from ..state import audit, auth

#: 받아 주는 MCP 규격 날짜 (클라이언트가 고른 것이 여기 있으면 그대로 답한다)
MCP_버전들 = ("2025-06-18", "2025-03-26", "2024-11-05")


# --- 운영 설정 -------------------------------------------------------------------------
def _보임(이름: str, 값) -> str:
    종류 = next(k for n, _l, k, _d in 운영항목 if n == 이름)
    if 종류 == "bool":
        return "켜짐" if 값 else "꺼짐"
    if 종류.startswith("고르기:"):
        return dict(x.split("=", 1) for x in 종류.split(":", 1)[1].split("|")).get(str(값), str(값))
    return str(값)


def _항목줄(이름: str, 라벨: str, 종류: str, 설명: str, 돌아갈곳: str) -> str:
    지금 = getattr(settings, 이름)
    기본 = getattr(settings.기본값, 이름)

    def 단추(값, 글: str, 켬: bool) -> str:
        return ("<form method='post' action='/admin/settings' style='display:inline'>"
                f"<input type='hidden' name='이름' value='{이름}'>"
                f"<input type='hidden' name='값' value='{html.escape(str(값))}'>"
                f"<input type='hidden' name='뒤로' value='{돌아갈곳}'>"
                f"<button class='{'' if 켬 else 'sec'}' {'disabled' if 켬 else ''}>{html.escape(글)}</button>"
                "</form> ")

    if 종류 == "bool":
        조작 = 단추("1", "켜기", bool(지금)) + 단추("0", "끄기", not 지금)
    elif 종류.startswith("고르기:"):
        조작 = "".join(단추(v, l, str(지금) == v)
                     for v, l in (x.split("=", 1) for x in 종류.split(":", 1)[1].split("|")))
    else:
        조작 = ("<form method='post' action='/admin/settings' style='display:inline'>"
              f"<input type='hidden' name='이름' value='{이름}'>"
              f"<input type='hidden' name='뒤로' value='{돌아갈곳}'>"
              f"<input name='값' value='{html.escape(str(지금))}' style='width:70px'> "
              "<button class='sec'>저장</button></form>")
    출처 = ("<span class='pill p-완료'>화면에서 바꿈</span>" if settings.화면에서_바꿈(이름)
           else "<span class='muted'>.env 값</span>")
    return (f"<tr><td class='ctl'><b>{html.escape(라벨)}</b><br><span class='muted'>{html.escape(설명)}</span></td>"
            f"<td class='ctl'><b>{html.escape(_보임(이름, 지금))}</b><br>{출처}"
            + (f"<br><span class='muted'>.env: {html.escape(_보임(이름, 기본))}</span>"
               if settings.화면에서_바꿈(이름) and 기본 != 지금 else "")
            + f"</td><td class='ctl'>{조작}</td></tr>")


def _설정표(이름들: tuple[str, ...] | None, 돌아갈곳: str) -> str:
    줄 = "".join(_항목줄(n, l, k, d, 돌아갈곳) for n, l, k, d in 운영항목
                if 이름들 is None or n in 이름들)
    return ("<table><tr><th>항목</th><th style='width:170px'>지금</th>"
            f"<th style='width:260px'>바꾸기</th></tr>{줄}</table>")


def _settings_page(me: User, msg: str = "", err: str = "") -> bytes:
    return _page("운영 설정", _알림(msg=msg, err=err)
                 + "<div class='card'><h2>운영 설정</h2>"
                 "<p class='muted'>.env 를 고치지 않고 여기서 켜고 끕니다. 바꾸면 <b>바로</b> 반영되고 "
                 "변경 이력에 남습니다. 비밀번호 · 토큰 · 서버 주소 같은 값은 여전히 .env 에서만 정합니다.</p>"
                 + _설정표(None, "/admin/settings") + "</div>", me=me)


@라우트("GET", '/admin/settings', 권한='운영_설정', 거부말='운영 설정은 관리자만 바꿀 수 있습니다.')
def get_admin_settings(self, me, path):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_settings_page(me, (q.get("msg") or [""])[0], (q.get("err") or [""])[0]))
    return _없는주소(self)


@라우트("POST", '/admin/settings', 권한='운영_설정')
def post_admin_settings(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    이름 = (data.get("이름") or [""])[0]
    값 = (data.get("값") or [""])[0]
    뒤로 = (data.get("뒤로") or ["/admin/settings"])[0]
    if 뒤로 not in ("/admin/settings", "/admin/agent"):
        뒤로 = "/admin/settings"
    라벨 = next((l for n, l, _k, _d in 운영항목 if n == 이름), "")
    if not 라벨:
        return self._redirect(f"{뒤로}?err=" + urllib.parse.quote(f"모르는 항목입니다: {이름}"))
    try:
        바뀐 = settings.바꾸기({이름: 값})
    except ValueError:
        return self._redirect(f"{뒤로}?err=" + urllib.parse.quote(f"{라벨}: 값이 올바르지 않습니다."))
    알림 = f"{라벨}: 그대로입니다."
    if 이름 in 바뀐:
        전, 후 = 바뀐[이름]
        audit.record(me.아이디, "설정", 이름, 항목=라벨, 이전값=_보임(이름, 전), 새값=_보임(이름, 후))
        알림 = f"{라벨}: {_보임(이름, 전)} → {_보임(이름, 후)}"
        if 이름 == "llm_mode" and 후 == "server":
            n = _resume_jobs()        # 클코를 기다리던 CV 를 서버 LLM 이 이어서 분석한다
            if n:
                알림 += f" (기다리던 CV {n}건을 서버 LLM 이 이어서 분석합니다)"
    return self._redirect(f"{뒤로}?msg=" + urllib.parse.quote(알림))
    return _없는주소(self)


# --- 사내 클코 연결 (토큰 · 설명서) --------------------------------------------------------
def _주소(handler) -> str:
    return f"http://{handler.headers.get('Host') or '이-서버-주소:포트'}/mcp"


_켜짐 = {True: "<span class='pill p-완료'>켜짐</span>", False: "<span class='pill p-실패'>꺼짐</span>"}


def _agent_page(handler, me: User, msg: str = "", err: str = "", 새토큰: str = "") -> bytes:
    주소 = _주소(handler)
    토큰줄 = "".join(
        f"<tr><td class='ctl'><b>{html.escape(t['이름'])}</b><br><span class='muted'>{html.escape(t['앞자리'])}…</span></td>"
        f"<td class='ctl'>{html.escape(t['역할'])}</td>"
        f"<td class='ctl'>{html.escape(t['만든때'])}<br><span class='muted'>{html.escape(t['만든이'])}</span></td>"
        f"<td class='ctl'>{html.escape(t['마지막사용'] or '아직 안 씀')}</td>"
        f"<td class='ctl'>{_켜짐[bool(t['활성'])]}</td>"
        "<td class='ctl'><form method='post' action='/admin/agent/token/toggle' style='display:inline'>"
        f"<input type='hidden' name='id' value='{t['id']}'>"
        f"<button class='sec'>{'끄기' if t['활성'] else '켜기'}</button></form> "
        "<form method='post' action='/admin/agent/token/delete' style='display:inline'"
        " onsubmit=\"return confirm('이 토큰을 지웁니다. 이 토큰을 쓰던 클코는 더 이상 접속할 수 없습니다.')\">"
        f"<input type='hidden' name='id' value='{t['id']}'><button class='danger'>삭제</button></form></td></tr>"
        for t in auth.tokens()) or "<tr><td colspan='6' class='muted'>아직 발급한 토큰이 없습니다.</td></tr>"
    새것 = (
        "<div class='card' style='border:2px solid #16a34a'><h2>새 토큰 — 지금 한 번만 보입니다</h2>"
        "<p>아래 토큰을 복사해 사내 클코 설정에 넣으세요. 이 화면을 벗어나면 다시 볼 수 없습니다 "
        "(잃어버리면 지우고 새로 발급).</p>"
        f"<pre style='white-space:pre-wrap;word-break:break-all;font-size:15px'>{html.escape(새토큰)}</pre></div>"
        if 새토큰 else "")
    설정예 = json.dumps({"mcpServers": {"cvtool": {
        "type": "http", "url": 주소, "headers": {"Authorization": "Bearer cvt_발급받은토큰"}}}},
        ensure_ascii=False, indent=2)
    도구줄 = "".join(f"<tr><td class='ctl'><code>{html.escape(t.이름)}</code></td><td class='ctl'>{html.escape(t.설명)}</td></tr>"
                   for t in 도구들.values())
    return _page(
        "사내 클코 연결",
        _알림(msg=msg, err=err) + 새것
        + "<div class='card'><h2>사내 클코 연결</h2>"
        "<p class='muted'>사내 클코(에이전트)가 MCP 로 이 시스템에 붙어 CV 분석 · 과제 매칭 · 조회 · 수정을 합니다. "
        "AI 분석을 클코에게 맡기려면 «AI 분석을 누가 하나» 를 <b>사내 클코</b> 로 두세요 — 그러면 CV 를 "
        "올려도 시스템은 LLM 을 부르지 않고 «클코 대기» 로 쌓아 둡니다. 클코에게 «CV 분석 대기 처리해줘», "
        "«매칭 대기 처리해줘» 라고 시키면 클코가 가져가 처리합니다.</p>"
        + _설정표(("agent_api", "llm_mode"), "/admin/agent") + "</div>"
        + "<div class='card'><h2>토큰</h2>"
        "<p class='muted'>클코 하나에 토큰 하나. 토큰으로 들어온 클코는 <b>채용담당자</b> 권한으로 일하고, "
        "바꾼 것은 변경 이력에 «agent:토큰이름» 으로 남습니다.</p>"
        "<table><tr><th>이름</th><th>권한</th><th>만든 때</th><th>마지막 사용</th><th>상태</th><th></th></tr>"
        + 토큰줄 + "</table>"
        "<form method='post' action='/admin/agent/token' class='mergebar' style='margin-top:12px'>"
        "<input name='이름' placeholder='토큰 이름 (예: 사내클코-채용팀)' required style='min-width:260px'>"
        "<button>토큰 발급</button></form></div>"
        + "<div class='card'><h2>MCP 연결 방법</h2>"
        f"<table><tr><th style='width:150px'>MCP 주소</th><td class='ctl'><code>{html.escape(주소)}</code> "
        "<span class='muted'>(Streamable HTTP)</span></td></tr>"
        "<tr><th>인증</th><td class='ctl'><code>Authorization: Bearer 토큰</code></td></tr></table>"
        "<p class='muted'>클코의 MCP 설정 예 (형식은 사내 클코 안내를 따르세요):</p>"
        f"<pre>{html.escape(설정예)}</pre>"
        "<p class='muted'>명령으로 넣는 경우 예:</p>"
        f"<pre>claude mcp add --transport http cvtool {html.escape(주소)} --header \"Authorization: Bearer cvt_발급받은토큰\"</pre>"
        "<p class='muted'>MCP 를 못 쓰면 같은 도구를 JSON 으로 부를 수 있습니다: "
        f"<code>POST {html.escape(주소[:-4])}/api/agent/call</code> 본문 "
        "<code>{\"tool\": \"status\", \"args\": {}}</code> · 같은 인증 머리.</p></div>"
        + "<div class='card'><h2>클코용 설명서</h2>"
        "<p class='muted'>클코가 접속하면 이 설명서를 받습니다 (guide 도구로도 읽습니다).</p>"
        f"<pre style='white-space:pre-wrap'>{html.escape(설명서)}</pre>"
        "<h3 style='font-size:15px'>도구 목록</h3><table><tr><th>도구</th><th>하는 일</th></tr>"
        + 도구줄 + "</table></div>",
        me=me)


@라우트("GET", '/admin/agent', 권한='에이전트_관리', 거부말='사내 클코 연결은 관리자만 볼 수 있습니다.')
def get_admin_agent(self, me, path):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_agent_page(self, me, (q.get("msg") or [""])[0], (q.get("err") or [""])[0]))
    return _없는주소(self)


def _id(self) -> int:
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    try:
        return int((data.get("id") or ["0"])[0])
    except ValueError:
        return 0


def _토큰이름(token_id: int) -> str:
    return next((t["이름"] for t in auth.tokens() if t["id"] == token_id), "")


@라우트("POST", '/admin/agent/token', 권한='에이전트_관리')
def post_admin_agent_token(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    이름 = (data.get("이름") or [""])[0].strip()
    try:
        토큰 = auth.create_token(이름, 만든이=me.아이디)
    except ValueError as exc:
        return self._redirect("/admin/agent?err=" + urllib.parse.quote(str(exc)))
    audit.record(me.아이디, "토큰", 이름, 비고="에이전트 토큰 발급")
    # 원문은 이 응답에서 한 번만 보여 준다 (주소에 싣지 않는다)
    return self._send(_agent_page(self, me, msg=f"토큰 «{이름}» 을 발급했습니다.", 새토큰=토큰))
    return _없는주소(self)


@라우트("POST", '/admin/agent/token/toggle', 권한='에이전트_관리')
def post_admin_agent_token_toggle(self, me, path):
    tid = _id(self)
    t = next((t for t in auth.tokens() if t["id"] == tid), None)
    if t is None:
        return self._redirect("/admin/agent?err=" + urllib.parse.quote("없는 토큰입니다."))
    auth.set_token_active(tid, not t["활성"])
    audit.record(me.아이디, "토큰", t["이름"], 비고="토큰 " + ("끔" if t["활성"] else "켬"))
    return self._redirect("/admin/agent?msg=" + urllib.parse.quote(
        f"토큰 «{t['이름']}» 을 {'껐' if t['활성'] else '켰'}습니다."))
    return _없는주소(self)


@라우트("POST", '/admin/agent/token/delete', 권한='에이전트_관리')
def post_admin_agent_token_delete(self, me, path):
    tid = _id(self)
    이름 = _토큰이름(tid)
    if not 이름:
        return self._redirect("/admin/agent?err=" + urllib.parse.quote("없는 토큰입니다."))
    auth.delete_token(tid)
    audit.record(me.아이디, "토큰", 이름, 비고="토큰 삭제")
    return self._redirect("/admin/agent?msg=" + urllib.parse.quote(f"토큰 «{이름}» 을 지웠습니다."))
    return _없는주소(self)


# --- MCP · JSON API --------------------------------------------------------------------
def _토큰사용자(self) -> tuple[User | None, str]:
    """(에이전트 사용자, 거절 이유)."""
    if not settings.agent_api:
        return None, "관리자가 에이전트 접속을 꺼 두었습니다 (운영 설정 «에이전트 접속 받기»)."
    머리 = self.headers.get("Authorization", "")
    토큰 = 머리[7:].strip() if 머리.lower().startswith("bearer ") else ""
    me = auth.user_for_api_token(토큰)
    if me is None:
        return None, "토큰이 없거나 틀렸거나 꺼져 있습니다 (Authorization: Bearer cvt_…)."
    return me, ""


def _본문json(self):
    try:
        return json.loads(self._read_body().decode("utf-8") or "null")
    except (UnicodeDecodeError, ValueError):
        return None


def _도구정의() -> list[dict]:
    return [{"name": t.이름, "description": t.설명, "inputSchema": t.입력} for t in 도구들.values()]


def _mcp_하나(me: User, 요청) -> dict | None:
    """JSON-RPC 요청 하나 → 응답 (알림이면 None)."""
    if not isinstance(요청, dict) or 요청.get("jsonrpc") != "2.0" or "method" not in 요청:
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "JSON-RPC 2.0 요청이 아닙니다."}}
    방법, rid, 인자 = 요청["method"], 요청.get("id"), 요청.get("params") or {}
    if "id" not in 요청:                       # 알림 (notifications/initialized 등)
        return None

    def 답(결과: dict) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "result": 결과}

    if 방법 == "initialize":
        버전 = 인자.get("protocolVersion")
        return 답({"protocolVersion": 버전 if 버전 in MCP_버전들 else MCP_버전들[0],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "cvtool", "title": "지원자 관리", "version": "1"},
                  "instructions": 설명서})
    if 방법 == "ping":
        return 답({})
    if 방법 == "tools/list":
        return 답({"tools": _도구정의()})
    if 방법 == "tools/call":
        이름 = str(인자.get("name") or "")
        try:
            결과 = 부르기(me, 이름, 인자.get("arguments") or {})
            return 답({"content": [{"type": "text", "text": json.dumps(결과, ensure_ascii=False)}],
                      "structuredContent": 결과, "isError": False})
        except 도구오류 as exc:
            return 답({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except Exception as exc:  # noqa: BLE001 - 에이전트에게 무엇이 터졌는지 알려 준다
            return 답({"content": [{"type": "text", "text": f"시스템 오류: {type(exc).__name__}: {exc}"}],
                      "isError": True})
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"모르는 방법입니다: {방법}"}}


@라우트("POST", '/mcp', 권한=공개)
def post_mcp(self, me, path):
    me, 이유 = _토큰사용자(self)
    if me is None:
        return self._send(json.dumps({"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32001, "message": 이유}},
                                     ensure_ascii=False).encode("utf-8"),
                          "application/json; charset=utf-8", code=401,
                          extra={"WWW-Authenticate": 'Bearer realm="cvtool"'})
    본문 = _본문json(self)
    if 본문 is None:
        return self._json({"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32700, "message": "JSON 을 읽지 못했습니다."}}, code=400)
    if isinstance(본문, list):
        답들 = [d for d in (_mcp_하나(me, x) for x in 본문) if d is not None]
        if not 답들:
            return self._send(b"", "application/json", code=202)
        return self._send(json.dumps(답들, ensure_ascii=False).encode("utf-8"),
                          "application/json; charset=utf-8")
    답 = _mcp_하나(me, 본문)
    if 답 is None:
        return self._send(b"", "application/json", code=202)
    return self._json(답)
    return _없는주소(self)


@라우트("GET", '/mcp', 권한=공개)
def get_mcp(self, me, path):
    # 서버가 먼저 보내는 알림(SSE)은 없다. 규격대로 405 로 답한다.
    return self._send("이 MCP 서버는 POST 만 받습니다.".encode("utf-8"),
                      "text/plain; charset=utf-8", code=405, extra={"Allow": "POST"})
    return _없는주소(self)


@라우트("POST", '/api/agent/call', 권한=공개)
def post_api_agent_call(self, me, path):
    me, 이유 = _토큰사용자(self)
    if me is None:
        return self._json({"ok": False, "error": 이유}, code=401)
    본문 = _본문json(self)
    if not isinstance(본문, dict):
        return self._json({"ok": False, "error": '본문은 {"tool": "...", "args": {...}} 입니다.'}, code=400)
    try:
        return self._json({"ok": True, "result": 부르기(me, str(본문.get("tool") or ""), 본문.get("args") or {})})
    except 도구오류 as exc:
        return self._json({"ok": False, "error": str(exc)}, code=400)
    return _없는주소(self)
