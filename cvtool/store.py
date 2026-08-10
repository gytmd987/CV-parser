"""지원자 저장소 (sqlite, 표준 라이브러리).

개인정보가 들어가므로 인사 RAG 의 Postgres/Qdrant 와 절대 섞지 않는다.
파일 하나로 격리되고, 폐쇄망에서 추가 설치가 필요 없다.

보관 기간: 엑셀 열에서는 뺐지만 DB 에는 유지한다. 그래야 자동 삭제가 가능하다.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import settings
from .retention import expiry_date
from .schemas import CVRecord
from .timeutil import now_kst

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    지원자_ID    TEXT PRIMARY KEY,
    등록일시      TEXT NOT NULL,
    원본_파일명   TEXT DEFAULT '',
    보관_만료일   TEXT DEFAULT '',
    record_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expiry ON candidates(보관_만료일);
"""


class CandidateStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def save(self, rec: CVRecord) -> None:
        now = now_kst()
        만료 = expiry_date(now, settings.retention_months).strftime("%Y-%m-%d")
        self._conn.execute(
            "INSERT OR REPLACE INTO candidates"
            " (지원자_ID, 등록일시, 원본_파일명, 보관_만료일, record_json)"
            " VALUES (?,?,?,?,?)",
            (
                rec.지원자_ID,
                now.strftime("%Y-%m-%d %H:%M:%S"),
                rec.원본_파일명,
                만료,
                rec.model_dump_json(),
            ),
        )
        self._conn.commit()

    def list_all(self) -> list[CVRecord]:
        rows = self._conn.execute(
            "SELECT record_json FROM candidates ORDER BY 등록일시 DESC"
        ).fetchall()
        return [CVRecord.model_validate(json.loads(r["record_json"])) for r in rows]

    def get(self, 지원자_ID: str) -> CVRecord | None:
        row = self._conn.execute(
            "SELECT record_json FROM candidates WHERE 지원자_ID=?", (지원자_ID,)
        ).fetchone()
        return CVRecord.model_validate(json.loads(row["record_json"])) if row else None

    def delete(self, 지원자_ID: str) -> None:
        self._conn.execute("DELETE FROM candidates WHERE 지원자_ID=?", (지원자_ID,))
        self._conn.commit()

    def purge_expired(self) -> list[str]:
        """보관 기간이 지난 지원자를 삭제하고 삭제된 ID 를 반환한다."""
        today = now_kst().strftime("%Y-%m-%d")
        rows = self._conn.execute(
            "SELECT 지원자_ID FROM candidates WHERE 보관_만료일 != '' AND 보관_만료일 <= ?",
            (today,),
        ).fetchall()
        ids = [r["지원자_ID"] for r in rows]
        if ids:
            self._conn.executemany(
                "DELETE FROM candidates WHERE 지원자_ID=?", [(i,) for i in ids]
            )
            self._conn.commit()
        return ids

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) c FROM candidates").fetchone()["c"]

    def close(self) -> None:
        self._conn.close()
