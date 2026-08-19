"""명칭 사전 — 같은 대상을 여러 표기로 적은 것을 하나로 묶는다.

같은 소속(학교·회사)이 이렇게 들어온다:
    포항공대 / POSTECH / 포항공과대학교 / 포항공과대학교(POSTECH)
    (주)가나다소프트 / 가나다소프트 / 가나다소프트웨어
같은 학회도 마찬가지다:
    International Conference on Machine Learning / ICML / Proc. of ICML 2023

소속·학회·저널·전공이 **모두 같은 문제**라서 한 구조로 처리한다.
종류(kind)만 다르고 화면도 같은 패턴을 쓴다.

핵심 원칙: **원문 표기는 절대 바꾸지 않는다.**
지원자 레코드에는 CV 에 적힌 그대로 저장하고, 화면·엑셀에서 보여줄 때만
대표명으로 바꾼다. 그래야 담당자가 관리화면에서 이름을 고치면
**이미 등록된 지원자 표에도 즉시 반영**된다.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .fsutil import secure_dir, secure_file
from .timeutil import now_kst

#: 사전이 다루는 대상 종류.
#: '소속' 은 학교와 회사를 함께 담는다 — 지원자의 현재 소속은 학교일 수도
#: 회사일 수도 있어서 사전을 둘로 나누면 같은 곳이 양쪽에 생긴다.
KINDS = ("소속", "학회·저널", "전공")

#: 등급이 의미 있는 종류 (소속·전공은 등급을 매기지 않는다)
GRADED_KINDS = ("학회·저널",)

#: 학회인지 저널인지는 **종류를 나누지 않고 열 하나로** 구분한다.
#: 같은 곳을 어떤 CV 는 학회로, 어떤 CV 는 저널로 적어서 사전이 둘로
#: 갈라지고 같은 이름이 양쪽에 생기는 일이 있었다.
SUBTYPES = ("학회", "저널", "불명")

#: 예전 이름 -> 지금 이름
KIND_ALIASES = {"학교": "소속", "학회": "학회·저널", "저널": "학회·저널"}


def canonical_kind(종류: str) -> str:
    """예전 링크·북마크(`?kind=학교`)도 계속 열리게 한다."""
    return KIND_ALIASES.get(종류, 종류)


#: 기본 등급. 표에 열로 나올지는 종류별로 담당자가 켜고 끈다.
DEFAULT_TIERS = [
    ("미분류", 0, 0),
    ("최우수", 1, 1),
    ("우수", 2, 1),
    ("일반", 3, 0),
    ("제외", 4, 0),
]

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_PAREN_RE = re.compile(r"[(（\[][^)）\]]*[)）\]]")
_VENUE_NOISE_RE = re.compile(
    r"\b(proc\.?|proceedings|of|the|in|conference|conf\.?|international|intl\.?|"
    r"journal|trans\.?|transactions|on|ieee|acm|vol\.?|no\.?|pp\.?)\b",
    re.IGNORECASE,
)
_NONWORD_RE = re.compile(r"[^0-9a-z가-힣]+")


def normalize(raw: str, 종류: str = "학회") -> str:
    """표기를 매칭용 키로 바꾼다.

    괄호 안 내용은 어느 종류든 떼어낸다. 그래야
    '포항공과대학교(POSTECH)' 와 '포항공과대학교' 가 자동으로 묶인다.
    ('POSTECH' 처럼 아예 다른 표기는 담당자가 관리화면에서 묶어준다.)

    옛 종류 이름('학회'/'저널')으로 불러도 같은 키가 나와야 한다.
    안 그러면 이미 저장된 정규화키와 어긋나 사전이 통째로 안 맞는다.
    """
    s = _PAREN_RE.sub(" ", raw.strip().lower())
    if canonical_kind(종류) in GRADED_KINDS:
        s = _YEAR_RE.sub(" ", s)
        s = _VENUE_NOISE_RE.sub(" ", s)
    return _NONWORD_RE.sub("", s)


@dataclass
class Name:
    id: int
    종류: str
    정규화키: str
    표시명: str
    등급: str
    국내해외: str
    발견횟수: int
    최초등록: str
    유형: str = "불명"          # 학회 / 저널 (학회·저널 종류에서만 씀)
    IF: str = ""               # Impact Factor (저널)

    def google_url(self, 무엇: str = "impact factor") -> str:
        """IF 를 찾아보기 쉽게 검색어를 미리 채운 구글 링크."""
        from urllib.parse import quote_plus

        return f"https://www.google.com/search?q={quote_plus(f'{self.표시명} {무엇}'.strip())}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS names (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    종류         TEXT NOT NULL,
    정규화키      TEXT NOT NULL,
    표시명        TEXT NOT NULL,
    등급         TEXT DEFAULT '미분류',
    국내해외      TEXT DEFAULT '불명',
    발견횟수      INTEGER DEFAULT 0,
    최초등록      TEXT DEFAULT '',
    유형         TEXT DEFAULT '불명',
    IF          TEXT DEFAULT '',
    UNIQUE(종류, 정규화키)
);
CREATE TABLE IF NOT EXISTS name_aliases (
    종류         TEXT NOT NULL,
    별칭키        TEXT NOT NULL,
    name_id     INTEGER NOT NULL REFERENCES names(id) ON DELETE CASCADE,
    PRIMARY KEY (종류, 별칭키)
);
CREATE TABLE IF NOT EXISTS tiers (
    이름         TEXT PRIMARY KEY,
    순서         INTEGER DEFAULT 99,
    표에_표시     INTEGER DEFAULT 0
);
"""


class NameRegistry:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        secure_dir(self.path.parent)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 여러 사람이 동시에 써도 읽기가 막히지 않게 한다
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._add_missing_columns()
        self._seed_tiers()
        self._migrate_kind_names()
        self._dedupe()
        self._migrate_from_venues()
        self._conn.commit()
        for suffix in ("", "-wal", "-shm"):
            secure_file(Path(str(self.path) + suffix))

    def _seed_tiers(self) -> None:
        for 이름, 순서, 표시 in DEFAULT_TIERS:
            self._conn.execute(
                "INSERT OR IGNORE INTO tiers (이름, 순서, 표에_표시) VALUES (?,?,?)",
                (이름, 순서, 표시),
            )

    def _add_missing_columns(self) -> None:
        """예전 DB 에 없던 열을 붙인다."""
        있는열 = {r["name"] for r in self._conn.execute("PRAGMA table_info(names)")}
        for 열, 기본 in (("유형", "'불명'"), ("IF", "''")):
            if 열 not in 있는열:
                self._conn.execute(f"ALTER TABLE names ADD COLUMN {열} TEXT DEFAULT {기본}")

    def _migrate_kind_names(self) -> None:
        """'학교'->'소속', '학회'/'저널'->'학회·저널' 로 옮긴다.

        분류해 둔 대표명·별칭·등급이 그대로 살아야 해서 행을 새로 만들지 않고
        종류 이름만 바꾼다. 학회/저널은 어느 쪽이었는지를 '유형' 열에 남긴다.
        """
        for 옛, 새 in KIND_ALIASES.items():
            for row in self._conn.execute(
                "SELECT * FROM names WHERE 종류=?", (옛,)
            ).fetchall():
                기존 = self._conn.execute(
                    "SELECT id FROM names WHERE 종류=? AND 정규화키=?", (새, row["정규화키"])
                ).fetchone()
                if 기존:
                    # 옮길 자리에 같은 이름이 이미 있다 (학회·저널이 갈라져 있던 흔적).
                    # 그냥 UPDATE 하면 UNIQUE 제약에 걸려 프로그램이 아예 안 뜬다.
                    self._absorb(Name(**dict(row)), 기존["id"])
                    continue
                if 옛 in SUBTYPES:
                    self._conn.execute(
                        "UPDATE names SET 종류=?, 유형=? WHERE id=?", (새, 옛, row["id"])
                    )
                else:
                    self._conn.execute(
                        "UPDATE names SET 종류=? WHERE id=?", (새, row["id"])
                    )
            self._conn.execute("UPDATE name_aliases SET 종류=? WHERE 종류=?", (새, 옛))

    def _absorb(self, src: "Name", 대표_id: int) -> None:
        """src 항목을 대표 항목에 합치고 지운다.

        분류해 둔 정보는 '값이 있는 쪽' 을 살린다. 빈 대표가 채워진 쪽을
        덮어써 버리면 담당자가 해둔 일이 날아간다.
        """
        dst = self.get(대표_id)
        if dst is None or src.id == 대표_id:
            return
        self._conn.execute(
            "UPDATE name_aliases SET name_id=? WHERE name_id=?", (대표_id, src.id)
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO name_aliases (종류, 별칭키, name_id) VALUES (?,?,?)",
            (dst.종류, src.정규화키, 대표_id),
        )
        등급 = dst.등급 if dst.등급 not in ("", "미분류") else src.등급
        국내해외 = dst.국내해외 if dst.국내해외 not in ("", "불명") else src.국내해외
        유형 = dst.유형 if dst.유형 not in ("", "불명") else src.유형
        IF = dst.IF or src.IF
        # 자동 정리에서는 CV 에 더 많이 나온 표기를 대표로 삼는다.
        # (담당자가 직접 묶는 merge() 는 고른 쪽을 그대로 대표로 둔다.)
        표시명 = dst.표시명 if dst.발견횟수 >= src.발견횟수 else src.표시명
        self._conn.execute(
            "UPDATE names SET 표시명=?, 등급=?, 국내해외=?, 유형=?, IF=?,"
            " 발견횟수=발견횟수+? WHERE id=?",
            (표시명, 등급, 국내해외, 유형, IF, src.발견횟수, 대표_id),
        )
        self._conn.execute("DELETE FROM names WHERE id=?", (src.id,))

    def _dedupe(self) -> None:
        """같은 정규화키가 여러 줄이면 하나로 합친다.

        학회/저널이 따로 관리되던 시절에 같은 이름이 양쪽에 하나씩 생겼다.
        발견횟수가 많은 쪽을 남기고, 지워지는 쪽 표기는 별칭으로 남겨
        앞으로 같은 표기가 들어와도 남은 항목으로 해석되게 한다.
        """
        중복 = self._conn.execute(
            "SELECT 종류, 정규화키 FROM names GROUP BY 종류, 정규화키 HAVING COUNT(*) > 1"
        ).fetchall()
        for 종류, 키 in [(r["종류"], r["정규화키"]) for r in 중복]:
            rows = [
                Name(**dict(r)) for r in self._conn.execute(
                    "SELECT * FROM names WHERE 종류=? AND 정규화키=?"
                    " ORDER BY 발견횟수 DESC, id", (종류, 키)
                )
            ]
            for r in rows[1:]:
                self._absorb(r, rows[0].id)
        self.merge_same_display()

    def merge_same_display(self, 종류: str | None = None) -> list[tuple[str, int]]:
        """대표명이 똑같은 항목들을 하나로 합친다.

        담당자가 '포항공과대학교' 와 'POSTECH' 을 각각 '포항공대' 로 고쳐 놓으면
        정규화키는 서로 달라서 표에 **같은 이름이 여러 줄** 남는다. 발견 횟수도
        따로 세어져 어느 게 진짜인지 알 수 없다. 이름이 같다는 건 같은 대상이라는
        뜻이므로 합친다 (지워지는 쪽 표기는 별칭으로 남아 계속 해석된다).

        Returns:
            [(대표명, 합친 줄 수), ...] — 화면에 알려줄 용도
        """
        종류 = canonical_kind(종류) if 종류 else 종류
        묶음: dict[tuple[str, str], list[Name]] = {}
        for n in self.list_all(종류):
            묶음.setdefault((n.종류, n.표시명.strip().casefold()), []).append(n)

        합친것: list[tuple[str, int]] = []
        for rows in 묶음.values():
            if len(rows) < 2:
                continue
            rows.sort(key=lambda n: (-n.발견횟수, n.id))
            for r in rows[1:]:
                self._absorb(r, rows[0].id)
            합친것.append((rows[0].표시명, len(rows)))
        if 합친것:
            self._conn.commit()
        return 합친것

    def _migrate_from_venues(self) -> None:
        """예전 venues 표가 같은 파일에 있으면 학회로 옮겨 담는다."""
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='venues'"
        ).fetchone()
        if not exists:
            return
        already = self._conn.execute("SELECT COUNT(*) c FROM names").fetchone()["c"]
        if already:
            return
        for row in self._conn.execute("SELECT * FROM venues"):
            종류 = "저널" if (row["유형"] or "") == "저널" else "학회"
            self._conn.execute(
                "INSERT OR IGNORE INTO names"
                " (종류,정규화키,표시명,등급,국내해외,발견횟수,최초등록)"
                " VALUES (?,?,?,?,?,?,?)",
                (종류, row["정규화키"], row["표시명"], row["등급"],
                 row["국내해외"], row["발견횟수"], row["최초등록"]),
            )

    # -- 조회 -------------------------------------------------------------
    def _resolve_id(self, 종류: str, key: str) -> int | None:
        row = self._conn.execute(
            "SELECT name_id FROM name_aliases WHERE 종류=? AND 별칭키=?", (종류, key)
        ).fetchone()
        if row:
            return row["name_id"]
        row = self._conn.execute(
            "SELECT id FROM names WHERE 종류=? AND 정규화키=?", (종류, key)
        ).fetchone()
        return row["id"] if row else None

    def get(self, name_id: int) -> Name | None:
        row = self._conn.execute("SELECT * FROM names WHERE id=?", (name_id,)).fetchone()
        return Name(**dict(row)) if row else None

    def lookup(self, 종류: str, 원문: str) -> Name | None:
        """등록하지 않고 조회만. 없으면 None."""
        종류 = canonical_kind(종류)
        key = normalize(원문, 종류)
        if not key:
            return None
        nid = self._resolve_id(종류, key)
        return self.get(nid) if nid else None

    def display(self, 종류: str, 원문: str) -> str:
        """화면·엑셀에 보여줄 대표명. 사전에 없으면 원문 그대로."""
        found = self.lookup(종류, 원문)
        return found.표시명 if found else (원문 or "").strip()

    def list_all(self, 종류: str | None = None) -> list[Name]:
        종류 = canonical_kind(종류) if 종류 else 종류
        sql = "SELECT * FROM names"
        args: tuple = ()
        if 종류:
            sql += " WHERE 종류=?"
            args = (종류,)
        sql += " ORDER BY (등급='미분류') DESC, 발견횟수 DESC, 표시명"
        return [Name(**dict(r)) for r in self._conn.execute(sql, args)]

    def aliases_of(self, name_id: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT 별칭키 FROM name_aliases WHERE name_id=?", (name_id,)
        ).fetchall()
        return [r["별칭키"] for r in rows]

    def unclassified_count(self, 종류: str | None = None) -> int:
        sql = "SELECT COUNT(*) c FROM names WHERE 등급='미분류' AND 종류 IN (%s)" % (
            ",".join("?" * len(GRADED_KINDS))
        )
        args = list(GRADED_KINDS)
        종류 = canonical_kind(종류) if 종류 else 종류
        if 종류:
            sql += " AND 종류=?"
            args.append(종류)
        return self._conn.execute(sql, args).fetchone()["c"]

    # -- 등록 -------------------------------------------------------------
    def observe(self, 종류: str, 표시명: str, *, 국내해외: str = "불명",
                유형: str = "") -> Name:
        """CV 에서 표기를 발견했을 때. 없으면 자동 등록하고 발견횟수를 센다.

        '학회' 나 '저널' 로 부르면 종류는 '학회·저널' 하나로 합쳐지고,
        어느 쪽이었는지는 유형 열에 남는다.
        """
        유형 = 유형 or (종류 if 종류 in SUBTYPES else "")
        종류 = canonical_kind(종류)
        if 종류 not in KINDS:
            raise ValueError(f"알 수 없는 종류: {종류}")
        key = normalize(표시명, 종류)
        if not key:
            raise ValueError(f"정규화 결과가 비어 있습니다: {표시명!r}")

        nid = self._resolve_id(종류, key)
        if nid is None:
            등급 = "미분류" if 종류 in GRADED_KINDS else ""
            cur = self._conn.execute(
                "INSERT INTO names"
                " (종류,정규화키,표시명,등급,국내해외,발견횟수,최초등록,유형)"
                " VALUES (?,?,?,?,?,1,?,?)",
                (종류, key, 표시명.strip(), 등급, 국내해외,
                 now_kst().strftime("%Y-%m-%d %H:%M:%S"), 유형 or "불명"),
            )
            nid = cur.lastrowid
        else:
            self._conn.execute("UPDATE names SET 발견횟수=발견횟수+1 WHERE id=?", (nid,))
            # 유형을 모르던 항목에 학회/저널 정보가 들어오면 채운다
            if 유형 in ("학회", "저널"):
                self._conn.execute(
                    "UPDATE names SET 유형=? WHERE id=? AND (유형 IS NULL OR 유형 IN ('','불명'))",
                    (유형, nid),
                )
        self._conn.commit()
        found = self.get(nid)
        assert found is not None
        return found

    def classify(
        self,
        name_id: int,
        *,
        표시명: str | None = None,
        등급: str | None = None,
        국내해외: str | None = None,
        유형: str | None = None,
        IF: str | None = None,
    ) -> None:
        """담당자가 대표명·등급·국내해외·유형·IF 를 지정한다.

        IF 는 빈 값으로 지울 수 있어야 해서 다른 항목과 달리 공백도 저장한다.
        """
        sets, args = [], []
        for col, val in (("표시명", 표시명), ("등급", 등급),
                         ("국내해외", 국내해외), ("유형", 유형)):
            if val is not None and str(val).strip():
                sets.append(f"{col}=?")
                args.append(str(val).strip())
        if IF is not None:
            sets.append("IF=?")
            args.append(str(IF).strip())
        if not sets:
            return
        args.append(name_id)
        self._conn.execute(f"UPDATE names SET {','.join(sets)} WHERE id=?", args)
        self._conn.commit()

    def merge(self, 별칭_id: int, 대표_id: int) -> None:
        """서로 다른 표기를 하나로 묶는다. 별칭 쪽 표기는 대표로 해석된다."""
        if 별칭_id == 대표_id:
            return
        src, dst = self.get(별칭_id), self.get(대표_id)
        if src is None or dst is None:
            raise ValueError("존재하지 않는 항목")
        if src.종류 != dst.종류:
            raise ValueError("종류가 다른 항목은 묶을 수 없습니다")

        # 별칭이 달고 있던 별칭들도 함께 옮긴다
        self._conn.execute(
            "UPDATE name_aliases SET name_id=? WHERE name_id=?", (대표_id, 별칭_id)
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO name_aliases (종류, 별칭키, name_id) VALUES (?,?,?)",
            (src.종류, src.정규화키, 대표_id),
        )
        self._conn.execute(
            "UPDATE names SET 발견횟수=발견횟수+? WHERE id=?", (src.발견횟수, 대표_id)
        )
        # 분류해 둔 정보는 값이 있는 쪽을 살린다 (빈 대표가 채워진 별칭을 덮지 않게)
        if dst.등급 in ("", "미분류") and src.등급 not in ("", "미분류"):
            self._conn.execute("UPDATE names SET 등급=? WHERE id=?", (src.등급, 대표_id))
        if dst.국내해외 in ("", "불명") and src.국내해외 not in ("", "불명"):
            self._conn.execute(
                "UPDATE names SET 국내해외=? WHERE id=?", (src.국내해외, 대표_id))
        if dst.유형 in ("", "불명") and src.유형 not in ("", "불명"):
            self._conn.execute("UPDATE names SET 유형=? WHERE id=?", (src.유형, 대표_id))
        if not dst.IF and src.IF:
            self._conn.execute("UPDATE names SET IF=? WHERE id=?", (src.IF, 대표_id))
        self._conn.execute("DELETE FROM names WHERE id=?", (별칭_id,))
        self._conn.commit()

    # -- 등급 관리 ---------------------------------------------------------
    def tiers(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM tiers ORDER BY 순서, 이름"))

    def tier_names(self) -> list[str]:
        return [r["이름"] for r in self.tiers()]

    def column_tiers(self) -> list[str]:
        """표에 개수 열로 낼 등급. 기본은 최우수·우수."""
        return [
            r["이름"]
            for r in self._conn.execute(
                "SELECT 이름 FROM tiers WHERE 표에_표시=1 ORDER BY 순서, 이름"
            )
        ]

    def set_tier_column(self, 이름: str, 표시: bool) -> None:
        self._conn.execute("UPDATE tiers SET 표에_표시=? WHERE 이름=?", (1 if 표시 else 0, 이름))
        self._conn.commit()

    def add_tier(self, 이름: str, 순서: int = 99, 표에_표시: bool = False) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO tiers (이름, 순서, 표에_표시) VALUES (?,?,?)",
            (이름.strip(), 순서, 1 if 표에_표시 else 0),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def observe_record(rec, registry: NameRegistry) -> list[str]:
    """지원자 레코드에 나온 이름들을 사전에 등록만 한다.

    ⚠️ 레코드의 값은 **고치지 않는다.** 예전 방식은 등급·국내해외를 레코드에
    써넣어서, 나중에 관리화면에서 분류를 바꿔도 이미 등록된 표에 반영되지
    않았다. 이제 표시할 때마다 사전을 다시 읽는다(CVRecord.to_row).

    Returns:
        아직 분류되지 않은 학회/저널 표시명 목록 (검토 필요 표시용)
    """
    from .normalize import MULTI_SEP
    from .schemas import NAME_COLUMNS

    for col, 종류 in NAME_COLUMNS.items():
        raw = str(getattr(rec, col, "") or "")
        for part in raw.split(MULTI_SEP):
            if part.strip():
                try:
                    registry.observe(종류, part)
                except ValueError:
                    pass

    미분류: list[str] = []
    for paper in rec.논문:
        if not (paper.제출처 or "").strip():
            continue
        종류 = "저널" if paper.유형 == "저널" else "학회"
        try:
            found = registry.observe(종류, paper.제출처, 국내해외=paper.국내해외 or "불명")
        except ValueError:
            continue
        if found.등급 == "미분류":
            미분류.append(found.표시명)
    return sorted(set(미분류))
