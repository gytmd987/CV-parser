"""계정 · 권한 · 조직(부서/과제) · 세션.

역할은 셋이다.
  관리자       — 전부. 계정·열 구성·명칭 사전·부서/과제 관리
  채용담당자   — 지원자와 채용 현황 전부. 현업 계정만 추가 가능
  현업         — **자기 과제 지원자만** 보고, 그 사람들의 채용 상태·비고만 수정

과제는 부서에 속한다(부서 → 과제). 현업은 과제에 배정된다.

비밀번호는 pbkdf2 로 해시해 저장한다. 표준 라이브러리만 쓴다.
세션도 DB 에 둬서 서버를 재시작해도 로그아웃되지 않는다.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .fsutil import secure_dir, secure_file
from .timeutil import now_kst

ROLES = ("관리자", "채용담당자", "현업")

#: 세션 유지 기간
SESSION_DAYS = 14

_PBKDF2_ROUNDS = 200_000


def hash_password(password: str) -> str:
    """비밀번호를 해시한다. 평문은 어디에도 저장하지 않는다."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, digest_hex = (stored or "").split("$")
        if algo != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


@dataclass
class User:
    아이디: str
    이름: str
    역할: str
    활성: int = 1
    생성일시: str = ""
    생성자: str = ""

    @property
    def is_admin(self) -> bool:
        return self.역할 == "관리자"

    @property
    def is_staff(self) -> bool:
        """관리자 또는 채용담당자."""
        return self.역할 in ("관리자", "채용담당자")


#: 권한 표. 여기 없는 행동은 관리자만 할 수 있다.
_PERMISSIONS: dict[str, tuple[str, ...]] = {
    # 전체 지원자 표. 현업은 배정된 과제의 채용 현황만 보면 되므로 뺀다.
    "지원자_목록": ("관리자", "채용담당자"),
    # 지원자 한 명의 상세. 현업은 **자기 과제 지원자만** (라우트에서 다시 거른다)
    "지원자_조회": ("관리자", "채용담당자", "현업"),
    "지원자_등록": ("관리자", "채용담당자"),
    "지원자_수정": ("관리자", "채용담당자"),
    "지원자_삭제": ("관리자", "채용담당자"),
    "채용현황_수정": ("관리자", "채용담당자", "현업"),  # 현업은 자기 과제만
    # 과제 매칭은 **회사 연구 과제 전체**와 지원자 전원을 나란히 보여준다.
    # 현업에게는 자기 과제 밖의 지원자·과제가 그대로 노출되므로 뺀다.
    "과제매칭_조회": ("관리자", "채용담당자"),
    "명칭_관리": ("관리자", "채용담당자"),
    "부서과제_관리": ("관리자", "채용담당자"),
    "계정_현업추가": ("관리자", "채용담당자"),
    "계정_전체관리": ("관리자",),
    "열_구성": ("관리자",),
    "엑셀_다운로드": ("관리자", "채용담당자"),
    "변경이력_조회": ("관리자", "채용담당자"),
    "메일_템플릿": ("관리자", "채용담당자"),
    "메일_발송": ("관리자", "채용담당자"),
}


def can(user: User | None, action: str) -> bool:
    if user is None or not user.활성:
        return False
    return user.역할 in _PERMISSIONS.get(action, ("관리자",))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    아이디       TEXT PRIMARY KEY,
    이름         TEXT NOT NULL,
    비밀번호      TEXT NOT NULL,
    역할         TEXT NOT NULL,
    활성         INTEGER DEFAULT 1,
    생성일시      TEXT DEFAULT '',
    생성자        TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sessions (
    토큰         TEXT PRIMARY KEY,
    아이디       TEXT NOT NULL REFERENCES users(아이디) ON DELETE CASCADE,
    만료         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS departments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    이름         TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    부서_id      INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    이름         TEXT NOT NULL,
    초대암호      TEXT DEFAULT '',
    UNIQUE(부서_id, 이름)
);
CREATE TABLE IF NOT EXISTS user_projects (
    아이디       TEXT NOT NULL REFERENCES users(아이디) ON DELETE CASCADE,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    PRIMARY KEY (아이디, project_id)
);
"""


class AuthStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        secure_dir(self.path.parent)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        for suffix in ("", "-wal", "-shm"):
            secure_file(Path(str(self.path) + suffix))

    # -- 계정 -------------------------------------------------------------
    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]

    def create_user(
        self, 아이디: str, 이름: str, 비밀번호: str, 역할: str, 생성자: str = ""
    ) -> User:
        아이디 = (아이디 or "").strip()
        if not 아이디:
            raise ValueError("아이디를 입력하세요")
        if 역할 not in ROLES:
            raise ValueError(f"알 수 없는 역할: {역할}")
        if len(비밀번호 or "") < 4:
            raise ValueError("비밀번호는 4자 이상이어야 합니다")
        if self.get_user(아이디):
            raise ValueError(f"이미 있는 아이디입니다: {아이디}")
        self._conn.execute(
            "INSERT INTO users (아이디,이름,비밀번호,역할,활성,생성일시,생성자)"
            " VALUES (?,?,?,?,1,?,?)",
            (아이디, (이름 or 아이디).strip(), hash_password(비밀번호), 역할,
             now_kst().strftime("%Y-%m-%d %H:%M:%S"), 생성자),
        )
        self._conn.commit()
        user = self.get_user(아이디)
        assert user is not None
        return user

    def get_user(self, 아이디: str) -> User | None:
        row = self._conn.execute(
            "SELECT 아이디,이름,역할,활성,생성일시,생성자 FROM users WHERE 아이디=?",
            (아이디,),
        ).fetchone()
        return User(**dict(row)) if row else None

    def list_users(self) -> list[User]:
        rows = self._conn.execute(
            "SELECT 아이디,이름,역할,활성,생성일시,생성자 FROM users"
            " ORDER BY (역할='관리자') DESC, (역할='채용담당자') DESC, 아이디"
        )
        return [User(**dict(r)) for r in rows]

    def set_active(self, 아이디: str, 활성: bool) -> None:
        self._conn.execute("UPDATE users SET 활성=? WHERE 아이디=?", (1 if 활성 else 0, 아이디))
        self._conn.commit()

    def set_password(self, 아이디: str, 비밀번호: str) -> None:
        if len(비밀번호 or "") < 4:
            raise ValueError("비밀번호는 4자 이상이어야 합니다")
        self._conn.execute(
            "UPDATE users SET 비밀번호=? WHERE 아이디=?", (hash_password(비밀번호), 아이디)
        )
        self._conn.commit()

    def delete_user(self, 아이디: str) -> None:
        self._conn.execute("DELETE FROM users WHERE 아이디=?", (아이디,))
        self._conn.commit()

    def authenticate(self, 아이디: str, 비밀번호: str) -> User | None:
        row = self._conn.execute(
            "SELECT 비밀번호, 활성 FROM users WHERE 아이디=?", (아이디,)
        ).fetchone()
        if not row or not row["활성"]:
            return None
        if not verify_password(비밀번호 or "", row["비밀번호"]):
            return None
        return self.get_user(아이디)

    # -- 세션 -------------------------------------------------------------
    def start_session(self, 아이디: str) -> str:
        token = secrets.token_urlsafe(32)
        만료 = (now_kst() + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        self._conn.execute(
            "INSERT INTO sessions (토큰, 아이디, 만료) VALUES (?,?,?)", (token, 아이디, 만료)
        )
        self._conn.commit()
        return token

    def user_for_session(self, token: str) -> User | None:
        if not token:
            return None
        row = self._conn.execute(
            "SELECT 아이디, 만료 FROM sessions WHERE 토큰=?", (token,)
        ).fetchone()
        if not row:
            return None
        if row["만료"] < now_kst().strftime("%Y-%m-%d %H:%M:%S"):
            self.end_session(token)
            return None
        user = self.get_user(row["아이디"])
        return user if user and user.활성 else None

    def end_session(self, token: str) -> None:
        self._conn.execute("DELETE FROM sessions WHERE 토큰=?", (token,))
        self._conn.commit()

    def end_all_sessions(self, 아이디: str) -> None:
        self._conn.execute("DELETE FROM sessions WHERE 아이디=?", (아이디,))
        self._conn.commit()

    # -- 부서 / 과제 -------------------------------------------------------
    def add_department(self, 이름: str) -> int:
        이름 = (이름 or "").strip()
        if not 이름:
            raise ValueError("부서 이름을 입력하세요")
        self._conn.execute("INSERT OR IGNORE INTO departments (이름) VALUES (?)", (이름,))
        self._conn.commit()
        row = self._conn.execute("SELECT id FROM departments WHERE 이름=?", (이름,)).fetchone()
        return row["id"]

    def departments(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM departments ORDER BY 이름"))

    def rename_department(self, dept_id: int, 새이름: str) -> str:
        """부서 이름을 바꾼다. 이전 이름을 돌려준다(이력용)."""
        새이름 = (새이름 or "").strip()
        if not 새이름:
            raise ValueError("부서 이름을 입력하세요")
        row = self._conn.execute(
            "SELECT 이름 FROM departments WHERE id=?", (dept_id,)
        ).fetchone()
        if row is None:
            raise ValueError("없는 부서입니다")
        if row["이름"] == 새이름:
            return 새이름
        겹침 = self._conn.execute(
            "SELECT 1 FROM departments WHERE 이름=? AND id!=?", (새이름, dept_id)
        ).fetchone()
        if 겹침:
            raise ValueError(f"이미 있는 부서 이름입니다: {새이름}")
        self._conn.execute("UPDATE departments SET 이름=? WHERE id=?", (새이름, dept_id))
        self._conn.commit()
        return row["이름"]

    def delete_department(self, dept_id: int) -> None:
        self._conn.execute("DELETE FROM departments WHERE id=?", (dept_id,))
        self._conn.commit()

    def add_project(self, 부서_id: int, 이름: str, 초대암호: str = "") -> int:
        이름 = (이름 or "").strip()
        if not 이름:
            raise ValueError("과제 이름을 입력하세요")
        self._conn.execute(
            "INSERT OR IGNORE INTO projects (부서_id, 이름, 초대암호) VALUES (?,?,?)",
            (부서_id, 이름, hash_password(초대암호) if 초대암호 else ""),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM projects WHERE 부서_id=? AND 이름=?", (부서_id, 이름)
        ).fetchone()
        return row["id"]

    def projects(self, 부서_id: int | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT p.*, d.이름 AS 부서명 FROM projects p"
            " JOIN departments d ON d.id = p.부서_id"
        )
        args: tuple = ()
        if 부서_id is not None:
            sql += " WHERE p.부서_id=?"
            args = (부서_id,)
        sql += " ORDER BY d.이름, p.이름"
        return list(self._conn.execute(sql, args))

    def rename_project(self, project_id: int, 새이름: str) -> str:
        """과제 이름을 바꾼다. 같은 부서 안에서 이름이 겹치면 거부한다."""
        새이름 = (새이름 or "").strip()
        if not 새이름:
            raise ValueError("과제 이름을 입력하세요")
        row = self._conn.execute(
            "SELECT 이름, 부서_id FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise ValueError("없는 과제입니다")
        if row["이름"] == 새이름:
            return 새이름
        겹침 = self._conn.execute(
            "SELECT 1 FROM projects WHERE 부서_id=? AND 이름=? AND id!=?",
            (row["부서_id"], 새이름, project_id),
        ).fetchone()
        if 겹침:
            raise ValueError(f"같은 부서에 이미 있는 과제 이름입니다: {새이름}")
        self._conn.execute("UPDATE projects SET 이름=? WHERE id=?", (새이름, project_id))
        self._conn.commit()
        return row["이름"]

    def set_project_password(self, project_id: int, 초대암호: str) -> None:
        """초대암호를 바꾸거나(값 있음) 지운다(빈 값)."""
        self._conn.execute(
            "UPDATE projects SET 초대암호=? WHERE id=?",
            (hash_password(초대암호) if 초대암호 else "", project_id),
        )
        self._conn.commit()

    def delete_project(self, project_id: int) -> None:
        self._conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        self._conn.commit()

    def check_project_password(self, project_id: int, 암호: str) -> bool:
        row = self._conn.execute(
            "SELECT 초대암호 FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if not row or not row["초대암호"]:
            return False
        return verify_password(암호 or "", row["초대암호"])

    # -- 현업의 과제 배정 ---------------------------------------------------
    def assign(self, 아이디: str, project_id: int) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO user_projects (아이디, project_id) VALUES (?,?)",
            (아이디, project_id),
        )
        self._conn.commit()

    def unassign(self, 아이디: str, project_id: int) -> None:
        self._conn.execute(
            "DELETE FROM user_projects WHERE 아이디=? AND project_id=?", (아이디, project_id)
        )
        self._conn.commit()

    def project_ids_of(self, 아이디: str) -> set[int]:
        rows = self._conn.execute(
            "SELECT project_id FROM user_projects WHERE 아이디=?", (아이디,)
        )
        return {r["project_id"] for r in rows}

    def visible_project_ids(self, user: User) -> set[int] | None:
        """이 사용자가 볼 수 있는 과제. None 이면 제한 없음(전부)."""
        if user.is_staff:
            return None
        return self.project_ids_of(user.아이디)

    def close(self) -> None:
        self._conn.close()
