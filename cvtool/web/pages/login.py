"""로그인 · 로그아웃.
"""

from __future__ import annotations

import html
import urllib.parse

from ..state import audit, auth
from ..layout import _page, _없는주소, 홈
from ..router import 공개, 라우트, 로그인만


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


@라우트("GET", '/login', 권한=공개)
def get_login(self, me, path):
    return self._send(_login_page())
    return _없는주소(self)


@라우트("GET", '/logout', 권한=공개)
def get_logout(self, me, path):
    auth.end_session(self._token())
    return self._redirect("/login")
    return _없는주소(self)


@라우트("GET", '/favicon.ico', 권한=로그인만)
def get_favicon_ico(self, me, path):
    return self._send(b"", "image/x-icon", code=204)
    return _없는주소(self)


@라우트("POST", '/login', 권한=공개)
def post_login(self, me, path):
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
    return _없는주소(self)
