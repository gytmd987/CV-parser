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
CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    지원자_ID    TEXT NOT NULL,
    파일명       TEXT NOT NULL,   -- 올릴 때의 원래 이름
    저장명       TEXT NOT NULL,   -- 디스크에 저장된 이름
    올린이       TEXT DEFAULT '',
    올린일시      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_attach ON attachments(지원자_ID, id);
"""

# 나중에 추가된 열. 기존 DB 에도 없으면 붙인다.
_ADDED_COLUMNS = {
    "원문_텍스트": "TEXT DEFAULT ''",
    "저장_파일명": "TEXT DEFAULT ''",
    "지문": "TEXT DEFAULT ''",          # 중복 검토용. 원문 복원 불가
    "중복_메모": "TEXT DEFAULT ''",      # 등록 시 발견한 중복 후보
    "등록년도": "TEXT DEFAULT ''",       # 기본은 등록 시점 연도. 수정 가능
}

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}

#: 첨부파일로 받아줄 형식 (CV 형식 + 자주 쓰는 문서·이미지)
ATTACHMENT_SUFFIXES = SUPPORTED_SUFFIXES | {
    ".hwp", ".hwpx", ".xlsx", ".pptx", ".png", ".jpg", ".jpeg", ".zip", ".csv",
}


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
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
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
        """DB 행을 지우기 전에 원본과 첨부파일부터 지운다."""
        for cid in ids:
            path = self.file_path(cid)
            if path:
                path.unlink(missing_ok=True)
            for att in self.attachments(cid):
                (self.files_dir / att["저장명"]).unlink(missing_ok=True)
            self._conn.execute("DELETE FROM attachments WHERE 지원자_ID=?", (cid,))

    # -- 쓰기 -------------------------------------------------------------
    def save(
        self,
        rec: CVRecord,
        원문_텍스트: str = "",
        저장_파일명: str | None = None,
        지문: list[str] | None = None,
        중복_메모: str | None = None,
    ) -> None:
        now = now_kst()
        # 보관 기간 0 = 무제한. 만료일을 비워두면 자동 삭제 대상이 되지 않는다.
        만료 = (
            expiry_date(now, settings.retention_months).strftime("%Y-%m-%d")
            if settings.retention_months > 0
            else ""
        )
        보관할_원문 = 원문_텍스트 if settings.store_cv_text else ""
        if 저장_파일명 is None:  # 재분석 등에서 기존 값을 유지
            row = self._conn.execute(
                "SELECT 저장_파일명 FROM candidates WHERE 지원자_ID=?", (rec.지원자_ID,)
            ).fetchone()
            저장_파일명 = row["저장_파일명"] if row else ""
        기존 = self._conn.execute(
            "SELECT 지문, 중복_메모, 등록일시, 등록년도 FROM candidates WHERE 지원자_ID=?",
            (rec.지원자_ID,),
        ).fetchone()
        if 지문 is None:
            지문_json = 기존["지문"] if 기존 else ""
        else:
            지문_json = json.dumps(지문)
        if 중복_메모 is None:
            중복_메모 = 기존["중복_메모"] if 기존 else ""
        # 재분석해도 최초 등록일시는 유지한다
        등록일시 = 기존["등록일시"] if 기존 else now.strftime("%Y-%m-%d %H:%M:%S")
        # 등록년도는 기본이 등록 시점 연도이고, 담당자가 고칠 수 있다
        등록년도 = (기존["등록년도"] if 기존 and 기존["등록년도"] else "") or now.strftime("%Y")

        self._conn.execute(
            "INSERT OR REPLACE INTO candidates"
            " (지원자_ID, 등록일시, 원본_파일명, 보관_만료일, record_json,"
            "  원문_텍스트, 저장_파일명, 지문, 중복_메모, 등록년도)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                rec.지원자_ID,
                등록일시,
                rec.원본_파일명,
                만료,
                rec.model_dump_json(),
                보관할_원문,
                저장_파일명 or "",
                지문_json,
                중복_메모 or "",
                등록년도,
            ),
        )
        self._conn.commit()

    def set_year(self, 지원자_ID: str, 등록년도: str) -> None:
        """등록년도를 고친다. 4자리 숫자만 받는다."""
        년도 = (등록년도 or "").strip()
        if not (len(년도) == 4 and 년도.isdigit()):
            raise ValueError(f"등록년도는 4자리 숫자여야 합니다: {등록년도!r}")
        self._conn.execute(
            "UPDATE candidates SET 등록년도=? WHERE 지원자_ID=?", (년도, 지원자_ID)
        )
        self._conn.commit()

    def year_of(self, 지원자_ID: str) -> str:
        row = self._conn.execute(
            "SELECT 등록년도 FROM candidates WHERE 지원자_ID=?", (지원자_ID,)
        ).fetchone()
        return (row["등록년도"] or "") if row else ""

    def years(self) -> list[str]:
        """등록된 연도 목록 (최신순)."""
        rows = self._conn.execute(
            "SELECT DISTINCT 등록년도 FROM candidates WHERE 등록년도 != ''"
            " ORDER BY 등록년도 DESC"
        )
        return [r["등록년도"] for r in rows]

    def year_map(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT 지원자_ID, 등록년도 FROM candidates")
        return {r["지원자_ID"]: r["등록년도"] or "" for r in rows}

    def fingerprints(self) -> list[tuple[CVRecord, list[str]]]:
        """중복 검토용 (레코드, 지문) 목록."""
        out = []
        for row in self._conn.execute("SELECT record_json, 지문 FROM candidates"):
            try:
                fp = json.loads(row["지문"]) if row["지문"] else []
            except json.JSONDecodeError:
                fp = []
            out.append((self._row_to_record(row), fp))
        return out

    def duplicate_note(self, 지원자_ID: str) -> str:
        row = self._conn.execute(
            "SELECT 중복_메모 FROM candidates WHERE 지원자_ID=?", (지원자_ID,)
        ).fetchone()
        return (row["중복_메모"] or "") if row else ""

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

    # -- 첨부파일 (지원자별 여러 개) ---------------------------------------
    def add_attachment(
        self, 지원자_ID: str, 파일명: str, content: bytes, 올린이: str = ""
    ) -> int:
        """CV 원본과 별개로 붙이는 자료. 여러 개 넣을 수 있다."""
        suffix = Path(파일명).suffix.lower()
        if suffix not in ATTACHMENT_SUFFIXES:
            raise ValueError(
                f"받지 않는 형식입니다: {suffix or '(확장자 없음)'} "
                f"(허용: {', '.join(sorted(ATTACHMENT_SUFFIXES))})"
            )
        cur = self._conn.execute(
            "INSERT INTO attachments (지원자_ID, 파일명, 저장명, 올린이, 올린일시)"
            " VALUES (?,?,?,?,?)",
            (지원자_ID, 파일명, "", 올린이, now_kst().strftime("%Y-%m-%d %H:%M:%S")),
        )
        att_id = cur.lastrowid
        # 저장명도 지원자_ID 기반으로. 파일명에 개인정보가 들어가지 않게 한다.
        저장명 = f"{지원자_ID}-att{att_id}{suffix}"
        dest = self.files_dir / 저장명
        dest.write_bytes(content)
        secure_file(dest)
        self._conn.execute(
            "UPDATE attachments SET 저장명=? WHERE id=?", (저장명, att_id)
        )
        self._conn.commit()
        return att_id

    def attachments(self, 지원자_ID: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM attachments WHERE 지원자_ID=? ORDER BY id", (지원자_ID,)
        )
        return [dict(r) for r in rows]

    def attachment(self, att_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM attachments WHERE id=?", (att_id,)
        ).fetchone()
        return dict(row) if row else None

    def delete_attachment(self, att_id: int) -> str:
        att = self.attachment(att_id)
        if not att:
            return ""
        (self.files_dir / att["저장명"]).unlink(missing_ok=True)
        self._conn.execute("DELETE FROM attachments WHERE id=?", (att_id,))
        self._conn.commit()
        return att["파일명"]

    def create_blank(self, 지원자_ID: str | None = None) -> CVRecord:
        """CV 없이 지원자를 만든다.

        다른 지원서로 지원한 경우처럼 CV 파일이 없을 때 쓴다.
        빈 레코드를 만들어 두면 사람이 채워 넣을 수 있다.
        """
        import uuid as _uuid

        cid = 지원자_ID or f"CV-{_uuid.uuid4().hex[:8].upper()}"
        rec = CVRecord(지원자_ID=cid, 검토_필요="Y", 검토_사유="CV 없이 직접 등록 (내용 확인 필요)")
        self.save(rec)
        return rec

    def orphan_files(self) -> list[Path]:
        """DB 에 대응하는 행이 없는 파일 (크래시 등으로 남은 것)."""
        known = {
            r["저장_파일명"]
            for r in self._conn.execute("SELECT 저장_파일명 FROM candidates")
            if r["저장_파일명"]
        }
        known |= {
            r["저장명"]
            for r in self._conn.execute("SELECT 저장명 FROM attachments")
            if r["저장명"]
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

    def list_filtered(
        self, q: str = "", review_only: bool = False, 년도: str = ""
    ) -> list[CVRecord]:
        """이름·소속·학교·파일명으로 검색하고, 검토 필요·등록년도로 거른다."""
        records = self.list_all()
        if 년도:
            연도맵 = self.year_map()
            records = [r for r in records if 연도맵.get(r.지원자_ID) == 년도]
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
