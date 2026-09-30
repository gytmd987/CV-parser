"""웹 앱이 함께 쓰는 상태 — 데이터 폴더, 저장소(DB)들, 최초 관리자.

`app.py` 가 다시 읽힐 때(테스트) 이 모듈도 **먼저** 다시 읽혀 새 DB 를 가리킨다.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..audit import AuditLog
from ..auth import AuthStore
from ..dashboards import DashboardStore
from ..mailing import MailStore
from ..names import NameRegistry
from ..recruit import RecruitStore
from ..store import CandidateStore


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
# `경력_회사` 가 사전 열이 되기 전에 등록된 사람들의 회사를 사전에 넣는다.
# 안 넣으면 상세 화면의 드롭다운에 그 회사가 없어, 상세를 저장하는 순간
# 회사 이름이 빈칸으로 지워진다. 이관이 끝나 있으면 레코드를 읽지도 않는다.
registry.backfill_careers(store.list_all)
auth = AuthStore(DATA_DIR / "admin.db")
recruit = RecruitStore(DATA_DIR / "recruit.db")
audit = AuditLog(DATA_DIR / "audit.db")
mailing = MailStore(DATA_DIR / "mail.db", DATA_DIR / "mail_files")
boards = DashboardStore(DATA_DIR / "dashboard.db")


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
