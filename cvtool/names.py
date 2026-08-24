"""명칭 사전 — CV 에 적힌 표기마다 '표에 보일 이름' 을 정한다.

같은 소속(학교·회사)이 이렇게 들어온다:
    포항공대 / POSTECH / 포항공과대학교 / 포항공과대학교(POSTECH)
    (주)가나다소프트 / 가나다소프트 / 가나다소프트웨어
같은 학회도 마찬가지다:
    International Conference on Machine Learning / ICML / Proc. of ICML 2023

소속·학회·저널·전공이 **모두 같은 문제**라서 한 구조로 처리한다.
종류(kind)만 다르고 화면도 같은 패턴을 쓴다.

구조가 두 층이다.

    표기(names)            : CV 에서 발견한 **원래 표기 하나당 한 줄**
    분류(name_classes)     : '표에 보일 이름' 하나당 등급·국내해외·유형·IF

원래 표기를 절대 감추지 않는 게 핵심이다. 예전에는 여러 표기를 한 줄로 합치고
대표명만 남겨서, 잘못 분류한 걸 나중에 알아채도 **무엇이 잘못 들어갔는지 볼 수도,
떼어낼 수도 없었다.** 지금은 표기마다 줄이 있어서 그 줄의 이름만 고치면 된다.

등급 같은 분류는 표기가 아니라 **이름에 붙는다.** 'ICML' 은 어떻게 적혀 있었든
최우수다. 그래서 같은 이름을 쓰는 표기들은 분류를 저절로 함께 쓴다.

핵심 원칙: **지원자 레코드의 원문 표기는 절대 바꾸지 않는다.**
CV 에 적힌 그대로 저장하고, 화면·엑셀에서 보여줄 때만 사전을 거친다.
그래야 관리화면에서 이름을 고치면 **이미 등록된 표에도 즉시 반영**된다.
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
    """표기를 묶기용 키로 바꾼다.

    이 키는 **처음 보는 표기의 이름을 뭘로 할지 정할 때만** 쓴다.
    키가 같은 표기가 이미 있으면 그 이름을 그대로 물려받는다
    ('ICML 2023' 이 새로 들어와도 'ICML' 로 보이게).
    한 번 등록된 뒤에는 사람이 정한 이름이 우선이고 키는 참고용이다.

    괄호 안 내용은 어느 종류든 떼어낸다. 학회·저널은 연도와 흔한 군더더기까지
    떼어낸다. 옛 종류 이름('학회'/'저널')으로 불러도 같은 키가 나와야 한다.
    """
    s = _PAREN_RE.sub(" ", raw.strip().lower())
    if canonical_kind(종류) in GRADED_KINDS:
        s = _YEAR_RE.sub(" ", s)
        s = _VENUE_NOISE_RE.sub(" ", s)
    return _NONWORD_RE.sub("", s)


@dataclass
class Name:
    """CV 에서 발견한 표기 한 줄 (+ 그 이름에 붙은 분류)."""

    id: int
    종류: str
    원표기: str                 # CV 에 적힌 그대로. 바뀌지 않는다.
    정규화키: str                # 묶기용 힌트
    표시명: str                  # 표·엑셀에 나갈 이름. 여기만 고친다.
    발견횟수: int
    최초등록: str
    확인자: str = ""             # 사람이 이 줄을 보고 맞다고 한 사람·때.
    확인일시: str = ""            # 비어 있으면 **아직 LLM 이 넣어 둔 그대로**다.
    등급: str = "미분류"          # 아래 넷은 '표시명' 에 붙는 값이다
    국내해외: str = "불명"
    유형: str = "불명"            # 학회 / 저널
    IF: str = ""                 # Impact Factor (저널)

    @property
    def 확인(self) -> bool:
        """사람이 본 줄인가.

        명칭은 처음 등록될 때 CV 에 적힌 표기를 그대로 이름으로 삼고, 등급·
        국내해외는 LLM 이 짐작한 값이다. **짐작과 확정을 눈으로 못 가리면**
        무엇을 더 봐야 하는지 알 수가 없다. 이 값이 그 경계다.
        """
        return bool((self.확인일시 or "").strip())

    def google_url(self, 무엇: str = "impact factor") -> str:
        """IF 를 찾아보기 쉽게 검색어를 미리 채운 구글 링크."""
        from urllib.parse import quote_plus

        return f"https://www.google.com/search?q={quote_plus(f'{self.표시명} {무엇}'.strip())}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS names (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    종류         TEXT NOT NULL,
    원표기        TEXT NOT NULL,
    정규화키      TEXT NOT NULL,
    표시명        TEXT NOT NULL,
    발견횟수      INTEGER DEFAULT 0,
    최초등록      TEXT DEFAULT '',
    확인자        TEXT DEFAULT '',
    확인일시      TEXT DEFAULT '',
    UNIQUE(종류, 원표기)
);
CREATE INDEX IF NOT EXISTS names_key ON names (종류, 정규화키);
CREATE TABLE IF NOT EXISTS name_classes (
    종류         TEXT NOT NULL,
    표시명        TEXT NOT NULL,
    등급         TEXT DEFAULT '미분류',
    국내해외      TEXT DEFAULT '불명',
    유형         TEXT DEFAULT '불명',
    IF          TEXT DEFAULT '',
    PRIMARY KEY (종류, 표시명)
);
CREATE TABLE IF NOT EXISTS tiers (
    이름         TEXT PRIMARY KEY,
    순서         INTEGER DEFAULT 99,
    표에_표시     INTEGER DEFAULT 0
);
"""

_CLASS_COLS = ("등급", "국내해외", "유형", "IF")


class NameRegistry:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        secure_dir(self.path.parent)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 여러 사람이 동시에 써도 읽기가 막히지 않게 한다
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        self._conn.executescript(_SCHEMA)
        self._add_missing_columns()
        self._seed_tiers()
        self._conn.commit()
        for suffix in ("", "-wal", "-shm"):
            secure_file(Path(str(self.path) + suffix))

    # -- 마이그레이션 -------------------------------------------------------
    def _table_cols(self, table: str) -> set[str]:
        return {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}

    def _add_missing_columns(self) -> None:
        """쓰던 DB 에 나중에 생긴 열을 붙인다.

        이미 사전을 채워 둔 서버에서 열이 없다고 죽으면 안 되고, 그렇다고
        표를 다시 만들면 그동안 고쳐 둔 이름이 날아간다. 없는 것만 붙인다.
        """
        있는열 = self._table_cols("names")
        for 열 in ("확인자", "확인일시"):
            if 열 not in 있는열:
                self._conn.execute(f"ALTER TABLE names ADD COLUMN {열} TEXT DEFAULT ''")

    def _migrate(self) -> None:
        """예전 구조(한 줄 = 대표명 하나 + 별칭표)를 표기별 한 줄로 옮긴다."""
        있는표 = {
            r["name"] for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "names" not in 있는표:
            self._migrate_from_venues(있는표)
            return
        cols = self._table_cols("names")
        if "원표기" in cols and "등급" not in cols:
            return                              # 이미 새 구조

        옛행 = [dict(r) for r in self._conn.execute("SELECT * FROM names")]
        옛별칭 = (
            [dict(r) for r in self._conn.execute("SELECT * FROM name_aliases")]
            if "name_aliases" in 있는표 else []
        )
        self._conn.execute("ALTER TABLE names RENAME TO names_old")
        self._conn.executescript(_SCHEMA)

        별칭들: dict[int, list[str]] = {}
        for a in 옛별칭:
            별칭들.setdefault(a["name_id"], []).append(a["별칭키"])

        for row in 옛행:
            종류 = canonical_kind(row.get("종류") or "")
            표시명 = (row.get("표시명") or "").strip()
            키 = row.get("정규화키") or normalize(표시명, 종류)
            # 원표기가 없던 시절 행은 이름을 고쳤는지로 판단한다.
            # 안 고쳤으면 이름이 곧 원래 표기, 고쳤으면 키가 유일한 흔적이다.
            원표기 = (row.get("원표기") or "").strip()
            if not 원표기:
                원표기 = 표시명 if normalize(표시명, 종류) == 키 else 키
            self._put(종류, 원표기, 키, 표시명, row.get("발견횟수") or 0,
                      row.get("최초등록") or "")
            # 별칭으로 숨어 있던 표기들도 각자 줄로 되살린다
            for 별칭키 in 별칭들.get(row.get("id"), []):
                self._put(종류, 별칭키, 별칭키, 표시명, 0, row.get("최초등록") or "")
            self._set_class(
                종류, 표시명,
                등급=row.get("등급") or ("미분류" if 종류 in GRADED_KINDS else ""),
                국내해외=row.get("국내해외") or "불명",
                유형=row.get("유형") or "불명",
                IF=row.get("IF") or "",
            )
        self._conn.execute("DROP TABLE names_old")
        if "name_aliases" in 있는표:
            self._conn.execute("DROP TABLE name_aliases")
        self._conn.commit()

    def _migrate_from_venues(self, 있는표: set[str]) -> None:
        """아주 예전 venues 표가 있으면 학회·저널로 옮겨 담는다."""
        self._conn.executescript(_SCHEMA)
        if "venues" not in 있는표:
            return
        for row in self._conn.execute("SELECT * FROM venues"):
            표시명 = row["표시명"]
            self._put("학회·저널", 표시명, row["정규화키"], 표시명,
                      row["발견횟수"] or 0, row["최초등록"] or "")
            self._set_class("학회·저널", 표시명, 등급=row["등급"] or "미분류",
                            국내해외=row["국내해외"] or "불명",
                            유형="저널" if (row["유형"] or "") == "저널" else "학회")
        self._conn.commit()

    def _put(self, 종류: str, 원표기: str, 키: str, 표시명: str,
             발견횟수: int, 최초등록: str) -> None:
        원표기 = (원표기 or "").strip()
        if not 원표기:
            return
        self._conn.execute(
            "INSERT INTO names (종류,원표기,정규화키,표시명,발견횟수,최초등록)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(종류,원표기) DO UPDATE SET 발견횟수=발견횟수+excluded.발견횟수",
            (종류, 원표기, 키, 표시명 or 원표기, 발견횟수, 최초등록),
        )

    def _seed_tiers(self) -> None:
        for 이름, 순서, 표시 in DEFAULT_TIERS:
            self._conn.execute(
                "INSERT OR IGNORE INTO tiers (이름, 순서, 표에_표시) VALUES (?,?,?)",
                (이름, 순서, 표시),
            )

    # -- 분류 (표시명에 붙는 값) --------------------------------------------
    def _class_of(self, 종류: str, 표시명: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM name_classes WHERE 종류=? AND 표시명=?", (종류, 표시명)
        ).fetchone()
        기본 = {
            "등급": "미분류" if 종류 in GRADED_KINDS else "",
            "국내해외": "불명", "유형": "불명", "IF": "",
        }
        return {**기본, **{k: row[k] for k in _CLASS_COLS}} if row else 기본

    def _set_class(self, 종류: str, 표시명: str, **값들) -> None:
        현재 = self._class_of(종류, 표시명)
        새것 = {k: (값들.get(k) if 값들.get(k) is not None else 현재[k]) for k in _CLASS_COLS}
        self._conn.execute(
            "INSERT INTO name_classes (종류,표시명,등급,국내해외,유형,IF) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(종류,표시명) DO UPDATE SET"
            " 등급=excluded.등급, 국내해외=excluded.국내해외,"
            " 유형=excluded.유형, IF=excluded.IF",
            (종류, 표시명, 새것["등급"], 새것["국내해외"], 새것["유형"], 새것["IF"]),
        )

    def _row(self, row: sqlite3.Row) -> Name:
        d = dict(row)
        return Name(**d, **self._class_of(d["종류"], d["표시명"]))

    # -- 조회 ---------------------------------------------------------------
    def get(self, name_id: int) -> Name | None:
        row = self._conn.execute("SELECT * FROM names WHERE id=?", (name_id,)).fetchone()
        return self._row(row) if row else None

    def lookup(self, 종류: str, 원문: str) -> Name | None:
        """등록하지 않고 조회만. 없으면 None.

        CV 에 적힌 그대로 먼저 찾고, 없으면 묶기 키로 찾는다.
        (`ICML 2024` 처럼 처음 보는 표기도 `ICML` 로 보이게 한다.)
        """
        종류 = canonical_kind(종류)
        표기 = (원문 or "").strip()
        if not 표기:
            return None
        row = self._conn.execute(
            "SELECT * FROM names WHERE 종류=? AND 원표기=?", (종류, 표기)
        ).fetchone()
        if row:
            return self._row(row)
        키 = normalize(표기, 종류)
        if not 키:
            return None
        row = self._conn.execute(
            "SELECT * FROM names WHERE 종류=? AND 정규화키=?"
            " ORDER BY 발견횟수 DESC, id LIMIT 1", (종류, 키)
        ).fetchone()
        return self._row(row) if row else None

    def display(self, 종류: str, 원문: str) -> str:
        """화면·엑셀에 보여줄 이름. 사전에 없으면 원문 그대로."""
        found = self.lookup(종류, 원문)
        return found.표시명 if found else (원문 or "").strip()

    def list_all(self, 종류: str | None = None) -> list[Name]:
        """사전이니까 **표에 보일 이름 오름차순**이 기본이다."""
        종류 = canonical_kind(종류) if 종류 else 종류
        sql = "SELECT * FROM names"
        args: tuple = ()
        if 종류:
            sql += " WHERE 종류=?"
            args = (종류,)
        sql += " ORDER BY 표시명 COLLATE NOCASE, 원표기 COLLATE NOCASE"
        return [self._row(r) for r in self._conn.execute(sql, args)]

    def same_display_groups(self, 종류: str | None = None) -> dict[str, list[int]]:
        """같은 이름을 쓰는 표기들. {표시명: [id, ...]} — 2개 이상만."""
        묶음: dict[str, list[int]] = {}
        for n in self.list_all(종류):
            묶음.setdefault(n.표시명, []).append(n.id)
        return {이름: ids for 이름, ids in 묶음.items() if len(ids) > 1}

    def siblings(self, name_id: int) -> list[Name]:
        """같은 이름으로 분류된 다른 표기들."""
        나 = self.get(name_id)
        if 나 is None:
            return []
        rows = self._conn.execute(
            "SELECT * FROM names WHERE 종류=? AND 표시명=? AND id<>?"
            " ORDER BY 원표기 COLLATE NOCASE",
            (나.종류, 나.표시명, name_id),
        )
        return [self._row(r) for r in rows]

    def display_names(self, 종류: str) -> list[str]:
        """그 종류에 쓰이고 있는 이름 목록 (오름차순)."""
        rows = self._conn.execute(
            "SELECT DISTINCT 표시명 FROM names WHERE 종류=?"
            " ORDER BY 표시명 COLLATE NOCASE", (canonical_kind(종류),)
        )
        return [r["표시명"] for r in rows]

    def unclassified_count(self, 종류: str | None = None) -> int:
        """아직 등급을 안 매긴 **이름** 수 (표기 수가 아니다)."""
        종류들 = [canonical_kind(종류)] if 종류 else list(GRADED_KINDS)
        총 = 0
        for k in 종류들:
            if k not in GRADED_KINDS:
                continue
            총 += sum(1 for 이름 in self.display_names(k)
                     if self._class_of(k, 이름)["등급"] == "미분류")
        return 총

    # -- 등록 ---------------------------------------------------------------
    def observe(self, 종류: str, 표시명: str, *, 국내해외: str = "불명",
                유형: str = "") -> Name:
        """CV 에서 표기를 발견했을 때.

        **표기마다 한 줄**이다. 처음 보는 표기면 줄을 만들고, 묶기 키가 같은
        표기가 이미 있으면 그 이름을 물려받는다. 이미 있는 표기면 횟수만 센다.
        """
        유형 = 유형 or (종류 if 종류 in SUBTYPES else "")
        종류 = canonical_kind(종류)
        if 종류 not in KINDS:
            raise ValueError(f"알 수 없는 종류: {종류}")
        표기 = (표시명 or "").strip()
        키 = normalize(표기, 종류)
        if not 키:
            raise ValueError(f"정규화 결과가 비어 있습니다: {표시명!r}")

        row = self._conn.execute(
            "SELECT * FROM names WHERE 종류=? AND 원표기=?", (종류, 표기)
        ).fetchone()
        if row:
            self._conn.execute(
                "UPDATE names SET 발견횟수=발견횟수+1 WHERE id=?", (row["id"],)
            )
            이름 = row["표시명"]
            nid = row["id"]
        else:
            형제 = self._conn.execute(
                "SELECT 표시명 FROM names WHERE 종류=? AND 정규화키=?"
                " ORDER BY 발견횟수 DESC, id LIMIT 1", (종류, 키)
            ).fetchone()
            이름 = 형제["표시명"] if 형제 else 표기
            cur = self._conn.execute(
                "INSERT INTO names (종류,원표기,정규화키,표시명,발견횟수,최초등록)"
                " VALUES (?,?,?,?,1,?)",
                (종류, 표기, 키, 이름, now_kst().strftime("%Y-%m-%d %H:%M:%S")),
            )
            nid = cur.lastrowid

        # 분류는 이름에 붙는다. 아직 모르는 값만 채운다.
        현재 = self._class_of(종류, 이름)
        채울것 = {}
        if 국내해외 in ("국내", "해외") and 현재["국내해외"] in ("", "불명"):
            채울것["국내해외"] = 국내해외
        if 유형 in ("학회", "저널") and 현재["유형"] in ("", "불명"):
            채울것["유형"] = 유형
        if 채울것 or 종류 in GRADED_KINDS:
            self._set_class(종류, 이름, **채울것)
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
        """이 표기의 이름을 바꾸거나, 그 이름의 분류를 지정한다.

        등급·국내해외·유형·IF 는 **이름에 붙는다.** 같은 이름을 쓰는 다른 표기도
        같은 값을 쓰게 된다 (ICML 은 어떻게 적혀 있든 최우수다).
        IF 는 빈 값으로 지울 수 있어야 해서 공백도 저장한다.
        """
        나 = self.get(name_id)
        if 나 is None:
            return
        이름 = 나.표시명
        if 표시명 is not None and str(표시명).strip() and str(표시명).strip() != 이름:
            이름 = str(표시명).strip()
            self._conn.execute("UPDATE names SET 표시명=? WHERE id=?", (이름, name_id))
            # 새 이름에 분류가 없으면 쓰던 값을 가져간다
            if not self._conn.execute(
                "SELECT 1 FROM name_classes WHERE 종류=? AND 표시명=?", (나.종류, 이름)
            ).fetchone():
                self._set_class(나.종류, 이름, 등급=나.등급, 국내해외=나.국내해외,
                                유형=나.유형, IF=나.IF)

        값들 = {"등급": 등급, "국내해외": 국내해외, "유형": 유형}
        값들 = {k: v for k, v in 값들.items() if v is not None and str(v).strip()}
        if IF is not None:
            값들["IF"] = str(IF).strip()
        if 값들:
            self._set_class(나.종류, 이름, **값들)
        self._conn.commit()

    def confirm(self, name_id: int, 사람: str = "") -> None:
        """이 표기를 사람이 봤다고 표시한다."""
        self._conn.execute(
            "UPDATE names SET 확인자=?, 확인일시=? WHERE id=?",
            (사람, now_kst().strftime("%Y-%m-%d %H:%M"), name_id),
        )
        self._conn.commit()

    def unconfirm(self, name_id: int) -> None:
        """확인 표시를 뗀다 (다시 봐야 할 것으로 되돌린다)."""
        self._conn.execute(
            "UPDATE names SET 확인자='', 확인일시='' WHERE id=?", (name_id,)
        )
        self._conn.commit()

    def unconfirmed_count(self, 종류: str | None = None) -> int:
        """아직 사람이 안 본 **표기** 수."""
        if 종류:
            return self._conn.execute(
                "SELECT COUNT(*) FROM names WHERE 종류=? AND 확인일시=''",
                (canonical_kind(종류),),
            ).fetchone()[0]
        return self._conn.execute(
            "SELECT COUNT(*) FROM names WHERE 확인일시=''"
        ).fetchone()[0]

    def forget(self, name_id: int) -> str:
        """표기 한 줄을 지운다 (오타로 들어온 것 정리용)."""
        나 = self.get(name_id)
        if 나 is None:
            return ""
        self._conn.execute("DELETE FROM names WHERE id=?", (name_id,))
        self._conn.commit()
        return 나.원표기

    # -- 등급 관리 -----------------------------------------------------------
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
        아직 분류되지 않은 학회/저널 이름 목록 (검토 필요 표시용)
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
