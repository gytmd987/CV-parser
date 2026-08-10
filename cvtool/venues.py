"""학회/저널 레지스트리.

CV 에서 발견된 제출처를 정규화해 등록한다. 관리 목록에 없으면 **자동으로
'미분류'로 추가**되고, 담당자가 웹에서 등급(우수/일반 등)을 지정한다.

핵심 문제는 표기 흔들림이다. 같은 학회가 이렇게 들어온다:
    "NeurIPS", "NIPS", "Neural Information Processing Systems",
    "Proc. of NeurIPS 2023", "neurips"
그래서 정규화 키로 묶고, 별칭(alias)을 등록하면 같은 항목으로 합쳐진다.

저장소는 sqlite (표준 라이브러리). 폐쇄망에서 추가 설치 없이 동작한다.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .fsutil import secure_dir, secure_file
from .timeutil import now_kst

# 기본 등급. 웹에서 자유롭게 바꿀 수 있다.
DEFAULT_TIERS = ["미분류", "최우수", "우수", "일반", "제외"]

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_NOISE_RE = re.compile(
    r"\b(proc\.?|proceedings|of|the|in|conference|conf\.?|international|intl\.?|"
    r"journal|trans\.?|transactions|on|ieee|acm|vol\.?|no\.?|pp\.?)\b",
    re.IGNORECASE,
)
_NONWORD_RE = re.compile(r"[^0-9a-z가-힣]+")


def normalize(raw: str) -> str:
    """제출처 표기를 매칭용 키로 정규화한다.

    연도·권호·'Proc. of' 같은 상투어를 걷어내고 소문자 영숫자만 남긴다.
    """
    s = raw.strip().lower()
    s = _YEAR_RE.sub(" ", s)
    s = _NOISE_RE.sub(" ", s)
    s = _NONWORD_RE.sub("", s)
    return s


@dataclass
class Venue:
    id: int
    정규화키: str
    표시명: str
    유형: str  # 학회 / 저널 / 기타
    등급: str  # DEFAULT_TIERS 중 하나 (사용자 확장 가능)
    국내해외: str  # 국내 / 해외 / 불명
    발견횟수: int
    최초등록: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS venues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    정규화키     TEXT UNIQUE NOT NULL,
    표시명       TEXT NOT NULL,
    유형         TEXT DEFAULT '',
    등급         TEXT DEFAULT '미분류',
    국내해외     TEXT DEFAULT '불명',
    발견횟수     INTEGER DEFAULT 0,
    최초등록     TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS venue_aliases (
    별칭키       TEXT PRIMARY KEY,
    venue_id    INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE
);
"""


class VenueRegistry:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        secure_dir(self.path.parent)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        for suffix in ("", "-wal", "-shm"):
            secure_file(Path(str(self.path) + suffix))

    # -- 조회 -------------------------------------------------------------
    def _resolve_id(self, key: str) -> int | None:
        row = self._conn.execute(
            "SELECT venue_id FROM venue_aliases WHERE 별칭키=?", (key,)
        ).fetchone()
        if row:
            return row["venue_id"]
        row = self._conn.execute("SELECT id FROM venues WHERE 정규화키=?", (key,)).fetchone()
        return row["id"] if row else None

    def get(self, venue_id: int) -> Venue | None:
        row = self._conn.execute("SELECT * FROM venues WHERE id=?", (venue_id,)).fetchone()
        return Venue(**dict(row)) if row else None

    def list_all(self, 등급: str | None = None) -> list[Venue]:
        sql = "SELECT * FROM venues"
        args: tuple = ()
        if 등급:
            sql += " WHERE 등급=?"
            args = (등급,)
        sql += " ORDER BY (등급='미분류') DESC, 발견횟수 DESC, 표시명"
        return [Venue(**dict(r)) for r in self._conn.execute(sql, args)]

    def unclassified_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) c FROM venues WHERE 등급='미분류'"
        ).fetchone()
        return row["c"]

    # -- 등록 -------------------------------------------------------------
    def observe(self, 표시명: str, *, 유형: str = "", 국내해외: str = "불명") -> Venue:
        """CV 에서 제출처를 발견했을 때 호출.

        이미 있으면 발견횟수만 올리고, 없으면 '미분류'로 자동 등록한다.
        """
        key = normalize(표시명)
        if not key:
            raise ValueError("정규화 결과가 비어 있습니다: " + repr(표시명))

        vid = self._resolve_id(key)
        if vid is None:
            cur = self._conn.execute(
                "INSERT INTO venues (정규화키,표시명,유형,등급,국내해외,발견횟수,최초등록)"
                " VALUES (?,?,?,'미분류',?,1,?)",
                (key, 표시명.strip(), 유형, 국내해외, now_kst().strftime("%Y-%m-%d %H:%M:%S")),
            )
            self._conn.commit()
            vid = cur.lastrowid
        else:
            self._conn.execute(
                "UPDATE venues SET 발견횟수=발견횟수+1 WHERE id=?", (vid,)
            )
            self._conn.commit()
        venue = self.get(vid)
        assert venue is not None
        return venue

    def classify(
        self,
        venue_id: int,
        *,
        등급: str | None = None,
        유형: str | None = None,
        국내해외: str | None = None,
        표시명: str | None = None,
    ) -> None:
        """담당자가 웹에서 등급/유형/국내해외를 지정."""
        sets, args = [], []
        for col, val in (
            ("등급", 등급),
            ("유형", 유형),
            ("국내해외", 국내해외),
            ("표시명", 표시명),
        ):
            if val is not None:
                sets.append(f"{col}=?")
                args.append(val)
        if not sets:
            return
        args.append(venue_id)
        self._conn.execute(f"UPDATE venues SET {','.join(sets)} WHERE id=?", args)
        self._conn.commit()

    def merge(self, alias_venue_id: int, into_venue_id: int) -> None:
        """표기가 다른 같은 학회를 하나로 합친다 (별칭 등록)."""
        if alias_venue_id == into_venue_id:
            return
        src = self.get(alias_venue_id)
        target = self.get(into_venue_id)
        if src is None or target is None:
            raise ValueError("존재하지 않는 venue")
        self._conn.execute(
            "INSERT OR REPLACE INTO venue_aliases (별칭키, venue_id) VALUES (?,?)",
            (src.정규화키, into_venue_id),
        )
        self._conn.execute(
            "UPDATE venues SET 발견횟수=발견횟수+? WHERE id=?",
            (src.발견횟수, into_venue_id),
        )
        self._conn.execute("DELETE FROM venues WHERE id=?", (alias_venue_id,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def apply_registry(rec, reg: VenueRegistry) -> None:
    """추출된 논문의 제출처를 레지스트리에 반영한다 (CVRecord 를 제자리 수정).

    - 목록에 없는 제출처는 '미분류'로 자동 등록된다.
    - 이미 분류된 제출처는 **담당자가 정한 국내/해외 값이 LLM 추측을 덮어쓴다.**
      사람이 판별한 쪽이 항상 우선한다.
    - 미분류가 하나라도 있으면 검토_필요 로 표시한다. 판별 전에는 해외 논문
      열을 그대로 믿으면 안 되기 때문이다.
    """
    미분류: list[str] = []
    for paper in rec.논문:
        raw = (paper.제출처 or "").strip()
        if not raw:
            continue
        try:
            venue = reg.observe(raw, 유형=paper.유형 or "", 국내해외=paper.국내해외 or "불명")
        except ValueError:
            continue
        if venue.등급 == "미분류":
            미분류.append(venue.표시명)
        elif venue.국내해외 in ("국내", "해외"):
            paper.국내해외 = venue.국내해외  # 사람이 판별한 값이 우선
        if venue.유형:
            paper.유형 = venue.유형

    if 미분류:
        사유 = "미분류 학회/저널: " + ", ".join(sorted(set(미분류)))
        rec.검토_사유 = f"{rec.검토_사유} / {사유}" if rec.검토_사유 else 사유
        rec.검토_필요 = "Y"
