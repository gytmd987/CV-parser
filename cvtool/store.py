"""지원자 저장소 (sqlite, 표준 라이브러리).

개인정보가 들어가므로 인사 RAG 의 Postgres/Qdrant 와 절대 섞지 않는다.
파일 하나로 격리되고, 폐쇄망에서 추가 설치가 필요 없다.

보관 기간: 엑셀 열에서는 뺐지만 DB 에는 유지한다. 그래야 자동 삭제가 가능하다.

원문 텍스트 보관은 기본으로 꺼져 있다(개인정보 최소 수집). 켜면 재업로드 없이
재분석할 수 있지만, CV 전문이 DB 에 남는다. CVTOOL_STORE_CV_TEXT 로 정한다.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import settings
from .fsutil import secure_dir, secure_file
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

# 나중에 추가된 열. 기존 DB 에도 없으면 붙인다.
_ADDED_COLUMNS = {
    "원문_텍스트": "TEXT DEFAULT ''",
    "저장_파일명": "TEXT DEFAULT ''",
}

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}


class CandidateStore:
    """지원자 레코드와 **CV 원본 파일**을 함께 소유한다.

    파일을 별도로 두면 삭제·보관기간이 어긋나 개인정보가 남는다.
    삭제·만료 처리는 반드시 DB 행과 원본 파일을 함께 지운다.
    """

    def __init__(self, db_path: str | Path, files_dir: str | Path | None = None) -> None:
        self.path = Path(db_path)
        # 개인정보가 들어가는 DB 다. 다른 계정이 읽지 못하게 권한을 조인다.
        secure_dir(self.path.parent)
        self.files_dir = secure_dir(
            Path(files_dir) if files_dir else self.path.parent / "files"
        )
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        # sqlite 가 만든 -wal/-shm 파일에도 같은 내용이 들어간다
        for suffix in ("", "-wal", "-shm"):
            secure_file(Path(str(self.path) + suffix))

    def _migrate(self) -> None:
        existing = {
            r["name"] for r in self._conn.execute("PRAGMA table_info(candidates)")
        }
        for col, decl in _ADDED_COLUMNS.items():
            if col not in existing:
                self._conn.execute(f"ALTER TABLE candidates ADD COLUMN {col} {decl}")

    # -- 원본 파일 ---------------------------------------------------------
    def store_file(self, 지원자_ID: str, 원본_파일명: str, content: bytes) -> str:
        """CV 원본을 보관하고 저장된 파일명을 반환한다.

        파일명은 지원자_ID 로 짓는다. 사용자가 올린 이름을 그대로 쓰면
        중복·충돌이 나고 파일명 자체가 개인정보(이름)를 노출한다.
        """
        suffix = Path(원본_파일명).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            suffix = ""
        저장명 = f"{지원자_ID}{suffix}"
        dest = self.files_dir / 저장명
        dest.write_bytes(content)
        secure_file(dest)
        return 저장명

    def file_path(self, 지원자_ID: str) -> Path | None:
        """보관된 원본 경로. 없으면 None."""
        row = self._conn.execute(
            "SELECT 저장_파일명 FROM candidates WHERE 지원자_ID=?", (지원자_ID,)
        ).fetchone()
        if not row or not row["저장_파일명"]:
            return None
        path = self.files_dir / row["저장_파일명"]
        return path if path.is_file() else None

    def _unlink_files(self, ids: list[str]) -> None:
        """DB 행을 지우기 전에 원본 파일부터 지운다."""
        for cid in ids:
            path = self.file_path(cid)
            if path:
                path.unlink(missing_ok=True)

    # -- 쓰기 -------------------------------------------------------------
    def save(self, rec: CVRecord, 원문_텍스트: str = "", 저장_파일명: str | None = None) -> None:
        now = now_kst()
        만료 = expiry_date(now, settings.retention_months).strftime("%Y-%m-%d")
        보관할_원문 = 원문_텍스트 if settings.store_cv_text else ""
        if 저장_파일명 is None:  # 재분석 등에서 기존 값을 유지
            row = self._conn.execute(
                "SELECT 저장_파일명 FROM candidates WHERE 지원자_ID=?", (rec.지원자_ID,)
            ).fetchone()
            저장_파일명 = row["저장_파일명"] if row else ""
        self._conn.execute(
            "INSERT OR REPLACE INTO candidates"
            " (지원자_ID, 등록일시, 원본_파일명, 보관_만료일, record_json, 원문_텍스트, 저장_파일명)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                rec.지원자_ID,
                now.strftime("%Y-%m-%d %H:%M:%S"),
                rec.원본_파일명,
                만료,
                rec.model_dump_json(),
                보관할_원문,
                저장_파일명 or "",
            ),
        )
        self._conn.commit()

    def delete(self, 지원자_ID: str) -> bool:
        self._unlink_files([지원자_ID])
        cur = self._conn.execute(
            "DELETE FROM candidates WHERE 지원자_ID=?", (지원자_ID,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_many(self, ids: list[str]) -> int:
        if not ids:
            return 0
        self._unlink_files(ids)
        marks = ",".join("?" * len(ids))
        cur = self._conn.execute(
            f"DELETE FROM candidates WHERE 지원자_ID IN ({marks})", ids
        )
        self._conn.commit()
        return cur.rowcount

    def delete_all(self) -> int:
        rows = self._conn.execute("SELECT 지원자_ID FROM candidates").fetchall()
        self._unlink_files([r["지원자_ID"] for r in rows])
        cur = self._conn.execute("DELETE FROM candidates")
        self._conn.commit()
        return cur.rowcount

    def orphan_files(self) -> list[Path]:
        """DB 에 대응하는 행이 없는 파일 (크래시 등으로 남은 것)."""
        known = {
            r["저장_파일명"]
            for r in self._conn.execute("SELECT 저장_파일명 FROM candidates")
            if r["저장_파일명"]
        }
        return [f for f in self.files_dir.iterdir() if f.is_file() and f.name not in known]

    def purge_expired(self) -> list[str]:
        """보관 기간이 지난 지원자를 삭제하고 삭제된 ID 를 반환한다."""
        today = now_kst().strftime("%Y-%m-%d")
        rows = self._conn.execute(
            "SELECT 지원자_ID FROM candidates WHERE 보관_만료일 != '' AND 보관_만료일 <= ?",
            (today,),
        ).fetchall()
        ids = [r["지원자_ID"] for r in rows]
        self.delete_many(ids)
        return ids

    # -- 읽기 -------------------------------------------------------------
    def _row_to_record(self, row: sqlite3.Row) -> CVRecord:
        return CVRecord.model_validate(json.loads(row["record_json"]))

    def list_all(self) -> list[CVRecord]:
        rows = self._conn.execute(
            "SELECT record_json FROM candidates ORDER BY 등록일시 DESC"
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_filtered(self, q: str = "", review_only: bool = False) -> list[CVRecord]:
        """이름·소속·학교·파일명으로 검색하고, 검토 필요만 골라 본다."""
        records = self.list_all()
        if review_only:
            records = [r for r in records if r.검토_필요 == "Y"]
        term = q.strip().lower()
        if term:
            def hit(r: CVRecord) -> bool:
                haystack = " ".join(
                    [
                        r.한글_이름, r.영문_이름, r.이메일, r.전화번호,
                        r.현재_소속, r.박사_학교, r.석사_학교, r.학사_학교,
                        r.원본_파일명, r.지원자_ID,
                    ]
                ).lower()
                return term in haystack

            records = [r for r in records if hit(r)]
        return records

    def get(self, 지원자_ID: str) -> CVRecord | None:
        row = self._conn.execute(
            "SELECT record_json FROM candidates WHERE 지원자_ID=?", (지원자_ID,)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def meta(self, 지원자_ID: str) -> dict | None:
        """엑셀에는 없지만 관리에 필요한 값들 (등록일시·만료일·원본 보유 여부)."""
        row = self._conn.execute(
            "SELECT 등록일시, 원본_파일명, 보관_만료일, 저장_파일명,"
            " (원문_텍스트 IS NOT NULL AND 원문_텍스트 != '') AS 원문보유"
            " FROM candidates WHERE 지원자_ID=?",
            (지원자_ID,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["원본보유"] = self.file_path(지원자_ID) is not None
        return data

    def get_text(self, 지원자_ID: str) -> str:
        """재분석용 원문. 보관하지 않았으면 빈 문자열."""
        row = self._conn.execute(
            "SELECT 원문_텍스트 FROM candidates WHERE 지원자_ID=?", (지원자_ID,)
        ).fetchone()
        return (row["원문_텍스트"] or "") if row else ""

    def expiry_map(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT 지원자_ID, 보관_만료일 FROM candidates")
        return {r["지원자_ID"]: r["보관_만료일"] or "" for r in rows}

    def expired_count(self) -> int:
        today = now_kst().strftime("%Y-%m-%d")
        row = self._conn.execute(
            "SELECT COUNT(*) c FROM candidates WHERE 보관_만료일 != '' AND 보관_만료일 <= ?",
            (today,),
        ).fetchone()
        return row["c"]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) c FROM candidates").fetchone()["c"]

    def close(self) -> None:
        self._conn.close()
