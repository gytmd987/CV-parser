"""대시보드 — 여러 템플릿으로 만들어 두고 골라 보는 화면.

메일 템플릿과 같은 감각이다. 하나의 대시보드는 **블록**을 쌓아 만든다.

    표(자유)    칸마다 수식을 따로 적는다
    표(축)      행·열 축을 정하고 칸 수식 하나를 {행}{열} 로 반복한다
    숫자        큰 숫자 한 개 + 설명
    글          그냥 적는 글 (제목·주석)
    프로필      한 사람을 정해진 문장 틀로. 여러 명이면 사람 수만큼 반복

계산은 `formula.py`, 프로필 문장은 `profile_form.py` 가 한다. 여기는
**무엇을 저장하고 어떤 순서로 보여줄지**만 안다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .fsutil import secure_dir, secure_file
from .timeutil import now_kst

#: 블록 종류
#: "목록" 이 제일 앞이다 — 사람들이 만들고 싶어 하는 표의 대부분이 이것이다.
#: 한 사람이 한 줄, 열은 만드는 사람이 정한다 (엑셀에서 표를 만들듯이).
#: "축표" 는 피벗(부서 × 단계 인원수)이고, "표" 는 칸을 하나하나 적는 자유표다.
BLOCK_KINDS = ("목록", "축표", "표", "숫자", "글", "프로필")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dashboards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    이름         TEXT NOT NULL UNIQUE,
    설명         TEXT DEFAULT '',
    만든이        TEXT DEFAULT '',
    만든일시      TEXT DEFAULT '',
    수정일시      TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS blocks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dashboard_id INTEGER NOT NULL,
    순서          INTEGER DEFAULT 0,
    종류          TEXT NOT NULL,
    제목          TEXT DEFAULT '',
    설정_json     TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS blocks_dash ON blocks (dashboard_id, 순서);
"""


@dataclass
class Block:
    id: int
    dashboard_id: int
    순서: int
    종류: str
    제목: str
    설정: dict = field(default_factory=dict)

    # -- 표 --------------------------------------------------------------
    @property
    def 행이름(self) -> list[str]:
        return [x for x in (self.설정.get("행") or []) if str(x).strip()]

    @property
    def 열이름(self) -> list[str]:
        return [x for x in (self.설정.get("열") or []) if str(x).strip()]

    @property
    def 칸(self) -> dict[str, str]:
        """자유 표의 칸. 키는 "행\\t열"."""
        return self.설정.get("칸") or {}

    # -- 축 표 ------------------------------------------------------------
    @property
    def 행축(self) -> str:
        return self.설정.get("행축") or ""

    @property
    def 열축(self) -> str:
        return self.설정.get("열축") or ""

    @property
    def 칸수식(self) -> str:
        return self.설정.get("칸수식") or ""

    # -- 숫자 / 글 ---------------------------------------------------------
    @property
    def 수식(self) -> str:
        return self.설정.get("수식") or ""

    @property
    def 글(self) -> str:
        return self.설정.get("글") or ""

    # -- 목록 표 ----------------------------------------------------------
    @property
    def 목록대상(self) -> str:
        return self.설정.get("목록대상") or "지원자"

    @property
    def 목록조건(self) -> str:
        """행을 고르는 수식. `=최종상태="합격"` 처럼 참/거짓을 낸다. 비면 전부."""
        return self.설정.get("목록조건") or ""

    @property
    def 목록열(self) -> list[tuple[str, str]]:
        """[(머리글, 수식)] — **열을 만드는 사람이 정한다.**"""
        return [(str(a), str(b)) for a, b in (self.설정.get("목록열") or [])]

    @property
    def 목록정렬(self) -> str:
        return self.설정.get("목록정렬") or ""

    @property
    def 목록내림차순(self) -> bool:
        return bool(self.설정.get("목록내림차순"))

    @property
    def 목록최대(self) -> int:
        try:
            return max(0, int(self.설정.get("목록최대") or 0))
        except (TypeError, ValueError):
            return 0

    # -- 프로필 -----------------------------------------------------------
    @property
    def 줄틀(self) -> list[tuple[str, str]]:
        """[(라벨, 문장 틀)]"""
        return [(str(a), str(b)) for a, b in (self.설정.get("줄") or [])]

    @property
    def 머리틀(self) -> str:
        return self.설정.get("머리") or ""

    @property
    def 대상조건(self) -> str:
        """누구를 보여줄지. `=LIST(...)` 와 같은 조건 문법."""
        return self.설정.get("대상") or "=LIST(지원자, 열=지원자_ID)"


@dataclass
class Dashboard:
    id: int
    이름: str
    설명: str
    만든이: str
    만든일시: str
    수정일시: str


class DashboardStore:
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

    # -- 대시보드 ------------------------------------------------------------
    def add(self, 이름: str, 만든이: str = "", 설명: str = "") -> int:
        이름 = (이름 or "").strip()
        if not 이름:
            raise ValueError("대시보드 이름을 입력하세요")
        if self.by_name(이름):
            raise ValueError(f"이미 있는 이름입니다: {이름}")
        지금 = now_kst().strftime("%Y-%m-%d %H:%M:%S")
        cur = self._conn.execute(
            "INSERT INTO dashboards (이름,설명,만든이,만든일시,수정일시)"
            " VALUES (?,?,?,?,?)",
            (이름, 설명, 만든이, 지금, 지금),
        )
        self._conn.commit()
        return cur.lastrowid

    def all(self) -> list[Dashboard]:
        return [Dashboard(**dict(r)) for r in self._conn.execute(
            "SELECT * FROM dashboards ORDER BY 이름"
        )]

    def get(self, did: int) -> Dashboard | None:
        row = self._conn.execute(
            "SELECT * FROM dashboards WHERE id=?", (did,)
        ).fetchone()
        return Dashboard(**dict(row)) if row else None

    def by_name(self, 이름: str) -> Dashboard | None:
        row = self._conn.execute(
            "SELECT * FROM dashboards WHERE 이름=?", ((이름 or "").strip(),)
        ).fetchone()
        return Dashboard(**dict(row)) if row else None

    def rename(self, did: int, 이름: str, 설명: str | None = None) -> None:
        이름 = (이름 or "").strip()
        if not 이름:
            raise ValueError("대시보드 이름을 입력하세요")
        겹침 = self.by_name(이름)
        if 겹침 and 겹침.id != did:
            raise ValueError(f"이미 있는 이름입니다: {이름}")
        옛 = self.get(did)
        if 옛 is None:
            return
        self._conn.execute(
            "UPDATE dashboards SET 이름=?, 설명=?, 수정일시=? WHERE id=?",
            (이름, 옛.설명 if 설명 is None else 설명,
             now_kst().strftime("%Y-%m-%d %H:%M:%S"), did),
        )
        self._conn.commit()

    def delete(self, did: int) -> str:
        d = self.get(did)
        if d is None:
            return ""
        self._conn.execute("DELETE FROM blocks WHERE dashboard_id=?", (did,))
        self._conn.execute("DELETE FROM dashboards WHERE id=?", (did,))
        self._conn.commit()
        return d.이름

    def copy(self, did: int, 새이름: str, 만든이: str = "") -> int:
        """블록까지 통째로 복제한다. 비슷한 대시보드를 여럿 만들 때 쓴다."""
        새id = self.add(새이름, 만든이=만든이, 설명=(self.get(did).설명 if self.get(did) else ""))
        for b in self.blocks(did):
            self.add_block(새id, b.종류, 제목=b.제목, 설정=b.설정)
        return 새id

    # -- 블록 ---------------------------------------------------------------
    def _touch(self, did: int) -> None:
        self._conn.execute(
            "UPDATE dashboards SET 수정일시=? WHERE id=?",
            (now_kst().strftime("%Y-%m-%d %H:%M:%S"), did),
        )

    def add_block(self, dashboard_id: int, 종류: str, *, 제목: str = "",
                  설정: dict | None = None) -> int:
        if 종류 not in BLOCK_KINDS:
            raise ValueError(f"블록 종류는 {'/'.join(BLOCK_KINDS)} 중 하나여야 합니다")
        순서 = self._conn.execute(
            "SELECT COALESCE(MAX(순서), 0) + 1 AS n FROM blocks WHERE dashboard_id=?",
            (dashboard_id,),
        ).fetchone()["n"]
        cur = self._conn.execute(
            "INSERT INTO blocks (dashboard_id,순서,종류,제목,설정_json)"
            " VALUES (?,?,?,?,?)",
            (dashboard_id, 순서, 종류, 제목,
             json.dumps(설정 or {}, ensure_ascii=False)),
        )
        self._touch(dashboard_id)
        self._conn.commit()
        return cur.lastrowid

    def _row(self, row: sqlite3.Row) -> Block:
        try:
            설정 = json.loads(row["설정_json"] or "{}")
        except json.JSONDecodeError:
            설정 = {}
        return Block(id=row["id"], dashboard_id=row["dashboard_id"],
                     순서=row["순서"], 종류=row["종류"], 제목=row["제목"] or "",
                     설정=설정 if isinstance(설정, dict) else {})

    def blocks(self, dashboard_id: int) -> list[Block]:
        return [self._row(r) for r in self._conn.execute(
            "SELECT * FROM blocks WHERE dashboard_id=? ORDER BY 순서, id",
            (dashboard_id,),
        )]

    def block(self, bid: int) -> Block | None:
        row = self._conn.execute("SELECT * FROM blocks WHERE id=?", (bid,)).fetchone()
        return self._row(row) if row else None

    def save_block(self, bid: int, *, 제목: str | None = None,
                   설정: dict | None = None) -> None:
        b = self.block(bid)
        if b is None:
            return
        self._conn.execute(
            "UPDATE blocks SET 제목=?, 설정_json=? WHERE id=?",
            (b.제목 if 제목 is None else 제목,
             json.dumps(b.설정 if 설정 is None else 설정, ensure_ascii=False), bid),
        )
        self._touch(b.dashboard_id)
        self._conn.commit()

    def move_block(self, bid: int, 방향: int) -> None:
        """위(-1)/아래(+1)로 한 칸. 순서를 다시 매겨 틈이 생기지 않게 한다."""
        b = self.block(bid)
        if b is None:
            return
        형제 = self.blocks(b.dashboard_id)
        자리 = [x.id for x in 형제].index(bid)
        새자리 = max(0, min(len(형제) - 1, 자리 + 방향))
        if 새자리 == 자리:
            return
        형제.insert(새자리, 형제.pop(자리))
        for i, x in enumerate(형제, start=1):
            self._conn.execute("UPDATE blocks SET 순서=? WHERE id=?", (i, x.id))
        self._touch(b.dashboard_id)
        self._conn.commit()

    def delete_block(self, bid: int) -> str:
        b = self.block(bid)
        if b is None:
            return ""
        self._conn.execute("DELETE FROM blocks WHERE id=?", (bid,))
        self._touch(b.dashboard_id)
        self._conn.commit()
        return b.제목 or b.종류

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# 계산 — 블록을 화면에 그릴 수 있는 모양으로
# ---------------------------------------------------------------------------
#: 축으로 쓸 수 있는 것. 값 목록은 바깥에서 넣어 준다 (부서·과제는 조직에서,
#: 단계·최종상태는 채용 설정에서 온다). 여기서 DB 를 직접 읽지 않는다.
AXIS_SOURCES = ("부서", "과제", "단계", "최종상태", "등록년도", "현재_신분", "직접 입력")

#: 칸 값에 입히는 형식
CELL_FORMATS = ("그대로", "정수", "소수1", "퍼센트", "쉼표", "명")


def format_cell(글: str, 값, 형식: str) -> str:
    """수식 결과에 보기 형식을 입힌다. 못 바꾸면 원래 글 그대로 둔다."""
    if 형식 in ("", "그대로") or 값 is None:
        return 글
    if isinstance(값, list):
        return 글
    try:
        n = float(값)
    except (TypeError, ValueError):
        return 글
    if 형식 == "정수":
        return f"{round(n):d}"
    if 형식 == "소수1":
        return f"{n:.1f}"
    if 형식 == "퍼센트":
        return f"{n:.1f}%"
    if 형식 == "쉼표":
        return f"{round(n):,d}"
    if 형식 == "명":
        return f"{round(n):,d}명"
    return 글


@dataclass
class RenderedTable:
    제목: str
    머리: list[str]
    행: list[tuple[str, list[str]]]      # (행 이름, 칸들)
    오류: list[str] = field(default_factory=list)


@dataclass
class RenderedProfile:
    제목: str
    사람: list[tuple[str, list[tuple[str, str]]]] = field(default_factory=list)
    오류: list[str] = field(default_factory=list)


@dataclass
class RenderedList:
    제목: str
    머리: list[str] = field(default_factory=list)
    행: list[list[str]] = field(default_factory=list)
    오류: list[str] = field(default_factory=list)
    전체: int = 0                      # 조건에 맞는 사람 수 (줄여 보여줄 때)


def render_list(b: Block, rows, 아는열: set[str] | None = None) -> RenderedList:
    """목록 표 — **한 사람이 한 줄, 열은 만드는 사람이 정한다.**

    피벗(축표)으로는 만들 수 없는 표가 대부분이다. "채용 중인 사람을 줄로 놓고
    옆에 이것저것 붙이고 싶다" 가 사람들이 실제로 만들려는 것이고, 그건 집계가
    아니라 목록이다.

    행 고르기·열 값·정렬이 **전부 같은 수식 언어**(`expr`)를 쓴다. 행 문맥이라
    열 이름은 그 사람의 값을 뜻한다.

    행 값은 화면들이 이미 만들어 둔 것을 그대로 쓴다(`Rows`). 사람마다 DB 를
    다시 읽으면 백 명짜리 표에서 백 번을 읽게 된다.
    """
    from . import expr

    오류: list[str] = []
    열들 = [(머리, 식) for 머리, 식 in b.목록열 if str(식).strip()]
    if not 열들:
        return RenderedList(제목=b.제목, 오류=["열이 없습니다. 아래에서 열을 추가하세요."])

    골라낸 = []
    for r in rows.of(b.목록대상):
        값들 = {k: ("" if v is None else str(v)) for k, v in r.items()}
        cid = 값들.get("지원자_ID") or ""
        if b.목록조건.strip():
            보임, 잘못 = expr.render(b.목록조건, 값들)
            if 잘못:
                return RenderedList(제목=b.제목,
                                    오류=[f"행 고르기 → {잘못}"])
            if str(보임).strip().upper() in ("", "FALSE", "0"):
                continue
        골라낸.append((cid, 값들))

    if b.목록정렬.strip():
        def 열쇠(짝):
            보임, 잘못 = expr.render(b.목록정렬, 짝[1])
            if 잘못:
                return ""
            # 숫자로 읽히면 숫자로 (문자열 정렬이면 10 이 9 보다 앞에 온다)
            try:
                return (0, float(str(보임).replace(",", "")), "")
            except ValueError:
                return (1, 0.0, str(보임))
        try:
            골라낸.sort(key=열쇠, reverse=b.목록내림차순)
        except TypeError:
            pass

    전체 = len(골라낸)
    if b.목록최대:
        골라낸 = 골라낸[:b.목록최대]

    표행 = []
    본오류 = set()
    for _cid, 값들 in 골라낸:
        칸들 = []
        for 머리, 식 in 열들:
            if not expr.is_formula(식):
                칸들.append(식)                  # 그냥 글자는 그대로
                continue
            보임, 잘못 = expr.render(식, 값들)
            if 잘못:
                칸들.append("?")
                본오류.add(f"'{머리 or 식}' → {잘못}")
            else:
                칸들.append(보임)
        표행.append(칸들)

    오류 += sorted(본오류)
    if 아는열 is not None:
        쓴열: set[str] = set()
        for 식 in [식 for _머리, 식 in 열들] + [b.목록조건, b.목록정렬]:
            if not expr.is_formula(식):
                continue
            try:
                쓴열 |= set(expr.columns(식))
            except expr.ExprError:
                pass                             # 문법 오류는 저장할 때 걸린다
        모르는 = sorted(c for c in 쓴열 if c not in 아는열)
        if 모르는:
            오류.append("표에 없는 열입니다: " + ", ".join(모르는))

    머리 = [머리 or 식 for 머리, 식 in 열들]
    return RenderedList(제목=b.제목, 머리=머리, 행=표행, 오류=오류, 전체=전체)


def render_table(b: Block, rows, 축값: dict[str, list[str]],
                 아는열: set[str] | None = None) -> RenderedTable:
    """자유 표 · 축 표를 계산한다.

    수식이 아닌 칸(그냥 글자)은 그대로 나간다. 틀린 수식은 칸에 `?` 를 두고
    무엇이 틀렸는지 위에 모아 적는다 — 조용히 0 을 띄우면 안 된다.
    """
    from . import formula as F

    형식 = b.설정.get("형식") or "그대로"
    오류: list[str] = []

    def 한칸(수식: str) -> str:
        수식 = (수식 or "").strip()
        if not 수식:
            return ""
        if not F.is_formula(수식):
            return 수식
        try:
            글, 값 = F.run(수식, rows, 아는열)
        except F.FormulaError as exc:
            메시지 = f"{수식} → {exc}"
            if 메시지 not in 오류:
                오류.append(메시지)
            return "?"
        return format_cell(글, 값, 형식)

    if b.종류 == "축표":
        행들 = 축값.get(b.행축, []) if b.행축 != "직접 입력" else b.행이름
        열들 = 축값.get(b.열축, []) if b.열축 != "직접 입력" else b.열이름
        열들 = 열들 or [""]
        나온행 = []
        for r in 행들:
            칸들 = []
            for c in 열들:
                수식 = b.칸수식.replace("{행}", str(r)).replace("{열}", str(c))
                칸들.append(한칸(수식))
            나온행.append((str(r), 칸들))
        return RenderedTable(제목=b.제목, 머리=[str(c) for c in 열들],
                             행=나온행, 오류=오류)

    나온행 = []
    for r in b.행이름:
        칸들 = [한칸(b.칸.get(f"{r}\t{c}", "")) for c in b.열이름]
        나온행.append((str(r), 칸들))
    return RenderedTable(제목=b.제목, 머리=[str(c) for c in b.열이름],
                         행=나온행, 오류=오류)


def render_profile(b: Block, rows, 값찾기, 아는열: set[str] | None = None
                   ) -> RenderedProfile:
    """프로필 블록 — 조건에 맞는 사람마다 문장 틀을 채운다.

    값찾기(지원자_ID) -> {열: 값} 은 바깥에서 넣는다. 여기서 DB 를 안 읽는다.
    """
    from . import formula as F
    from . import profile_form as P

    오류: list[str] = []
    try:
        f = F.parse(b.대상조건)
        f.열 = "지원자_ID"
        _글, ids = F.evaluate(f, rows)
    except F.FormulaError as exc:
        return RenderedProfile(제목=b.제목, 오류=[f"{b.대상조건} → {exc}"])

    사람 = []
    for cid in (ids if isinstance(ids, list) else []):
        값들 = 값찾기(cid)
        if not 값들:
            continue
        머리 = P.render(b.머리틀, 값들) if b.머리틀 else ""
        줄들 = P.render_rows(b.줄틀, 값들)
        if 머리 or 줄들:
            사람.append((머리 or cid, 줄들))
    if 아는열 is not None:
        쓴열 = {c for _라벨, 틀 in b.줄틀 for c in P.columns(틀)}
        쓴열 |= set(P.columns(b.머리틀))
        모르는 = sorted(c for c in 쓴열 if c not in 아는열)
        if 모르는:
            오류.append("표에 없는 열입니다: " + ", ".join(모르는))
    return RenderedProfile(제목=b.제목, 사람=사람, 오류=오류)
