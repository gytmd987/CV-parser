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
from .dbconn import Db, atomic
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
CREATE TABLE IF NOT EXISTS custom_fields (
    이름         TEXT PRIMARY KEY,
    유형         TEXT NOT NULL,      -- 텍스트 / 선택 / 연월 / 숫자
    선택지       TEXT DEFAULT '',    -- '선택' 유형일 때 | 로 구분
    순서         INTEGER DEFAULT 99,
    만든이       TEXT DEFAULT '',
    만든일시      TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS matches (
    지원자_ID    TEXT NOT NULL,
    과제키        TEXT NOT NULL,
    과제명        TEXT DEFAULT '',
    점수         INTEGER DEFAULT 0,
    사유         TEXT DEFAULT '',
    근거         TEXT DEFAULT '',
    순위         INTEGER DEFAULT 0,
    유사도        REAL,
    평가됨        INTEGER DEFAULT 1,
    판단일시      TEXT DEFAULT '',
    PRIMARY KEY (지원자_ID, 과제키)
);
CREATE INDEX IF NOT EXISTS matches_cand ON matches (지원자_ID);
CREATE TABLE IF NOT EXISTS column_config (
    열이름        TEXT PRIMARY KEY,   -- 기본 열·추가 열 공통
    표시이름      TEXT DEFAULT '',    -- 비면 열이름 그대로
    숨김         INTEGER DEFAULT 0,
    순서         INTEGER DEFAULT 0,   -- 0 이면 원래 순서
    긴글         INTEGER DEFAULT 0    -- 여러 줄을 넣을 수 있는 열인가
);
CREATE TABLE IF NOT EXISTS review_done (
    지원자_ID    TEXT NOT NULL,
    사유         TEXT NOT NULL,      -- 검토_사유 항목 글 그대로
    본사람        TEXT DEFAULT '',
    본일시        TEXT DEFAULT '',
    PRIMARY KEY (지원자_ID, 사유)
);
CREATE TABLE IF NOT EXISTS custom_values (
    지원자_ID    TEXT NOT NULL,
    필드명       TEXT NOT NULL,
    값           TEXT DEFAULT '',
    PRIMARY KEY (지원자_ID, 필드명)
);
"""

#: 사용자 정의 열이 가질 수 있는 유형
CUSTOM_TYPES = ("텍스트", "선택", "연월", "숫자")

#: 추가한 열이 어느 표에 속하는지.
#: '지원자 정보' 는 지원자 목록·엑셀에, '채용 현황' 은 채용 현황 표에 나간다.
CUSTOM_SCOPES = ("지원자 정보", "채용 현황")

# 나중에 추가된 열. 기존 DB 에도 없으면 붙인다.
_ADDED_COLUMNS = {
    "원문_텍스트": "TEXT DEFAULT ''",
    "저장_파일명": "TEXT DEFAULT ''",
    "지문": "TEXT DEFAULT ''",          # 중복 검토용. 원문 복원 불가
    "중복_메모": "TEXT DEFAULT ''",      # 등록 시 발견한 중복 후보
    "등록년도": "TEXT DEFAULT ''",       # 기본은 등록 시점 연도. 수정 가능
}

#: `column_config` 에 나중에 생긴 열
_ADDED_CONFIG_COLUMNS = {"긴글": "INTEGER DEFAULT 0"}

#: 처음부터 여러 줄로 두는 열. 원래 문단이 들어가는 자리다.
#: (관리자가 «표 항목» 에서 다른 열도 켜고 이 둘을 끌 수 있다.)
DEFAULT_LONG_COLUMNS = ("경력_요약", "비고")

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
        self._conn = Db(self.path)
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._custom_field_columns()
        self._matches_columns()
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
        설정열 = {
            r["name"] for r in self._conn.execute("PRAGMA table_info(column_config)")
        }
        for col, decl in _ADDED_CONFIG_COLUMNS.items():
            if col not in 설정열:
                self._conn.execute(
                    f"ALTER TABLE column_config ADD COLUMN {col} {decl}")

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

    @atomic
    def _unlink_files(self, ids: list[str]) -> None:
        """DB 행을 지우기 전에 그 지원자의 파일을 **전부** 지운다.

        DB 에 적힌 파일만 지우면, 연결이 끊긴 파일(재분석 중 오류 등)이 서버에
        남아 개인정보가 그대로 보관된다. 그래서 마지막에 지원자_ID 로 시작하는
        파일을 통째로 쓸어낸다. 파일명은 전부 지원자_ID 기반이라 안전하다.
          원본  : CV-XXXX.pdf
          첨부  : CV-XXXX-att3.docx
        """
        for cid in ids:
            path = self.file_path(cid)
            if path:
                path.unlink(missing_ok=True)
            for att in self.attachments(cid):
                (self.files_dir / att["저장명"]).unlink(missing_ok=True)
            self._conn.execute("DELETE FROM attachments WHERE 지원자_ID=?", (cid,))
            self._conn.execute("DELETE FROM custom_values WHERE 지원자_ID=?", (cid,))
            self._conn.execute("DELETE FROM matches WHERE 지원자_ID=?", (cid,))
            for 남은 in self.files_of(cid):
                남은.unlink(missing_ok=True)

    def files_of(self, 지원자_ID: str) -> list[Path]:
        """그 지원자 것으로 저장된 파일 전부 (원본·첨부·끊어진 것 포함)."""
        if not 지원자_ID or self.files_dir is None or not self.files_dir.is_dir():
            return []
        안전 = 지원자_ID.replace("[", "[[]")          # glob 특수문자 방어
        return sorted(
            set(self.files_dir.glob(f"{안전}.*")) | set(self.files_dir.glob(f"{안전}-att*"))
        )

    # -- 쓰기 -------------------------------------------------------------
    @atomic
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

    def meta_map(self) -> dict[str, dict[str, str]]:
        """{지원자_ID: {등록년도, 등록일시, 원본_파일명, 보관_만료일}}.

        표에 관리 정보 열을 같이 보여주려고 한 번에 읽는다. 사람마다
        meta() 를 부르면 지원자 수만큼 질의가 나간다.
        """
        rows = self._conn.execute(
            "SELECT 지원자_ID, 등록년도, 등록일시, 원본_파일명, 보관_만료일"
            " FROM candidates"
        )
        return {
            r["지원자_ID"]: {
                "등록년도": r["등록년도"] or "",
                "등록일시": r["등록일시"] or "",
                "원본_파일명": r["원본_파일명"] or "",
                "보관_만료일": r["보관_만료일"] or "",
            }
            for r in rows
        }

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

    @atomic
    def delete(self, 지원자_ID: str) -> bool:
        self._unlink_files([지원자_ID])
        cur = self._conn.execute(
            "DELETE FROM candidates WHERE 지원자_ID=?", (지원자_ID,)
        )
        self._conn.execute("DELETE FROM review_done WHERE 지원자_ID=?", (지원자_ID,))
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
        self._conn.execute(
            f"DELETE FROM review_done WHERE 지원자_ID IN ({marks})", ids
        )
        self._conn.commit()
        return cur.rowcount

    @atomic
    def delete_all(self) -> int:
        rows = self._conn.execute("SELECT 지원자_ID FROM candidates").fetchall()
        self._unlink_files([r["지원자_ID"] for r in rows])
        cur = self._conn.execute("DELETE FROM candidates")
        self._conn.execute("DELETE FROM review_done")
        self._conn.commit()
        return cur.rowcount

    # -- 첨부파일 (지원자별 여러 개) ---------------------------------------
    @atomic
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

    # -- 사용자 정의 열 -----------------------------------------------------
    @atomic
    def add_field(
        self, 이름: str, 유형: str = "텍스트", 선택지: str = "", 만든이: str = "",
        구분: str = "지원자 정보",
    ) -> None:
        """관리자가 웹에서 표에 열을 추가한다.

        값은 사람이 채운다(LLM 이 자동으로 채우지 않는다).
        """
        이름 = (이름 or "").strip()
        if not 이름:
            raise ValueError("열 이름을 입력하세요")
        if 유형 not in CUSTOM_TYPES:
            raise ValueError(f"유형은 {'/'.join(CUSTOM_TYPES)} 중 하나여야 합니다")
        if 구분 not in CUSTOM_SCOPES:
            raise ValueError(f"구분은 {'/'.join(CUSTOM_SCOPES)} 중 하나여야 합니다")
        from .schemas import COLUMNS

        if 이름 in COLUMNS:
            raise ValueError(f"이미 있는 기본 열입니다: {이름}")
        if 유형 == "선택" and not (선택지 or "").strip():
            raise ValueError("'선택' 유형은 선택지를 하나 이상 적어야 합니다")
        if self.field(이름):
            raise ValueError(f"이미 있는 열입니다: {이름}")
        순서 = self._conn.execute(
            "SELECT COALESCE(MAX(순서), 0) + 1 AS n FROM custom_fields"
        ).fetchone()["n"]
        self._conn.execute(
            "INSERT INTO custom_fields (이름, 유형, 선택지, 순서, 만든이, 만든일시, 구분)"
            " VALUES (?,?,?,?,?,?,?)",
            (이름, 유형, (선택지 or "").strip(), 순서, 만든이,
             now_kst().strftime("%Y-%m-%d %H:%M:%S"), 구분),
        )
        self._conn.commit()

    # -- 과제 매칭 ----------------------------------------------------------
    @atomic
    def _custom_field_columns(self) -> None:
        """예전 DB 의 custom_fields 에 없던 열을 붙인다."""
        있는열 = {r["name"] for r in self._conn.execute("PRAGMA table_info(custom_fields)")}
        if "구분" not in 있는열:
            self._conn.execute(
                "ALTER TABLE custom_fields ADD COLUMN 구분 TEXT DEFAULT '지원자 정보'"
            )
            self._conn.execute("UPDATE custom_fields SET 구분='지원자 정보' WHERE 구분 IS NULL")
        self._conn.commit()

    def _matches_columns(self) -> None:
        """예전 DB 에 없던 매칭 열을 붙인다."""
        있는열 = {r["name"] for r in self._conn.execute("PRAGMA table_info(matches)")}
        for 열, 정의 in (("유사도", "REAL"), ("평가됨", "INTEGER DEFAULT 1")):
            if 열 not in 있는열:
                self._conn.execute(f"ALTER TABLE matches ADD COLUMN {열} {정의}")
        self._conn.commit()

    @atomic
    def save_matches(self, 지원자_ID: str, matches) -> None:
        """이 지원자의 과제 매칭 결과를 통째로 갈아 끼운다.

        과제 목록이 바뀌면 옛 결과는 의미가 없으므로 남기지 않는다.
        """
        from .timeutil import now_kst

        now = now_kst().strftime("%Y-%m-%d %H:%M:%S")
        self._conn.execute("DELETE FROM matches WHERE 지원자_ID=?", (지원자_ID,))
        for 순위, m in enumerate(matches, start=1):
            self._conn.execute(
                "INSERT INTO matches (지원자_ID,과제키,과제명,점수,사유,근거,순위,"
                "유사도,평가됨,판단일시) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (지원자_ID, m.과제키, m.과제명, m.점수, m.사유,
                 "\n".join(m.근거), 순위, getattr(m, "유사도", None),
                 1 if getattr(m, "평가됨", True) else 0, now),
            )
        self._conn.commit()

    def matches(self, 지원자_ID: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM matches WHERE 지원자_ID=? ORDER BY 순위", (지원자_ID,)
        )
        out = []
        for r in rows:
            d = dict(r)
            d["근거"] = [x for x in (d.get("근거") or "").split("\n") if x.strip()]
            d["평가됨"] = bool(d.get("평가됨", 1))
            out.append(d)
        return out

    def top_matches(self) -> dict[str, dict]:
        """지원자별 1순위 과제. 표에 열로 낼 때 쓴다."""
        rows = self._conn.execute(
            "SELECT * FROM matches WHERE 순위=1"
        )
        return {r["지원자_ID"]: dict(r) for r in rows}

    def match_counts(self) -> dict[str, int]:
        """지원자별 비교한 과제 수. '정말 다 비교했나' 를 화면에서 보여주려고."""
        return {
            r["지원자_ID"]: r["c"] for r in self._conn.execute(
                "SELECT 지원자_ID, COUNT(*) c FROM matches GROUP BY 지원자_ID"
            )
        }

    def matched_count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(DISTINCT 지원자_ID) c FROM matches"
        ).fetchone()["c"]

    def clear_matches(self) -> int:
        cur = self._conn.execute("DELETE FROM matches")
        self._conn.commit()
        return cur.rowcount

    # -- 열 설정 (기본 열 + 추가 열 공통) -----------------------------------
    def column_config(self) -> dict[str, dict]:
        """{열이름: {표시이름, 숨김, 순서, 긴글}}. 설정한 열만 들어 있다."""
        return {
            r["열이름"]: {"표시이름": r["표시이름"] or "", "숨김": bool(r["숨김"]),
                        "순서": r["순서"] or 0, "긴글": bool(r["긴글"])}
            for r in self._conn.execute("SELECT * FROM column_config")
        }

    def 긴글열(self) -> set[str]:
        """여러 줄을 넣을 수 있는 열.

        화면(어떤 입력칸을 그릴까)·검사(줄바꿈을 살릴까)·엑셀(줄을 펴서
        보여줄까)이 **같은 곳**을 봐야 한다. 설정이 없는 열은 기본값을 따른다.
        """
        cfg = self.column_config()
        켠것 = {col for col, c in cfg.items() if c.get("긴글")}
        켠것 |= {col for col in DEFAULT_LONG_COLUMNS if col not in cfg}
        return 켠것

    def set_column(self, 열이름: str, *, 표시이름: str | None = None,
                   숨김: bool | None = None, 순서: int | None = None,
                   긴글: bool | None = None) -> None:
        """기본 열이든 추가 열이든 보이는 이름·숨김·순서·긴 글 여부를 정한다."""
        현재 = self.column_config().get(
            열이름,
            {"표시이름": "", "숨김": False, "순서": 0,
             "긴글": 열이름 in DEFAULT_LONG_COLUMNS},
        )
        새것 = {
            "표시이름": 현재["표시이름"] if 표시이름 is None else 표시이름.strip(),
            "숨김": 현재["숨김"] if 숨김 is None else bool(숨김),
            "순서": 현재["순서"] if 순서 is None else int(순서 or 0),
            "긴글": 현재.get("긴글", False) if 긴글 is None else bool(긴글),
        }
        self._conn.execute(
            "INSERT INTO column_config (열이름, 표시이름, 숨김, 순서, 긴글)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(열이름) DO UPDATE SET 표시이름=excluded.표시이름,"
            " 숨김=excluded.숨김, 순서=excluded.순서, 긴글=excluded.긴글",
            (열이름, 새것["표시이름"], 1 if 새것["숨김"] else 0, 새것["순서"],
             1 if 새것["긴글"] else 0),
        )
        self._conn.commit()

    def arrange(self, 열들: list[str]) -> list[str]:
        """설정한 순서·숨김을 적용한 열 목록.

        순서를 안 정한 열은 원래 자리를 지킨다. 그래야 열 하나에만 번호를
        매겨도 나머지가 뒤죽박죽되지 않는다.
        """
        cfg = self.column_config()
        보이는 = [c for c in 열들 if not cfg.get(c, {}).get("숨김")]
        번호 = {c: cfg[c]["순서"] for c in 보이는 if cfg.get(c, {}).get("순서")}
        if not 번호:
            return 보이는
        return sorted(
            보이는,
            key=lambda c: (번호.get(c, 보이는.index(c) + 1000), 보이는.index(c)),
        )

    def label(self, 열이름: str) -> str:
        """표에 보일 이름. 안 정했으면 기본 이름, 그것도 없으면 열 이름 그대로."""
        from .schemas import DEFAULT_LABELS

        return (self.column_config().get(열이름, {}).get("표시이름")
                or DEFAULT_LABELS.get(열이름) or 열이름)

    def labels(self, 열들: list[str]) -> dict[str, str]:
        from .schemas import DEFAULT_LABELS

        cfg = self.column_config()
        return {
            c: (cfg.get(c, {}).get("표시이름") or DEFAULT_LABELS.get(c) or c)
            for c in 열들
        }

    def fields(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM custom_fields ORDER BY 순서, 이름"
        )]

    def field(self, 이름: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM custom_fields WHERE 이름=?", (이름,)
        ).fetchone()
        return dict(row) if row else None

    def field_names(self, 구분: str | None = None) -> list[str]:
        """추가한 열 이름. 구분을 주면 그 묶음만."""
        return [f["이름"] for f in self.fields()
                if 구분 is None or (f.get("구분") or "지원자 정보") == 구분]

    @atomic
    def update_field(self, 이름: str, *, 새이름: str | None = None,
                     유형: str | None = None, 선택지: str | None = None,
                     구분: str | None = None) -> dict:
        """추가한 열의 이름·유형·선택지·구분을 고치고 **이전 내용**을 돌려준다.

        형식 검사를 건드리지 않는 선에서만 허용한다:

        - **유형 변경**은 이미 들어 있는 값이 새 유형에서도 전부 통과할 때만
          된다. 안 그러면 표에 있는 값이 형식 검사를 못 넘기는 유령이 된다.
        - **선택지 제거**는 그 선택지를 쓰고 있는 지원자가 없을 때만 된다.
        - **이름 변경**은 들어 있던 값을 같이 옮긴다 (값을 잃지 않는다).
        """
        from .edit import ValidationError, custom_field_spec, validate_custom
        from .schemas import COLUMNS

        옛 = self.field(이름)
        if 옛 is None:
            raise ValueError(f"없는 열입니다: {이름}")
        새것 = dict(옛)
        if 유형 is not None:
            if 유형 not in CUSTOM_TYPES:
                raise ValueError(f"유형은 {'/'.join(CUSTOM_TYPES)} 중 하나여야 합니다")
            새것["유형"] = 유형
        if 선택지 is not None:
            새것["선택지"] = (선택지 or "").strip()
        if 구분 is not None:
            if 구분 not in CUSTOM_SCOPES:
                raise ValueError(f"구분은 {'/'.join(CUSTOM_SCOPES)} 중 하나여야 합니다")
            새것["구분"] = 구분
        if 새것["유형"] == "선택" and not 새것["선택지"]:
            raise ValueError("'선택' 유형은 선택지를 하나 이상 적어야 합니다")

        쓰는값 = [
            r["값"] for r in self._conn.execute(
                "SELECT 값 FROM custom_values WHERE 필드명=? AND 값 != ''", (이름,)
            )
        ]
        걸린것: list[str] = []
        for 값 in 쓰는값:
            try:
                validate_custom(새것, 값)
            except ValidationError:
                if 값 not in 걸린것:
                    걸린것.append(값)
        if 걸린것:
            보임 = ", ".join(걸린것[:5]) + (" 외" if len(걸린것) > 5 else "")
            raise ValueError(
                f"이미 들어 있는 값이 새 형식을 못 넘깁니다: {보임} — "
                "그 값들을 먼저 고치거나 선택지에 남겨 두세요."
            )

        옮길이름 = (새이름 or 이름).strip()
        if not 옮길이름:
            raise ValueError("열 이름을 입력하세요")
        if 옮길이름 != 이름:
            if 옮길이름 in COLUMNS:
                raise ValueError(f"이미 있는 기본 열입니다: {옮길이름}")
            if self.field(옮길이름):
                raise ValueError(f"이미 있는 열입니다: {옮길이름}")

        self._conn.execute(
            "UPDATE custom_fields SET 이름=?, 유형=?, 선택지=?, 구분=? WHERE 이름=?",
            (옮길이름, 새것["유형"], 새것["선택지"], 새것["구분"], 이름),
        )
        if 옮길이름 != 이름:
            self._conn.execute(
                "UPDATE custom_values SET 필드명=? WHERE 필드명=?", (옮길이름, 이름)
            )
            self._conn.execute(
                "UPDATE column_config SET 열이름=? WHERE 열이름=?", (옮길이름, 이름)
            )
        self._conn.commit()
        # custom_field_spec 은 화면이 쓰는 것과 같은 규칙이다. 여기서 한 번
        # 불러 보는 것으로 새 설정이 실제로 입력칸을 만들 수 있는지 확인한다.
        custom_field_spec(새것)
        return 옛

    @atomic
    def delete_field(self, 이름: str) -> None:
        """열과 그 열에 들어 있던 값을 전부 지운다."""
        self._conn.execute("DELETE FROM custom_fields WHERE 이름=?", (이름,))
        self._conn.execute("DELETE FROM custom_values WHERE 필드명=?", (이름,))
        self._conn.commit()

    # -- 검토 항목 -----------------------------------------------------------
    def review_done(self, 지원자_ID: str) -> set[str]:
        """이 지원자에 대해 **이미 확인한** 검토 사유들."""
        return {
            r["사유"] for r in self._conn.execute(
                "SELECT 사유 FROM review_done WHERE 지원자_ID=?", (지원자_ID,)
            )
        }

    def review_done_map(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for r in self._conn.execute("SELECT 지원자_ID, 사유 FROM review_done"):
            out.setdefault(r["지원자_ID"], set()).add(r["사유"])
        return out

    def mark_reviewed(self, 지원자_ID: str, 사유: str, 본사람: str = "") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO review_done (지원자_ID, 사유, 본사람, 본일시)"
            " VALUES (?,?,?,?)",
            (지원자_ID, 사유, 본사람, now_kst().strftime("%Y-%m-%d %H:%M:%S")),
        )
        self._conn.commit()

    def unmark_reviewed(self, 지원자_ID: str, 사유: str) -> None:
        self._conn.execute(
            "DELETE FROM review_done WHERE 지원자_ID=? AND 사유=?", (지원자_ID, 사유)
        )
        self._conn.commit()

    def clear_reviews(self, 지원자_ID: str) -> None:
        """재분석하면 사유가 새로 나온다. 옛 확인 기록은 무효다."""
        self._conn.execute("DELETE FROM review_done WHERE 지원자_ID=?", (지원자_ID,))
        self._conn.commit()

    def set_custom(self, 지원자_ID: str, 필드명: str, 값: str) -> str:
        """사용자 정의 열 값을 저장하고 이전 값을 돌려준다."""
        if not self.field(필드명):
            raise ValueError(f"없는 열입니다: {필드명}")
        이전 = self.custom_values(지원자_ID).get(필드명, "")
        self._conn.execute(
            "INSERT INTO custom_values (지원자_ID, 필드명, 값) VALUES (?,?,?)"
            " ON CONFLICT(지원자_ID, 필드명) DO UPDATE SET 값=excluded.값",
            (지원자_ID, 필드명, 값 or ""),
        )
        self._conn.commit()
        return 이전

    def custom_values(self, 지원자_ID: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT 필드명, 값 FROM custom_values WHERE 지원자_ID=?", (지원자_ID,)
        )
        return {r["필드명"]: r["값"] or "" for r in rows}

    def custom_map(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for r in self._conn.execute("SELECT 지원자_ID, 필드명, 값 FROM custom_values"):
            out.setdefault(r["지원자_ID"], {})[r["필드명"]] = r["값"] or ""
        return out

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

    def ids(self) -> set[str]:
        """등록된 지원자 ID 만.

        '있는 사람인가' 만 알면 될 때 :meth:`list_all` 을 부르면 레코드를 전부
        JSON 에서 풀어 낸다. 사람 수가 늘면 그 값이 그대로 화면 여는 시간이 된다.
        """
        return {r["지원자_ID"] for r in
                self._conn.execute("SELECT 지원자_ID FROM candidates")}

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
