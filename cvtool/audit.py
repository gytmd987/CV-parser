"""변경 이력.

누가 · 언제 · 무엇을 · 어떻게 바꿨는지 남긴다.
값을 덮어쓰기 전의 값도 함께 저장해서, 나중에 되짚어볼 수 있게 한다.

지원자 항목뿐 아니라 계정·명칭 사전·채용 상태 변경도 같은 표에 쌓는다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .fsutil import secure_dir, secure_file
from .timeutil import now_kst

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    일시         TEXT NOT NULL,
    사용자        TEXT NOT NULL,
    대상종류      TEXT NOT NULL,   -- 지원자 / 계정 / 명칭 / 채용현황 / 과제 ...
    대상          TEXT NOT NULL,   -- 지원자_ID 등
    항목          TEXT DEFAULT '',
    이전값        TEXT DEFAULT '',
    새값          TEXT DEFAULT '',
    비고          TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_대상 ON audit(대상종류, 대상, id DESC);
CREATE INDEX IF NOT EXISTS idx_audit_일시 ON audit(id DESC);
"""


@dataclass
class Entry:
    id: int
    일시: str
    사용자: str
    대상종류: str
    대상: str
    항목: str
    이전값: str
    새값: str
    비고: str

    def summary(self) -> str:
        if self.항목 and (self.이전값 or self.새값):
            이전 = self.이전값 or "(빈칸)"
            새 = self.새값 or "(빈칸)"
            return f"{self.항목}: {이전} → {새}"
        return self.비고 or self.항목 or "-"


class AuditLog:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        secure_dir(self.path.parent)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        for suffix in ("", "-wal", "-shm"):
            secure_file(Path(str(self.path) + suffix))

    def record(
        self,
        사용자: str,
        대상종류: str,
        대상: str,
        *,
        항목: str = "",
        이전값: str = "",
        새값: str = "",
        비고: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO audit (일시,사용자,대상종류,대상,항목,이전값,새값,비고)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                now_kst().strftime("%Y-%m-%d %H:%M:%S"),
                사용자 or "(알수없음)",
                대상종류,
                대상,
                항목,
                str(이전값 or ""),
                str(새값 or ""),
                비고,
            ),
        )
        self._conn.commit()

    def for_target(self, 대상종류: str, 대상: str, limit: int = 200) -> list[Entry]:
        rows = self._conn.execute(
            "SELECT * FROM audit WHERE 대상종류=? AND 대상=? ORDER BY id DESC LIMIT ?",
            (대상종류, 대상, limit),
        )
        return [Entry(**dict(r)) for r in rows]

    def recent(self, limit: int = 200, 사용자: str = "", 대상종류: str = "") -> list[Entry]:
        sql = "SELECT * FROM audit WHERE 1=1"
        args: list = []
        if 사용자:
            sql += " AND 사용자=?"
            args.append(사용자)
        if 대상종류:
            sql += " AND 대상종류=?"
            args.append(대상종류)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [Entry(**dict(r)) for r in self._conn.execute(sql, args)]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"]

    def close(self) -> None:
        self._conn.close()
