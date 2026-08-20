"""채용 현황 — 단계별 진행 상태 · 부서/과제 배정 · 비고.

지원자 DB 는 하나만 쓴다. 여기에는 채용 진행에 관한 것만 담고,
지원자 정보 자체는 candidates 쪽에 그대로 둔다.

단계는 순서가 고정돼 있고, 각 단계마다 상태를 고른다.
    서류 검토 -> 전화 면접 -> 기술 면접 -> HR 면접
    각 단계: 진행중 / 합격 / 불합격 / 보류 (빈칸 = 아직 시작 안 함)

정렬 기본값은 **불합격자를 맨 아래로** 내린다. 현재 보고 있어야 할 사람이
위로 오는 게 실무에 맞기 때문이다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .fsutil import secure_dir, secure_file
from .timeutil import now_kst

#: 채용 단계 (순서 고정)
STAGES = ("서류 검토", "전화 면접", "기술 면접", "HR 면접")

#: 각 단계에서 고를 수 있는 상태. 빈 문자열은 '아직 시작 안 함'
#: 처음 값일 뿐이고, 관리자가 `표 항목` 화면에서 바꿀 수 있다.
STATUSES = ("", "진행중", "합격", "불합격", "보류")

#: **바꿀 수 없는 상태.** 최종상태·탈락 판정·정렬이 이 값에 걸려 있다.
#: 빈칸은 '아직 시작 안 함', 합격/불합격은 다음 단계로 갈지 끝났는지를 정한다.
FIXED_STATUSES = ("", "합격", "불합격")

#: 채용 현황 표에만 있는 열 (지원자 DB 열과 합쳐서 보여준다)
RECRUIT_COLUMNS = ("부서", "과제", *STAGES, "최종상태", "비고")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recruit (
    지원자_ID    TEXT PRIMARY KEY,
    부서_id      INTEGER,
    project_id  INTEGER,
    비고         TEXT DEFAULT '',
    갱신일시      TEXT DEFAULT '',
    갱신자        TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stages (
    지원자_ID    TEXT NOT NULL,
    단계         TEXT NOT NULL,
    상태         TEXT DEFAULT '',
    갱신일시      TEXT DEFAULT '',
    갱신자        TEXT DEFAULT '',
    PRIMARY KEY (지원자_ID, 단계)
);
CREATE TABLE IF NOT EXISTS view_columns (
    열이름       TEXT PRIMARY KEY,
    순서         INTEGER DEFAULT 99,
    표시         INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stage_statuses (
    순서         INTEGER PRIMARY KEY,
    상태         TEXT NOT NULL
);
"""


@dataclass
class Progress:
    지원자_ID: str
    부서_id: int | None = None
    project_id: int | None = None
    비고: str = ""
    갱신일시: str = ""
    갱신자: str = ""
    단계상태: dict[str, str] = field(default_factory=dict)

    @property
    def 최종상태(self) -> str:
        """지금 이 사람이 어디까지 왔는지 한 줄로.

        한 단계라도 불합격이면 그 시점에서 끝난 것으로 본다.
        """
        for 단계 in STAGES:
            상태 = self.단계상태.get(단계, "")
            if 상태 == "불합격":
                return f"{단계} 불합격"
        진행 = [s for s in STAGES if self.단계상태.get(s)]
        if not 진행:
            return "미시작"
        마지막 = 진행[-1]
        상태 = self.단계상태[마지막]
        if 마지막 == STAGES[-1] and 상태 == "합격":
            return "최종 합격"
        return f"{마지막} {상태}"

    @property
    def 탈락(self) -> bool:
        return any(self.단계상태.get(s) == "불합격" for s in STAGES)

    def 정렬키(self) -> tuple:
        """불합격은 맨 아래, 그 다음은 진행이 많이 된 순서."""
        진행수 = sum(1 for s in STAGES if self.단계상태.get(s))
        보류 = any(self.단계상태.get(s) == "보류" for s in STAGES)
        return (1 if self.탈락 else 0, 1 if 보류 else 0, -진행수)


class RecruitStore:
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

    # -- 조회 -------------------------------------------------------------
    def get(self, 지원자_ID: str) -> Progress:
        row = self._conn.execute(
            "SELECT * FROM recruit WHERE 지원자_ID=?", (지원자_ID,)
        ).fetchone()
        p = (
            Progress(
                지원자_ID=지원자_ID,
                부서_id=row["부서_id"],
                project_id=row["project_id"],
                비고=row["비고"] or "",
                갱신일시=row["갱신일시"] or "",
                갱신자=row["갱신자"] or "",
            )
            if row
            else Progress(지원자_ID=지원자_ID)
        )
        for r in self._conn.execute(
            "SELECT 단계, 상태 FROM stages WHERE 지원자_ID=?", (지원자_ID,)
        ):
            p.단계상태[r["단계"]] = r["상태"] or ""
        return p

    def all(self) -> dict[str, Progress]:
        out: dict[str, Progress] = {}
        for row in self._conn.execute("SELECT * FROM recruit"):
            out[row["지원자_ID"]] = Progress(
                지원자_ID=row["지원자_ID"],
                부서_id=row["부서_id"],
                project_id=row["project_id"],
                비고=row["비고"] or "",
                갱신일시=row["갱신일시"] or "",
                갱신자=row["갱신자"] or "",
            )
        for r in self._conn.execute("SELECT 지원자_ID, 단계, 상태 FROM stages"):
            out.setdefault(r["지원자_ID"], Progress(지원자_ID=r["지원자_ID"]))
            out[r["지원자_ID"]].단계상태[r["단계"]] = r["상태"] or ""
        return out

    # -- 수정 -------------------------------------------------------------
    def _touch(self, 지원자_ID: str, 사용자: str) -> None:
        self._conn.execute(
            "INSERT INTO recruit (지원자_ID, 갱신일시, 갱신자) VALUES (?,?,?)"
            " ON CONFLICT(지원자_ID) DO UPDATE SET 갱신일시=excluded.갱신일시,"
            " 갱신자=excluded.갱신자",
            (지원자_ID, now_kst().strftime("%Y-%m-%d %H:%M:%S"), 사용자),
        )

    def set_stage(self, 지원자_ID: str, 단계: str, 상태: str, 사용자: str) -> str:
        """단계 상태를 바꾸고 이전 상태를 돌려준다."""
        if 단계 not in STAGES:
            raise ValueError(f"없는 단계입니다: {단계}")
        고를수있는것 = self.statuses()
        if 상태 not in 고를수있는것:
            raise ValueError(
                f"상태는 {'/'.join(s or '(빈칸)' for s in 고를수있는것)} 중 하나여야 합니다"
            )
        이전 = self.get(지원자_ID).단계상태.get(단계, "")
        self._conn.execute(
            "INSERT INTO stages (지원자_ID, 단계, 상태, 갱신일시, 갱신자) VALUES (?,?,?,?,?)"
            " ON CONFLICT(지원자_ID, 단계) DO UPDATE SET 상태=excluded.상태,"
            " 갱신일시=excluded.갱신일시, 갱신자=excluded.갱신자",
            (지원자_ID, 단계, 상태, now_kst().strftime("%Y-%m-%d %H:%M:%S"), 사용자),
        )
        self._touch(지원자_ID, 사용자)
        self._conn.commit()
        return 이전

    def set_assignment(
        self, 지원자_ID: str, 부서_id: int | None, project_id: int | None, 사용자: str
    ) -> tuple[int | None, int | None]:
        before = self.get(지원자_ID)
        self._touch(지원자_ID, 사용자)
        self._conn.execute(
            "UPDATE recruit SET 부서_id=?, project_id=? WHERE 지원자_ID=?",
            (부서_id, project_id, 지원자_ID),
        )
        self._conn.commit()
        return before.부서_id, before.project_id

    def set_note(self, 지원자_ID: str, 비고: str, 사용자: str) -> str:
        before = self.get(지원자_ID).비고
        self._touch(지원자_ID, 사용자)
        self._conn.execute(
            "UPDATE recruit SET 비고=? WHERE 지원자_ID=?", (비고 or "", 지원자_ID)
        )
        self._conn.commit()
        return before

    def delete(self, 지원자_ID: str) -> None:
        self._conn.execute("DELETE FROM recruit WHERE 지원자_ID=?", (지원자_ID,))
        self._conn.execute("DELETE FROM stages WHERE 지원자_ID=?", (지원자_ID,))
        self._conn.commit()

    # -- 단계 상태 목록 (관리자) -------------------------------------------
    def statuses(self) -> list[str]:
        """각 단계에서 고를 수 있는 상태. 안 정했으면 기본값."""
        rows = self._conn.execute(
            "SELECT 상태 FROM stage_statuses ORDER BY 순서"
        ).fetchall()
        return [r["상태"] for r in rows] if rows else list(STATUSES)

    def set_statuses(self, 목록: list[str]) -> list[str]:
        """상태 목록을 바꾸고 이전 목록을 돌려준다.

        합격·불합격·빈칸은 뺄 수 없다. 최종상태와 탈락 판정이 이 값을 보고
        돌아가서, 이름이 바뀌면 이미 저장된 진행 상황이 통째로 뜻을 잃는다.
        쓰고 있는 상태도 뺄 수 없다 — 빼면 그 사람 진행 상황이 사라진다.
        """
        깨끗한 = [""]
        for 값 in 목록:
            값 = (값 or "").strip()
            if 값 and 값 not in 깨끗한:
                깨끗한.append(값)
        빠진고정 = [s for s in FIXED_STATUSES if s not in 깨끗한]
        if 빠진고정:
            raise ValueError(
                "다음 상태는 뺄 수 없습니다: "
                + ", ".join(s or "(빈칸)" for s in 빠진고정)
                + " — 최종상태·탈락 판정이 이 값에 걸려 있습니다."
            )
        쓰는중 = {
            r["상태"] for r in self._conn.execute(
                "SELECT DISTINCT 상태 FROM stages WHERE 상태 != ''"
            )
        }
        사라지는것 = sorted(쓰는중 - set(깨끗한))
        if 사라지는것:
            raise ValueError(
                "지금 쓰고 있는 상태라 뺄 수 없습니다: " + ", ".join(사라지는것)
                + " — 그 지원자들 상태를 먼저 바꾸세요."
            )
        이전 = self.statuses()
        self._conn.execute("DELETE FROM stage_statuses")
        for i, 상태 in enumerate(깨끗한):
            self._conn.execute(
                "INSERT INTO stage_statuses (순서, 상태) VALUES (?,?)", (i, 상태)
            )
        self._conn.commit()
        return 이전

    # -- 표시 열 구성 (관리자) ---------------------------------------------
    def set_columns(self, 열목록: list[str]) -> None:
        """채용 현황 표에 보일 열과 순서를 정한다."""
        self._conn.execute("DELETE FROM view_columns")
        for i, 이름 in enumerate(열목록):
            self._conn.execute(
                "INSERT OR REPLACE INTO view_columns (열이름, 순서, 표시) VALUES (?,?,1)",
                (이름, i),
            )
        self._conn.commit()

    def columns(self, 기본: list[str] | None = None) -> list[str]:
        rows = self._conn.execute(
            "SELECT 열이름 FROM view_columns WHERE 표시=1 ORDER BY 순서"
        ).fetchall()
        if rows:
            return [r["열이름"] for r in rows]
        return list(기본 or ["한글_이름", "현재_신분", "박사_학교", *RECRUIT_COLUMNS])

    def close(self) -> None:
        self._conn.close()
