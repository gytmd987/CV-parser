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
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .dbconn import Db, atomic
from .fsutil import secure_dir, secure_file
from .timeutil import now_kst

#: 블록 종류
#: "목록" 이 제일 앞이다 — 사람들이 만들고 싶어 하는 표의 대부분이 이것이다.
#: 한 사람이 한 줄, 열은 만드는 사람이 정한다 (엑셀에서 표를 만들듯이).
#: "축표" 는 피벗(부서 × 단계 인원수)이고, "표" 는 칸을 하나하나 적는 자유표다.
BLOCK_KINDS = ("목록", "축표", "표", "숫자", "글", "프로필", "시트")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dashboards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    이름         TEXT NOT NULL UNIQUE,
    설명         TEXT DEFAULT '',
    너비         TEXT DEFAULT '',
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


#: 시트 격자의 상한. 넘으면 JSON 도 화면도 감당이 안 된다.
SHEET_MAX_ROWS = 100
SHEET_MAX_COLS = 26

#: 시트 칸에 줄 수 있는 값들. **목록 밖의 값은 받지 않는다** — 브라우저가 보낸
#: JSON 이 그대로 style 속성에 들어가므로, 모양을 확인한 것만 내보낸다.
SHEET_FONTS = ("기본", "고딕", "명조", "고정폭")
SHEET_ALIGNS = ("left", "center", "right")
SHEET_MIN_SIZE, SHEET_MAX_SIZE = 8, 48
#: 테두리 선 모양. 칸마다 네 변(위·아래·왼쪽·오른쪽)을 따로 정한다.
SHEET_BORDER_STYLES = ("얇게", "굵게", "점선", "이중")
SHEET_BORDER_SIDES = ("위", "아래", "왼쪽", "오른쪽")
_선CSS = {"얇게": "1px solid", "굵게": "2px solid", "점선": "1px dashed",
         "이중": "3px double"}
_변CSS = {"위": "top", "아래": "bottom", "왼쪽": "left", "오른쪽": "right"}


def _사이(값, 작은: int, 큰: int, 기본: int) -> int:
    try:
        return max(작은, min(큰, int(값)))
    except (TypeError, ValueError):
        return 기본


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

    # -- 시트 -------------------------------------------------------------
    @property
    def 시트행수(self) -> int:
        return _사이(self.설정.get("행수"), 1, SHEET_MAX_ROWS, 10)

    @property
    def 시트열수(self) -> int:
        return _사이(self.설정.get("열수"), 1, SHEET_MAX_COLS, 6)

    @property
    def 시트칸(self) -> dict[str, dict]:
        """{`A1`: {글, 배경, 글자, 굵게, 기울임, 크기, 글꼴, 정렬, 가로병합, 세로병합}}

        자유표의 `칸` 과 달리 **주소(자리)** 를 열쇠로 쓴다. 시트의 열은 이름이
        아니라 자리라서, 열을 하나 끼워 넣으면 뒤가 다 밀리는 게 맞다.
        """
        담긴것 = self.설정.get("시트칸")
        return 담긴것 if isinstance(담긴것, dict) else {}

    @property
    def 시트열너비(self) -> dict[str, str]:
        """{`A`: px}. 축표·자유표의 `열너비` 는 **이름**이 열쇠지만 여기는 자리다."""
        담긴것 = self.설정.get("시트열너비")
        return 담긴것 if isinstance(담긴것, dict) else {}

    @property
    def 시트행높이(self) -> dict[str, str]:
        """{`1`: px}"""
        담긴것 = self.설정.get("시트행높이")
        return 담긴것 if isinstance(담긴것, dict) else {}

    @property
    def 시트보일범위(self) -> tuple[int, int, int, int] | None:
        """보기 화면·엑셀에 내보낼 네모 (r0, c0, r1, c1) — 0부터. 안 정했으면 None.

        편집 화면은 늘 전체를 보인다 (범위 밖에 계산용 칸을 둘 수 있어야 한다).
        """
        return 보일범위풀기(self.설정.get("보일범위"), self.시트행수, self.시트열수)

    @property
    def 시트격자숨김(self) -> bool:
        """보기 화면에서 연한 격자선을 숨긴다 (A·B·1·2 머리글은 보기에 늘 안 나온다).

        보고서처럼 꾸민 시트는 **내가 그은 테두리만** 보여야 읽힌다. 편집 화면은
        늘 격자를 보여준다 (칸을 골라야 하니까).
        """
        return bool(self.설정.get("격자숨김"))

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
    def 목록열(self) -> list[tuple[str, str, str]]:
        """[(머리글, 수식, 너비)] — **열을 만드는 사람이 정한다.**

        너비는 px 숫자거나 빈 문자열(알아서). 예전에 저장한 두 칸짜리 줄도
        그대로 읽는다 — 쓰던 대시보드가 깨지면 안 된다.
        """
        나온것 = []
        for 줄 in (self.설정.get("목록열") or []):
            줄 = list(줄) + ["", "", ""]
            나온것.append((str(줄[0]), str(줄[1]), str(줄[2] or "").strip()))
        return 나온것

    # -- 열 너비 (축표·자유표·프로필) ------------------------------------------
    @property
    def 열너비(self) -> dict[str, str]:
        """{열 이름: px}. 맨 왼쪽 줄 이름 칸은 `""` 을 열쇠로 쓴다.

        **자리 번호가 아니라 이름으로 담는다.** 축표의 열은 축 값(부서 목록
        같은 것)이라 개수가 바뀌는데, 번호로 담으면 부서가 하나 늘 때 정해 둔
        너비가 통째로 한 칸씩 밀린다.

        목록은 여기 안 쓴다 — 거기는 열 정의(머리글·수식) 옆에 `폭` 이 붙어
        있고, 그 자리가 더 자연스럽다.
        """
        것 = self.설정.get("열너비")
        if not isinstance(것, dict):
            return {}
        return {str(k): str(v) for k, v in 것.items() if str(v).strip()}

    # -- 표 모양 (목록·축표·자유표가 함께 쓴다) --------------------------------
    @property
    def 테두리(self) -> str:
        """`격자`(기본) · `가로줄` · `없음`

        기본이 가로줄이던 때가 있었다. 조용해서 좋았지만 열이 여럿이면 **어디까지가
        한 칸인지** 눈으로 자를 수가 없다. 화면의 다른 표들도 전부 격자라, 대시보드만
        다르면 그것대로 어색하다.
        """
        return self.설정.get("테두리") or "격자"

    @property
    def 줄무늬(self) -> bool:
        return bool(self.설정.get("줄무늬"))

    @property
    def 촘촘히(self) -> bool:
        return bool(self.설정.get("촘촘히"))

    @property
    def 표너비(self) -> str:
        """`창에 맞춤` · `내용에 맞춤`

        **목록은 `내용에 맞춤`이 기본이다.** 열을 만드는 사람이 정하니 열두 개도
        되는데, 화면 폭을 억지로 나눠 가지면 전부 잘려서 아무것도 안 읽힌다.
        칸을 줄이지 말고 **가로로 넘기게** 두는 쪽이 낫다.

        축표·자유표는 열이 몇 개 안 되니 화면을 채우는 쪽이 보기 좋다.
        """
        정한것 = self.설정.get("표너비")
        if 정한것 in ("창에 맞춤", "내용에 맞춤"):
            return 정한것
        return "내용에 맞춤" if self.종류 == "목록" else "창에 맞춤"

    @property
    def 머리배경(self) -> str:
        return self.설정.get("머리배경") or ""

    @property
    def 조건서식(self) -> list[dict]:
        """값에 따라 칠하기. [{조건, 대상, 배경, 글자}]

        - **조건** 은 행 문맥 수식이다 (`=최종상태="불합격"`). 참이면 칠한다.
        - **대상** 이 `줄 전체` 면 그 줄을, 열 머리글이면 그 칸만 칠한다.
        - 여러 규칙을 둘 수 있고 **위에서부터 보다가 처음 맞는 것**을 쓴다.
          칸 규칙이 줄 규칙을 이긴다 (더 좁게 가리킨 쪽이 이긴다).
        """
        나온것 = []
        for r in (self.설정.get("조건서식") or []):
            if not isinstance(r, dict):
                continue
            조건 = str(r.get("조건") or "").strip()
            if not 조건:
                continue
            나온것.append({
                "조건": 조건,
                "대상": str(r.get("대상") or ROW_TARGET),
                "배경": str(r.get("배경") or ""),
                "글자": str(r.get("글자") or ""),
            })
        return 나온것

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

    @property
    def 프로필최대(self) -> int:
        """최대 몇 명까지 그릴지. 0 이면 전부."""
        try:
            return max(0, int(self.설정.get("최대") or 0))
        except (TypeError, ValueError):
            return 0

    @property
    def 프로필펼침(self) -> int:
        """처음부터 펼쳐 둘 사람 수. 나머지는 이름 한 줄로 접힌다.

        200명이 전부 펼쳐지면 화면이 50장 길이가 된다. 설정이 없던 옛 블록도
        이 기본값(5명)을 따른다.
        """
        값 = self.설정.get("펼침")
        if 값 in (None, ""):
            return 5
        try:
            return max(0, int(값))
        except (TypeError, ValueError):
            return 5


#: 대시보드 폭. 표가 넓으면 화면을 다 쓰고 싶고, 글이 많으면 좁은 게 읽기 좋다.
WIDTHS = ("보통", "넓게", "좁게")

#: 조건서식의 '대상' 이 이것이면 줄 전체를 칠한다 (아니면 그 이름의 열만).
ROW_TARGET = "줄 전체"
_WIDTH_PX = {"보통": "1600px", "넓게": "100%", "좁게": "1100px"}


@dataclass
class Dashboard:
    id: int
    이름: str
    설명: str
    만든이: str
    만든일시: str
    수정일시: str
    너비: str = ""
    #: 휴지통 — 비어 있으면 살아 있는 대시보드다.
    지운일시: str = ""
    지운이: str = ""
    지운이름: str = ""

    @property
    def 폭(self) -> str:
        """`main` 에 줄 max-width. 안 정했으면 다른 화면과 같은 폭."""
        return _WIDTH_PX.get(self.너비 or "보통", _WIDTH_PX["보통"])


class DashboardStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        secure_dir(self.path.parent)
        self._conn = Db(self.path)
        self._conn.executescript(_SCHEMA)
        # 쓰던 DB 에 나중에 생긴 열을 붙인다. 표를 다시 만들면 만들어 둔
        # 대시보드가 날아간다.
        있는열 = {r["name"] for r in self._conn.execute("PRAGMA table_info(dashboards)")}
        if "너비" not in 있는열:
            self._conn.execute("ALTER TABLE dashboards ADD COLUMN 너비 TEXT DEFAULT ''")
        # 휴지통. 지워도 바로 없애지 않는다 — 대시보드 하나에 표 수십 개를 꾸며
        # 두었다가 잘못 눌러 날리면 되돌릴 길이 없었다.
        for 열 in ("지운일시", "지운이", "지운이름"):
            if 열 not in 있는열:
                self._conn.execute(f"ALTER TABLE dashboards ADD COLUMN {열} TEXT DEFAULT ''")
        블록열 = {r["name"] for r in self._conn.execute("PRAGMA table_info(blocks)")}
        if "지운일시" not in 블록열:
            self._conn.execute("ALTER TABLE blocks ADD COLUMN 지운일시 TEXT DEFAULT ''")
        self._rename_recruit_note()
        self._conn.commit()
        for suffix in ("", "-wal", "-shm"):
            secure_file(Path(str(self.path) + suffix))

    #: 한 번만 도는 이관 표시 (`PRAGMA user_version`).
    #: 1 = 수식 안의 `비고` 를 `채용_비고` 로 옮겼다.
    SCHEMA_VERSION = 1

    #: 수식이 들어 있는 자리. 여기만 바꾼다 — 제목·머리글·`글` 은 사람이 쓴
    #: 글이라 손대면 안 된다.
    _수식칸 = ("수식", "칸수식", "목록조건")

    def _rename_recruit_note(self) -> None:
        """저장된 수식 안의 낱말 `비고` 를 `채용_비고` 로 바꾼다.

        지원자 쪽에 `비고` 가 새로 생기면서 이름이 겹쳤다. 이관 시점에 `비고`
        라고 쓴 수식은 전부 채용 쪽을 가리키던 것이라 이 바꿔치기는 맞다.
        **딱 한 번만** 돈다 — 두 번 돌면 그 뒤에 사람이 새로 쓴 지원자 `비고`
        수식까지 채용 쪽으로 끌어간다.
        """
        판 = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if 판 >= self.SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            return
        낱말 = re.compile(r"(?<![0-9A-Za-z가-힣_])비고(?![0-9A-Za-z가-힣_])")

        def 바꾸기(글: str) -> str:
            return 낱말.sub("채용_비고", str(글 or ""))

        for row in self._conn.execute("SELECT id, 설정_json FROM blocks").fetchall():
            try:
                설정 = json.loads(row["설정_json"] or "{}")
            except json.JSONDecodeError:
                continue
            이전 = json.dumps(설정, ensure_ascii=False, sort_keys=True)
            for 키 in self._수식칸:
                if 설정.get(키):
                    설정[키] = 바꾸기(설정[키])
            if isinstance(설정.get("칸"), dict):
                설정["칸"] = {k: 바꾸기(v) for k, v in 설정["칸"].items()}
            if isinstance(설정.get("목록열"), list):
                설정["목록열"] = [
                    ([줄[0]] + [바꾸기(줄[1])] + list(줄[2:]))
                    if isinstance(줄, list) and len(줄) >= 2 else 줄
                    for 줄 in 설정["목록열"]
                ]
            if json.dumps(설정, ensure_ascii=False, sort_keys=True) != 이전:
                self._conn.execute(
                    "UPDATE blocks SET 설정_json=? WHERE id=?",
                    (json.dumps(설정, ensure_ascii=False), row["id"]),
                )
        self._conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

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
            "SELECT * FROM dashboards WHERE 지운일시='' ORDER BY 이름"
        )]

    def trash(self) -> list[Dashboard]:
        """휴지통에 든 대시보드. 최근에 지운 것부터."""
        return [Dashboard(**dict(r)) for r in self._conn.execute(
            "SELECT * FROM dashboards WHERE 지운일시<>'' ORDER BY 지운일시 DESC"
        )]

    def get(self, did: int, *, 지운것도: bool = False) -> Dashboard | None:
        row = self._conn.execute(
            "SELECT * FROM dashboards WHERE id=?"
            + ("" if 지운것도 else " AND 지운일시=''"), (did,)
        ).fetchone()
        return Dashboard(**dict(row)) if row else None

    def by_name(self, 이름: str) -> Dashboard | None:
        row = self._conn.execute(
            "SELECT * FROM dashboards WHERE 이름=?", ((이름 or "").strip(),)
        ).fetchone()
        return Dashboard(**dict(row)) if row else None

    def set_width(self, did: int, 너비: str) -> None:
        if 너비 not in WIDTHS:
            return
        self._conn.execute("UPDATE dashboards SET 너비=? WHERE id=?", (너비, did))
        self._conn.commit()

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

    @atomic
    def delete(self, did: int, 누가: str = "") -> str:
        """휴지통으로 보낸다. 블록은 그대로 두어 되살리면 전부 돌아온다.

        이름은 비켜 준다 (`이름 ⌫시각`) — 이름이 겹치면 안 되는데, 지운 것 때문에
        같은 이름으로 새로 못 만들면 이상하다. 원래 이름은 `지운이름` 에 둔다.
        """
        d = self.get(did)
        if d is None:
            return ""
        지금 = now_kst().strftime("%Y-%m-%d %H:%M:%S")
        self._conn.execute(
            "UPDATE dashboards SET 지운일시=?, 지운이=?, 지운이름=?, 이름=? WHERE id=?",
            (지금, 누가, d.이름, f"{d.이름} ⌫{지금}", did))
        self._conn.commit()
        return d.이름

    @atomic
    def restore(self, did: int) -> str:
        """휴지통에서 되살린다. 그사이 같은 이름이 생겼으면 «(되살림)» 을 붙인다."""
        d = self.get(did, 지운것도=True)
        if d is None or not d.지운일시:
            return ""
        이름 = d.지운이름 or d.이름
        if self.by_name(이름):
            이름 = f"{이름} (되살림)"
            n = 2
            while self.by_name(이름):
                이름 = f"{d.지운이름} (되살림 {n})"
                n += 1
        self._conn.execute(
            "UPDATE dashboards SET 지운일시='', 지운이='', 지운이름='', 이름=? WHERE id=?",
            (이름, did))
        self._conn.commit()
        return 이름

    @atomic
    def purge(self, did: int) -> str:
        """휴지통에서 **완전히** 지운다. 되돌릴 수 없다."""
        d = self.get(did, 지운것도=True)
        if d is None or not d.지운일시:
            return ""
        self._conn.execute("DELETE FROM blocks WHERE dashboard_id=?", (did,))
        self._conn.execute("DELETE FROM dashboards WHERE id=?", (did,))
        self._conn.commit()
        return d.지운이름 or d.이름

    def copy(self, did: int, 새이름: str, 만든이: str = "") -> int:
        """블록까지 통째로 복제한다. 비슷한 대시보드를 여럿 만들 때 쓴다."""
        새id = self.add(새이름, 만든이=만든이, 설명=(self.get(did).설명 if self.get(did) else ""))
        for b in self.blocks(did):
            self.add_block(새id, b.종류, 제목=b.제목, 설정=b.설정)
        return 새id

    @atomic
    def copy_block(self, bid: int) -> int:
        """블록 하나를 **바로 아래에** 복제한다. 0 이면 없는 블록이다.

        비슷한 표를 둘 만들 때 쓴다. 대시보드 통째 복제(`copy`)만 있어서, 표
        하나를 닮은 것으로 하나 더 만들려면 열·수식·너비를 처음부터 다시 적어야
        했다 — 시트 블록이면 칸 백 개를 다시 꾸미는 일이다.

        **맨 끝이 아니라 바로 아래다.** `add_block` 은 끝에 붙이는데, 블록이
        열 개쯤 되면 복제본을 찾아 ↑ 를 아홉 번 눌러야 한다.

        설정은 JSON 으로 오가므로(`add_block` 이 `json.dumps`, `_row` 가
        `json.loads`) 두 블록이 같은 dict 를 나눠 쓰는 일은 없다 — 한쪽 칸을
        고쳐도 다른 쪽은 그대로다.
        """
        b = self.block(bid)
        if b is None:
            return 0
        # 제목 뒤에 «복제» 를 붙인다. 똑같은 제목이 나란히 두 개면 어느 쪽을
        # 고치는 중인지 알 수 없다.
        새id = self.add_block(b.dashboard_id, b.종류,
                             제목=f"{b.제목 or b.종류} 복제", 설정=b.설정)
        # 끝에 붙은 것을 원본 바로 뒤로 끌어온다. `move_block` 과 같은 방식으로
        # 순서를 1 부터 다시 매겨 틈이 생기지 않게 한다.
        형제 = self.blocks(b.dashboard_id)
        줄 = [x.id for x in 형제 if x.id != 새id]
        줄.insert(줄.index(bid) + 1, 새id)
        for i, x in enumerate(줄, start=1):
            self._conn.execute("UPDATE blocks SET 순서=? WHERE id=?", (i, x))
        self._touch(b.dashboard_id)
        self._conn.commit()
        return 새id

    # -- 블록 ---------------------------------------------------------------
    def _touch(self, did: int) -> None:
        self._conn.execute(
            "UPDATE dashboards SET 수정일시=? WHERE id=?",
            (now_kst().strftime("%Y-%m-%d %H:%M:%S"), did),
        )

    @atomic
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
            "SELECT * FROM blocks WHERE dashboard_id=? AND 지운일시=''"
            " ORDER BY 순서, id",
            (dashboard_id,),
        )]

    def deleted_blocks(self, dashboard_id: int) -> list[tuple[Block, str]]:
        """지운 블록과 지운 때. 최근에 지운 것부터."""
        return [(self._row(r), r["지운일시"]) for r in self._conn.execute(
            "SELECT * FROM blocks WHERE dashboard_id=? AND 지운일시<>''"
            " ORDER BY 지운일시 DESC", (dashboard_id,))]

    def block(self, bid: int, *, 지운것도: bool = False) -> Block | None:
        row = self._conn.execute(
            "SELECT * FROM blocks WHERE id=?" + ("" if 지운것도 else " AND 지운일시=''"),
            (bid,)).fetchone()
        return self._row(row) if row else None

    def restore_block(self, bid: int) -> int:
        """지운 블록을 되살린다 (맨 아래로). 대시보드 id, 없으면 0."""
        b = self.block(bid, 지운것도=True)
        if b is None:
            return 0
        순서 = self._conn.execute(
            "SELECT COALESCE(MAX(순서), 0) + 1 AS n FROM blocks"
            " WHERE dashboard_id=? AND 지운일시=''", (b.dashboard_id,)).fetchone()["n"]
        self._conn.execute("UPDATE blocks SET 지운일시='', 순서=? WHERE id=?", (순서, bid))
        self._touch(b.dashboard_id)
        self._conn.commit()
        return b.dashboard_id

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
        # 휴지통으로. 편집 화면 아래 «지운 블록» 에서 되살린다.
        self._conn.execute("UPDATE blocks SET 지운일시=? WHERE id=?",
                           (now_kst().strftime("%Y-%m-%d %H:%M:%S"), bid))
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
    #: 조건에 맞은 사람 수 (최대로 자르기 전)
    전체: int = 0
    #: 사람마다의 지원자_ID (`사람` 과 같은 순서)
    ids: list[str] = field(default_factory=list)


@dataclass
class RenderedList:
    제목: str
    머리: list[str] = field(default_factory=list)
    폭: list[str] = field(default_factory=list)      # 열마다 px, 빈 값은 알아서
    행: list[list[str]] = field(default_factory=list)
    오류: list[str] = field(default_factory=list)
    전체: int = 0                      # 조건에 맞는 사람 수 (줄여 보여줄 때)
    #: 조건서식 결과. 줄마다 하나, 칸마다 하나. 빈 문자열이면 안 칠한다.
    행색: list[str] = field(default_factory=list)
    칸색: list[list[str]] = field(default_factory=list)


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
    열들 = [(머리, 식, 폭) for 머리, 식, 폭 in b.목록열 if str(식).strip()]
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

    # 조건서식 — 값에 따라 칠하기. 규칙을 미리 뜯어 두고 줄마다 견줘 본다.
    규칙 = []
    for r in b.조건서식:
        스타일 = _색스타일(r.get("배경"), r.get("글자"))
        if not 스타일:
            continue                    # 색을 안 고른 규칙은 아무 일도 안 한다
        규칙.append((r["조건"], r.get("대상") or ROW_TARGET, 스타일))
    머리이름 = [머리 or 식 for 머리, 식, _폭 in 열들]

    표행, 행색, 칸색 = [], [], []
    본오류 = set()
    # 줄 번호는 **여기서** 매긴다 — 거르고 정렬하고 자른 뒤라 화면에 보이는
    # 차례와 늘 같다. 조건서식과 칸 수식이 같은 루프를 도므로 `=ROW()` 를
    # 색칠 규칙에서도 쓸 수 있다.
    for 번호, (_cid, 값들) in enumerate(골라낸, start=1):
        값들[expr.줄번호_키] = 번호
        # 위에서부터 보다가 처음 맞는 것을 쓴다 (엑셀도 규칙에 순서가 있다).
        줄스타일 = ""
        칸스타일 = [""] * len(열들)
        for 조건, 대상, 스타일 in 규칙:
            보임, 잘못 = expr.render(조건, 값들)
            if 잘못:
                본오류.add(f"색칠 조건 '{조건}' → {잘못}")
                continue
            if str(보임).strip().upper() in ("", "FALSE", "0"):
                continue
            if 대상 == ROW_TARGET:
                if not 줄스타일:
                    줄스타일 = 스타일
            elif 대상 in 머리이름:
                i = 머리이름.index(대상)
                if not 칸스타일[i]:
                    칸스타일[i] = 스타일
        행색.append(줄스타일)
        칸색.append(칸스타일)

        칸들 = []
        for 열번호, (머리, 식, _폭) in enumerate(열들, start=1):
            if not expr.is_formula(식):
                칸들.append(식)                  # 그냥 글자는 그대로
                continue
            보임, 잘못 = expr.render(식, {**값들, expr.열번호_키: 열번호})
            if 잘못:
                칸들.append("?")
                본오류.add(f"'{머리 or 식}' → {잘못}")
            else:
                칸들.append(보임)
        표행.append(칸들)

    오류 += sorted(본오류)
    if 아는열 is not None:
        쓴열: set[str] = set()
        for 식 in [식 for _머리, 식, _폭 in 열들] + [b.목록조건, b.목록정렬]:
            if not expr.is_formula(식):
                continue
            try:
                쓴열 |= set(expr.columns(식))
            except expr.ExprError:
                pass                             # 문법 오류는 저장할 때 걸린다
        모르는 = sorted(c for c in 쓴열 if c not in 아는열)
        if 모르는:
            오류.append("표에 없는 열입니다: " + ", ".join(모르는))

    폭들 = [폭 for _머리, _식, 폭 in 열들]
    # 없는 열을 가리키는 규칙은 조용히 아무 일도 안 하므로 알려 준다.
    for r in b.조건서식:
        대상 = r.get("대상") or ROW_TARGET
        if 대상 != ROW_TARGET and 대상 not in 머리이름:
            오류.append(f"색칠 규칙이 가리키는 열이 없습니다: {대상}")
    return RenderedList(제목=b.제목, 머리=머리이름, 폭=폭들, 행=표행,
                        오류=오류, 전체=전체, 행색=행색, 칸색=칸색)


def _색스타일(배경: str, 글자: str) -> str:
    """고른 색을 인라인 스타일로. 색처럼 안 생긴 값은 버린다.

    사용자가 고른 값이 그대로 style 속성에 들어가므로, **모양을 확인한 것만**
    내보낸다 (`#rrggbb` 만). 안 그러면 스타일 속성을 통해 아무거나 넣을 수 있다.
    """
    좋은것 = []
    for 이름, 값 in (("background", 배경), ("color", 글자)):
        값 = str(값 or "").strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", 값):
            좋은것.append(f"{이름}:{값}")
    return ";".join(좋은것)


def render_table(b: Block, rows, 축값: dict[str, list[str]],
                 아는열: set[str] | None = None) -> RenderedTable:
    """자유 표 · 축 표를 계산한다.

    수식이 아닌 칸(그냥 글자)은 그대로 나간다. 틀린 수식은 칸에 `?` 를 두고
    무엇이 틀렸는지 위에 모아 적는다 — 조용히 0 을 띄우면 안 된다.
    """
    from . import expr as E
    from . import formula as F
    from .sheet import SheetError, 계산

    형식 = b.설정.get("형식") or "그대로"
    오류: list[str] = []

    def 한칸(수식: str) -> str:
        수식 = (수식 or "").strip()
        if not 수식:
            return ""
        if not E.is_formula(수식):
            return 수식
        try:
            # 시트와 **같은 계산기**다. 예전에는 여기만 `F.run` 이라
            # `=COUNT(지원자)/2` 가 안 됐다.
            글, 값 = 계산(수식, rows, 아는열)
        except (F.FormulaError, E.ExprError, SheetError, ValueError) as exc:
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

    from . import expr

    사람, 누구 = [], []
    ids = ids if isinstance(ids, list) else []
    최대 = b.프로필최대
    for 번호, cid in enumerate(ids[:최대] if 최대 else ids, start=1):
        값들 = 값찾기(cid)
        if not 값들:
            continue
        값들 = {**값들, expr.줄번호_키: 번호}   # 몇 번째 사람인가 (`=ROW()`)
        머리 = P.render(b.머리틀, 값들) if b.머리틀 else ""
        줄들 = P.render_rows(b.줄틀, 값들)
        if 머리 or 줄들:
            사람.append((머리 or cid, 줄들))
            누구.append(cid)
    if 아는열 is not None:
        쓴열 = {c for _라벨, 틀 in b.줄틀 for c in P.columns(틀)}
        쓴열 |= set(P.columns(b.머리틀))
        모르는 = sorted(c for c in 쓴열 if c not in 아는열)
        if 모르는:
            오류.append("표에 없는 열입니다: " + ", ".join(모르는))
    return RenderedProfile(제목=b.제목, 사람=사람, 오류=오류, 전체=len(ids), ids=누구)


# ---------------------------------------------------------------------------
# 시트
# ---------------------------------------------------------------------------
_보일범위_RE = re.compile(r"\$?([A-Za-z]{1,2})\$?(\d{1,4})(?::\$?([A-Za-z]{1,2})\$?(\d{1,4}))?")


def 보일범위정리(글) -> str:
    """`a1:f12` → `A1:F12`. 모양이 틀리면 빈칸 (= 전체)."""
    m = _보일범위_RE.fullmatch(str(글 or "").strip().replace(" ", ""))
    if not m:
        return ""
    가 = f"{m.group(1).upper()}{int(m.group(2))}"
    나 = f"{m.group(3).upper()}{int(m.group(4))}" if m.group(3) else 가
    return 가 if 가 == 나 else f"{가}:{나}"


def 보일범위풀기(글, 행수: int, 열수: int) -> tuple[int, int, int, int] | None:
    from .sheet import 자리

    정리 = 보일범위정리(글)
    if not 정리:
        return None
    가, _, 나 = 정리.partition(":")
    (r0, c0), (r1, c1) = 자리(가), 자리(나 or 가)
    r0, r1 = sorted((r0, r1))
    c0, c1 = sorted((c0, c1))
    r1, c1 = min(r1, 행수 - 1), min(c1, 열수 - 1)
    if r0 > r1 or c0 > c1:
        return None                       # 격자 밖 — 전체를 보인다
    return r0, c0, r1, c1


def 시트_다듬기(들어온것: dict) -> dict:
    """브라우저가 보낸 시트 JSON 을 **믿지 않고** 걸러 받는다.

    값이 그대로 style 속성과 격자 크기가 되므로, 모양을 확인한 것만 남긴다.
    어긋난 항목은 그것만 버리고 나머지는 받는다 — 색 하나 잘못 왔다고 그 사람이
    한참 꾸며 둔 시트를 통째로 되돌리면 안 된다.
    """
    from .sheet import 자리

    행수 = _사이((들어온것 or {}).get("행수"), 1, SHEET_MAX_ROWS, 10)
    열수 = _사이((들어온것 or {}).get("열수"), 1, SHEET_MAX_COLS, 6)

    칸들: dict[str, dict] = {}
    for 주소글, 값 in ((들어온것 or {}).get("칸") or {}).items():
        if not isinstance(값, dict):
            continue
        try:
            r, c = 자리(str(주소글))
        except ValueError:
            continue
        if not (0 <= r < 행수 and 0 <= c < 열수):
            continue                      # 격자 밖의 칸은 버린다
        남길것: dict = {}
        글 = str(값.get("글") or "")
        if 글:
            남길것["글"] = 글
        for 이름 in ("배경", "글자"):
            색 = str(값.get(이름) or "").strip()
            if re.fullmatch(r"#[0-9a-fA-F]{6}", 색):
                남길것[이름] = 색
        for 이름 in ("굵게", "기울임", "밑줄"):
            if 값.get(이름):
                남길것[이름] = 1
        # 0 이나 빈 값은 «안 정함» 이다. 그대로 `_사이` 에 넣으면 8 로 올려
        # 붙어서, 크기를 안 고른 칸이 전부 8px 글씨가 된다.
        if 값.get("크기"):
            남길것["크기"] = _사이(값.get("크기"), SHEET_MIN_SIZE, SHEET_MAX_SIZE,
                              SHEET_MIN_SIZE)
        if str(값.get("글꼴") or "") in SHEET_FONTS[1:]:
            남길것["글꼴"] = str(값.get("글꼴"))
        if str(값.get("정렬") or "") in SHEET_ALIGNS:
            남길것["정렬"] = str(값.get("정렬"))
        # 테두리 — 변마다 정해진 모양만. 색은 #rrggbb 만 (style 속성에 들어간다).
        테 = 값.get("테두리")
        if isinstance(테, dict):
            변들 = {변: str(테.get(변)) for 변 in SHEET_BORDER_SIDES
                  if str(테.get(변) or "") in SHEET_BORDER_STYLES}
            if 변들:
                남길것["테두리"] = 변들
                색 = str(값.get("테두리색") or "").strip()
                if re.fullmatch(r"#[0-9a-fA-F]{6}", 색):
                    남길것["테두리색"] = 색
        # 병합은 격자를 벗어나지 않게 자른다. 1 은 '안 합침' 이라 안 담는다.
        가로 = _사이(값.get("가로병합"), 1, 열수 - c, 1)
        세로 = _사이(값.get("세로병합"), 1, 행수 - r, 1)
        if 가로 > 1:
            남길것["가로병합"] = 가로
        if 세로 > 1:
            남길것["세로병합"] = 세로
        if 남길것:
            칸들[f"{주소글}".upper()] = 남길것

    # 병합에 **덮인 자리**의 글은 보이지 않는다 (그려지는 칸은 왼쪽 위뿐이다).
    # 예전 편집기는 덮인 자리에도 적을 수 있어서, A1·A2 를 합친 뒤 A2 에 적은
    # 글이 안 보인 채 남아 있을 수 있다. 주인 칸이 비어 있으면 그리로 옮기고,
    # 아니면 버린다 — 보이지도 고치지도 못하는 글을 남겨 둘 까닭이 없다.
    from .sheet import 덮인칸 as _덮인칸
    for 덮인, 주인 in _덮인칸(칸들, 행수, 열수).items():
        남은 = 칸들.get(덮인)
        if not 남은 or not 남은.get("글"):
            continue
        if not (칸들.get(주인) or {}).get("글"):
            칸들.setdefault(주인, {})["글"] = 남은["글"]
        del 남은["글"]
        if not 남은:
            del 칸들[덮인]

    def 크기묶음(무엇: str, 열쇠확인) -> dict[str, str]:
        나온것 = {}
        for k, v in ((들어온것 or {}).get(무엇) or {}).items():
            if not 열쇠확인(str(k)):
                continue
            px = _사이(v, 20, 2000, 0)
            if px:
                나온것[str(k).upper()] = str(px)
        return 나온것

    return {
        "행수": 행수,
        "열수": 열수,
        "격자숨김": bool((들어온것 or {}).get("격자숨김")),
        "보일범위": 보일범위정리((들어온것 or {}).get("보일범위")),
        "시트칸": 칸들,
        "시트열너비": 크기묶음("열너비", lambda k: re.fullmatch(r"[A-Za-z]{1,2}", k)),
        "시트행높이": 크기묶음("행높이", lambda k: k.isdigit()),
    }


def 시트_채우기(들어온것: dict, 원본: str, 대상: str) -> dict:
    """원본 네모를 대상 네모에 **깔아** 놓는다 (자동 채우기·붙여넣기).

    엑셀의 자동 채우기와 복사·붙여넣기는 같은 일이다 — 한 덩어리를 다른 자리에
    깔고, **수식의 칸 참조를 옮긴 만큼 민다.** 그래서 함수 하나로 둘 다 한다.

    대상이 원본보다 크면 원본을 되풀이해 깐다 (엑셀도 그렇게 한다). 대상이
    작으면 대상만큼만 깐다.

    **여기가 미는 자리다.** 브라우저에서 밀면 규칙이 두 벌이 되어 조용히
    갈라진다 — 화면에서 만든 수식과 서버가 계산하는 수식이 다르면 아무도
    못 알아챈다. 브라우저는 «어디를 어디에» 만 보내고 미는 일은 여기서 한다.

    글이 없는 칸(서식만 있는 칸)도 그대로 깔린다. 원본에 아예 없는 칸은
    대상에서 **지운다** — 엑셀도 빈 칸을 복사하면 빈 칸이 된다.
    """
    from .sheet import 자리, 주소, 옮기기

    행수 = _사이((들어온것 or {}).get("행수"), 1, SHEET_MAX_ROWS, 10)
    열수 = _사이((들어온것 or {}).get("열수"), 1, SHEET_MAX_COLS, 6)
    칸들 = dict((들어온것 or {}).get("칸") or {})
    try:
        r0, c0, r1, c1 = _네모(원본)
        R0, C0, R1, C1 = _네모(대상)
    except ValueError:
        return 들어온것                        # 주소가 아니면 아무것도 안 한다

    높이, 너비 = r1 - r0 + 1, c1 - c0 + 1
    새칸들 = dict(칸들)
    for R in range(R0, R1 + 1):
        for C in range(C0, C1 + 1):
            if not (0 <= R < 행수 and 0 <= C < 열수):
                continue
            # 되풀이해 깐다. 원본 자리 그대로면 옮길 것이 없다.
            src = 주소(r0 + (R - R0) % 높이, c0 + (C - C0) % 너비)
            dst = 주소(R, C)
            원 = 칸들.get(src)
            if 원 is None:
                새칸들.pop(dst, None)
                continue
            벤것 = dict(원)
            if 벤것.get("글"):
                r, c = 자리(src)
                벤것["글"] = 옮기기(벤것["글"], R - r, C - c,
                                 행수=행수, 열수=열수)
            # 병합은 안 따라간다. 깔린 자리마다 병합이 번지면 격자가 엉킨다.
            벤것.pop("가로병합", None)
            벤것.pop("세로병합", None)
            새칸들[dst] = 벤것
    return {**(들어온것 or {}), "칸": 새칸들}


def _네모(범위: str) -> tuple[int, int, int, int]:
    """`A1:C3` · `A1` -> (r0, c0, r1, c1). 주소가 아니면 ValueError."""
    from .sheet import 자리

    조각 = str(범위 or "").split(":")
    a = 자리(조각[0])
    b = 자리(조각[1]) if len(조각) > 1 else a
    return (min(a[0], b[0]), min(a[1], b[1]),
            max(a[0], b[0]), max(a[1], b[1]))


#: 한 변에 테두리가 둘 겹치면 더 센 것을 쓴다 (엑셀도 두꺼운 쪽이 보인다).
_선세기 = {"점선": 1, "얇게": 2, "굵게": 3, "이중": 4}


def 시트_테두리선(칸들: dict, 행수: int, 열수: int) -> tuple[dict, dict]:
    """칸마다 적힌 테두리를 **선(칸과 칸 사이 경계)** 으로 바꾼다. (가로선, 세로선)

        가로선[(r, c)] = r-1 행과 r 행 사이, c 열 자리의 선 → (모양, 색)
        세로선[(r, c)] = c-1 열과 c 열 사이, r 행 자리의 선

    테두리는 칸의 것이 아니라 **두 칸이 나눠 가진 경계**의 것이다. 칸마다 제
    테두리만 그리면, 표가 이웃한 두 칸의 선이 겹칠 때 **왼쪽·위쪽 칸의 선**을
    쓰기 때문에 오른쪽·아래 칸에 그은 위·왼쪽 테두리가 연한 격자선에 져서
    사라졌다. 선으로 모아 두고 양쪽 칸이 같은 선을 그리면 누가 이기든 같다.

    병합된 칸은 덮는 넓이 전체의 둘레에 선을 긋는다.
    """
    from .sheet import 자리

    가로: dict = {}
    세로: dict = {}

    def 긋기(판: dict, 열쇠, 모양: str, 색: str) -> None:
        옛 = 판.get(열쇠)
        if 옛 is None or _선세기.get(모양, 0) >= _선세기.get(옛[0], 0):
            판[열쇠] = (모양, 색)

    for 주소글, 칸 in (칸들 or {}).items():
        테 = (칸 or {}).get("테두리") or {}
        if not isinstance(테, dict) or not 테:
            continue
        try:
            r0, c0 = 자리(주소글)
        except ValueError:
            continue
        h = max(1, int(칸.get("세로병합", 1) or 1))
        w = max(1, int(칸.get("가로병합", 1) or 1))
        색 = 칸.get("테두리색") or "#222222"
        for 변, 모양 in 테.items():
            if 모양 not in _선CSS:
                continue
            if 변 == "위":
                for c in range(c0, c0 + w):
                    긋기(가로, (r0, c), 모양, 색)
            elif 변 == "아래":
                for c in range(c0, c0 + w):
                    긋기(가로, (r0 + h, c), 모양, 색)
            elif 변 == "왼쪽":
                for r in range(r0, r0 + h):
                    긋기(세로, (r, c0), 모양, 색)
            elif 변 == "오른쪽":
                for r in range(r0, r0 + h):
                    긋기(세로, (r, c0 + w), 모양, 색)
    return 가로, 세로


def 시트_칸테두리(가로: dict, 세로: dict, r: int, c: int, h: int = 1,
              w: int = 1) -> dict[str, tuple[str, str]]:
    """(r, c) 에서 h×w 를 덮는 칸이 그릴 네 변. 병합 칸의 한 변이 부분만 그어져
    있으면 가장 센 조각을 쓴다 (HTML 은 변을 쪼개 그릴 수 없다)."""
    def 센것(조각들):
        있는 = [x for x in 조각들 if x]
        return max(있는, key=lambda x: _선세기.get(x[0], 0)) if 있는 else None

    나온것 = {}
    for 변, 조각들 in (
        ("위", [가로.get((r, cc)) for cc in range(c, c + w)]),
        ("아래", [가로.get((r + h, cc)) for cc in range(c, c + w)]),
        ("왼쪽", [세로.get((rr, c)) for rr in range(r, r + h)]),
        ("오른쪽", [세로.get((rr, c + w)) for rr in range(r, r + h)]),
    ):
        x = 센것(조각들)
        if x:
            나온것[변] = x
    return 나온것


def 시트_칸스타일(칸: dict, 테두리: dict | None = None) -> str:
    """칸 하나의 인라인 스타일. **다듬기를 거친 값만** 들어온다고 본다.

    `테두리` 는 `시트_칸테두리` 가 선에서 모아 준 네 변 {변: (모양, 색)}.
    안 주면 칸에 적힌 것만 그린다 (칸 하나만 볼 때).
    """
    조각 = [_색스타일(칸.get("배경", ""), 칸.get("글자", ""))]
    if 칸.get("굵게"):
        조각.append("font-weight:700")
    if 칸.get("기울임"):
        조각.append("font-style:italic")
    if 칸.get("밑줄"):
        조각.append("text-decoration:underline")
    if 칸.get("크기"):
        조각.append(f"font-size:{int(칸['크기'])}px")
    글꼴 = 칸.get("글꼴")
    if 글꼴 in _글꼴스택:
        조각.append(f"font-family:{_글꼴스택[글꼴]}")
    if 칸.get("정렬"):
        조각.append(f"text-align:{칸['정렬']}")
    if 테두리 is None:
        테 = 칸.get("테두리") or {}
        색 = 칸.get("테두리색") or "#222222"
        테두리 = ({변: (모양, 색) for 변, 모양 in 테.items()}
               if isinstance(테, dict) else {})
    for 변 in SHEET_BORDER_SIDES:
        if 변 in 테두리:
            모양, 색 = 테두리[변]
            if 모양 in _선CSS:
                조각.append(f"border-{_변CSS[변]}:{_선CSS[모양]} {색}")
    return ";".join(x for x in 조각 if x)


def 시트_행열(들어온것: dict, 무엇: str, 위치: int, 개수: int = 1) -> dict:
    """행·열을 **그 자리에** 끼우거나 뺀다 (엑셀의 «삽입»·«삭제»).

    `무엇`: "행삽입" "행삭제" "열삽입" "열삭제". `위치` 는 0-based.
    칸·병합·너비·높이가 따라 옮겨지고, **수식의 칸 참조도 같이 밀린다**
    (`sheet.참조밀기`). 뺀 줄을 가리키던 참조는 `#참조!` 가 된다 — 조용히
    옆 칸을 가리키게 두면 아무도 틀린 줄 모른다.
    """
    from .sheet import 자리, 주소, 참조밀기

    행수 = _사이((들어온것 or {}).get("행수"), 1, SHEET_MAX_ROWS, 10)
    열수 = _사이((들어온것 or {}).get("열수"), 1, SHEET_MAX_COLS, 6)
    축 = "행" if 무엇.startswith("행") else "열"
    끼움 = 무엇.endswith("삽입")
    개수 = max(1, int(개수 or 1))
    한도 = 행수 if 축 == "행" else 열수
    위치 = max(0, min(int(위치), 한도 - (0 if 끼움 else 1)))
    if not 끼움:
        개수 = min(개수, 한도 - 위치)
        if 한도 - 개수 < 1:
            return 들어온것                   # 마지막 한 줄은 못 뺀다
    차 = 개수 if 끼움 else -개수
    새행수 = min(SHEET_MAX_ROWS, 행수 + 차) if 축 == "행" else 행수
    새열수 = min(SHEET_MAX_COLS, 열수 + 차) if 축 == "열" else 열수

    새칸: dict = {}
    for 주소글, 칸 in ((들어온것 or {}).get("칸") or {}).items():
        try:
            r, c = 자리(주소글)
        except ValueError:
            continue
        i = r if 축 == "행" else c
        if not 끼움 and 위치 <= i < 위치 + 개수:
            continue                          # 뺀 줄의 칸
        if i >= 위치:
            i += 차
        r, c = (i, c) if 축 == "행" else (r, i)
        if not (0 <= r < 새행수 and 0 <= c < 새열수):
            continue
        칸 = dict(칸 or {})
        if 칸.get("글"):
            칸["글"] = 참조밀기(칸["글"], 축, 위치, 차, 행수=새행수, 열수=새열수)
        # 병합이 끼운·뺀 자리를 가로지르면 그만큼 늘이고 줄인다 (엑셀과 같다).
        병합키 = "세로병합" if 축 == "행" else "가로병합"
        원자리 = (자리(주소글)[0] if 축 == "행" else 자리(주소글)[1])
        폭 = int(칸.get(병합키, 1) or 1)
        if 폭 > 1:
            if 끼움 and 원자리 < 위치 < 원자리 + 폭:
                폭 += 개수
            elif not 끼움:
                겹침 = max(0, min(원자리 + 폭, 위치 + 개수) - max(원자리, 위치))
                폭 -= 겹침
            if 폭 > 1:
                칸[병합키] = 폭
            else:
                칸.pop(병합키, None)
        새칸[주소(r, c)] = 칸

    def 크기옮기기(묶음: dict, 열쇠가행: bool) -> dict:
        from .xlsx_read import col_index
        from .export import col_letter
        나온것 = {}
        for k, v in (묶음 or {}).items():
            i = int(k) - 1 if 열쇠가행 else col_index(str(k).upper())
            if not 끼움 and 위치 <= i < 위치 + 개수:
                continue
            if i >= 위치:
                i += 차
            if i < 0:
                continue
            나온것[str(i + 1) if 열쇠가행 else col_letter(i)] = v
        return 나온것

    나온것 = {**(들어온것 or {}), "행수": 새행수, "열수": 새열수, "칸": 새칸}
    범위 = 보일범위풀기((들어온것 or {}).get("보일범위"), 행수, 열수)
    if 범위 is not None:
        # 보일 범위도 끼운·뺀 만큼 따라 늘고 준다 (엑셀의 인쇄 영역처럼).
        r0, c0, r1, c1 = 범위
        가, 나 = (r0, r1) if 축 == "행" else (c0, c1)
        if 끼움:
            가 += 개수 if 가 >= 위치 else 0
            나 += 개수 if 나 >= 위치 else 0
        else:
            가 = 가 - 개수 if 가 >= 위치 + 개수 else min(가, 위치)
            나 = 나 - 개수 if 나 >= 위치 + 개수 else min(나, 위치 - 1)
        if 가 > 나:
            나온것["보일범위"] = ""
        else:
            r0, r1, c0, c1 = (가, 나, c0, c1) if 축 == "행" else (r0, r1, 가, 나)
            나온것["보일범위"] = f"{주소(r0, c0)}:{주소(r1, c1)}"
    if 축 == "행":
        나온것["행높이"] = 크기옮기기((들어온것 or {}).get("행높이"), True)
    else:
        나온것["열너비"] = 크기옮기기((들어온것 or {}).get("열너비"), False)
    return 나온것


#: 폐쇄망이라 웹폰트를 못 받는다. 깔려 있을 만한 것으로만 고른다.
_글꼴스택 = {
    "고딕": "'맑은 고딕','Malgun Gothic',sans-serif",
    "명조": "'바탕','Batang',serif",
    "고정폭": "'D2Coding','Consolas',monospace",
}


@dataclass
class RenderedSheet:
    제목: str
    행수: int
    열수: int
    #: [[(주소, 보일 값, 스타일, 가로병합, 세로병합)]] — 덮인 칸은 아예 빠진다
    행: list
    열너비: dict
    행높이: dict
    오류: list
    #: {주소: 다듬어진 서식 dict}. 엑셀로 내보낼 때 쓴다 — 화면은 스타일 글자를
    #: 쓰지만 엑슬은 값이 하나하나 필요하다.
    칸서식: dict = field(default_factory=dict)
    #: 테두리 선 (`시트_테두리선`). 엑셀로 내보낼 때 병합에 덮인 칸까지 선을 준다.
    가로선: dict = field(default_factory=dict)
    세로선: dict = field(default_factory=dict)
    #: 보일 범위로 잘랐으면 그 왼쪽 위 자리 (머리글 A·B·1·2 를 원래 자리로 적는다).
    #: 잘라낸 결과의 주소·서식·선·너비는 **(0,0) 부터 다시 매겨져** 있다.
    시작행: int = 0
    시작열: int = 0


def render_sheet(b: Block, rows, 아는열: set[str] | None = None, *,
                 잘라보기: bool = False) -> RenderedSheet:
    """시트를 계산한다. 덮인 칸은 내보내지 않는다 (병합된 칸의 왼쪽 위만 그린다).

    **덮인 자리를 세는 일은 `sheet.덮인칸` 하나가 한다.** 계산하는 쪽도 그것을
    본다 — 두 벌로 두면 «그려지는 칸» 과 «값이 있는 칸» 이 조용히 갈라진다.

    `잘라보기` 면 **보일 범위**(`시트보일범위`)만 내보낸다 (보기 화면·엑셀).
    계산은 늘 전체로 한다 — 범위 밖 칸을 가리키는 수식도 맞게 나와야 한다.
    범위 경계에 걸친 병합은 범위 안쪽만큼 잘린다.
    """
    from .export import col_letter
    from .sheet import 값들, 덮인칸, 자리 as _자리, 주소

    칸들 = b.시트칸
    행수, 열수 = b.시트행수, b.시트열수
    계산값, 오류 = 값들(칸들, rows, 아는열, 행수=행수, 열수=열수)
    덮인 = set(덮인칸(칸들, 행수, 열수))
    가로선, 세로선 = 시트_테두리선(칸들, 행수, 열수)
    범위 = b.시트보일범위 if 잘라보기 else None
    r0, c0, r1, c1 = 범위 if 범위 is not None else (0, 0, 행수 - 1, 열수 - 1)

    놓을것: dict[tuple[int, int], tuple] = {}
    새서식: dict[str, dict] = {}
    for r in range(행수):
        for c in range(열수):
            주소글 = 주소(r, c)
            if 주소글 in 덮인:
                continue
            칸 = 칸들.get(주소글) or {}
            h, w = int(칸.get("세로병합", 1)), int(칸.get("가로병합", 1))
            # 이 칸(병합이면 그 넓이)과 보일 범위가 겹치는 네모
            ir0, ic0 = max(r, r0), max(c, c0)
            ir1, ic1 = min(r + h - 1, r1), min(c + w - 1, c1)
            if ir0 > ir1 or ic0 > ic1:
                continue
            ih, iw = ir1 - ir0 + 1, ic1 - ic0 + 1
            새주소 = 주소(ir0 - r0, ic0 - c0)
            놓을것[(ir0 - r0, ic0 - c0)] = (
                새주소, 계산값.get(주소글, ""),
                시트_칸스타일(칸, 시트_칸테두리(가로선, 세로선, ir0, ic0, ih, iw)), iw, ih)
            if 칸:
                새서식[새주소] = 칸
    행수2, 열수2 = r1 - r0 + 1, c1 - c0 + 1
    나온행 = [[놓을것[(r, c)] for c in range(열수2) if (r, c) in 놓을것]
             for r in range(행수2)]
    if 범위 is None:
        return RenderedSheet(제목=b.제목, 행수=행수, 열수=열수, 행=나온행,
                             열너비=b.시트열너비, 행높이=b.시트행높이, 오류=오류,
                             칸서식=칸들, 가로선=가로선, 세로선=세로선)
    열너비 = {}
    for k, v in b.시트열너비.items():
        c = _자리(f"{k}1")[1]
        if c0 <= c <= c1:
            열너비[col_letter(c - c0)] = v
    행높이 = {str(int(k) - r0): v for k, v in b.시트행높이.items()
            if str(k).isdigit() and r0 <= int(k) - 1 <= r1}
    return RenderedSheet(
        제목=b.제목, 행수=행수2, 열수=열수2, 행=나온행, 열너비=열너비, 행높이=행높이,
        오류=오류, 칸서식=새서식,
        가로선={(r - r0, c - c0): v for (r, c), v in 가로선.items()
               if r0 <= r <= r1 + 1 and c0 <= c <= c1},
        세로선={(r - r0, c - c0): v for (r, c), v in 세로선.items()
               if r0 <= r <= r1 and c0 <= c <= c1 + 1},
        시작행=r0, 시작열=c0)
