"""사내 지원자 관리 웹 앱 (표준 라이브러리 http.server) — **조립하는 곳**.

폐쇄망이라 FastAPI/uvicorn 이 없을 수 있어 표준 라이브러리만 쓴다.
실행:  python3 -m cvtool.web.app

이 파일은 요청을 받아 주소표에서 처리 함수를 찾아 부르고(`Handler`), 서버를
띄운다(`main`). 화면은 여기 없다 — 나눠 둔 곳:

    state.py         데이터 폴더 · 저장소(DB)들 · 최초 관리자
    jobs.py          CV 분석 대기열 · 과제 매칭 실행
    columns.py       표의 열 (머리글 · 너비 · 값 · 칸 편집 모양)
    mail_targets.py  아직 메일을 안 보낸 사람
    layout.py        화면 틀 (CSS · 탭 · 알림 · `_page`)
    router.py        주소표 — `@라우트(방법, 주소, 권한=…)`
    pages/*.py       화면별 처리 (파일 하나가 탭 하나)
    static/*.js      화면에 심는 JavaScript
"""

from __future__ import annotations

import html
import importlib
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..auth import _PERMISSIONS as _권한표, can, User
from ..dotenv import candidate_paths, LOADED_FROM
from ..fsutil import is_world_readable, mode_of, secure_dir, secure_file
from ..timeutil import now_kst

from .router import 라우터, 통과

#: 나눈 모듈들. **이 차례가 곧 의존 차례다** — 뒤쪽이 앞쪽을 쓴다 (앞쪽은 뒤쪽을
#: 모른다). 화면 파일을 새로 만들면 여기 넣어야 불리고 주소가 등록된다.
#:
#: 테스트는 새 임시 폴더를 가리킨 뒤 이 모듈을 **다시 읽는다**(reload). 그때 나눈
#: 모듈들도 이 차례대로 다시 읽어야 화면들이 새 DB(`state.store` …)를 가리킨다.
_모듈차례 = (
    "state", "jobs", "columns", "mail_targets", "layout",
    "pages.login", "pages.help", "pages.match", "pages.org", "pages.names", "pages.mail",
    "pages.recruit", "pages.fields", "pages.dashboard", "pages.candidates",
)
_다시읽기 = "_모듈들" in globals()


def _불러오기(이름: str):
    전체 = f"{__package__}.{이름}"
    if _다시읽기 and 전체 in sys.modules:
        return importlib.reload(sys.modules[전체])
    return importlib.import_module(전체)


_모듈들 = [_불러오기(이름) for 이름 in _모듈차례]
# 나누기 전처럼 `app.store` · `app._dash_view_page` 로도 닿게 한다 (테스트·도구가 쓴다).
for _m in _모듈들:
    globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})

from .jobs import _resume_jobs  # noqa: E402
from .layout import _page, _없는주소, 현재경로  # noqa: E402
from .pages.dashboard import _행캐시, _행캐시_잠금  # noqa: E402
from .state import (  # noqa: E402
    audit, auth, bootstrap_admin, DATA_DIR, HOST, PORT, registry, store, WEB_PASSWORD,
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
        self._처리("GET")

    # -- POST ---------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        self._처리("POST")

    def _처리(self, method: str) -> None:
        """주소표(`router.py`)에서 찾아 **로그인·권한을 여기서 한 번** 보고 부른다."""
        path = self._경로기억()
        길 = 라우터.find(method, path)
        # 무엇이든 바꿀 수 있는 요청이다. 미리보기용 줄 캐시를 버린다 — 방금 고친
        # 값이 미리보기에 몇 초 늦게 나오면 고친 게 안 먹은 줄 안다.
        if method == "POST" and not (길 and 길.읽기전용):
            with _행캐시_잠금:
                _행캐시["값"] = None
        me = None
        if 길 is None or 길.로그인필요:
            me = self._user()
            if me is None:
                # 표에서 바로 고치기는 fetch 라 리다이렉트를 받으면 HTML 을 파싱하게 된다.
                if method == "POST" and (path.startswith("/api/") or (길 and 길.json)):
                    return self._json(
                        {"ok": False, "error": "로그인이 풀렸습니다. 새로고침하세요."}, code=401)
                return self._redirect("/login")
        if 길 is None:
            return _없는주소(self)
        if not 통과(me, 길.권한, can):
            if 길.json:
                return self._json({"ok": False, "error": 길.거부말 or "권한이 없습니다."},
                                  code=403)
            return self._deny(길.거부말) if 길.거부말 else self._deny()
        return 길.func(self, me, path)


# 모르는 권한 이름을 쓰는 주소가 있으면 **여기서 터진다** (서버가 안 뜬다).
# `can()` 은 모르는 이름을 관리자 전용으로 보므로, 그냥 두면 조용히 막힌다.
라우터.점검(_권한표)


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
    이어서 = _resume_jobs()
    if 이어서:
        print(f"지난번에 끝내지 못한 CV {이어서}건을 이어서 분석합니다.")
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