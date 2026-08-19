"""메일 템플릿과 발송 기록.

규칙 두 가지가 이 파일의 존재 이유다.

  1. **템플릿별로 지원자에게 한 번씩만** 보낸다. 같은 안내를 두 번 받으면
     지원자 입장에서 혼란스럽다.
  2. **탈락 메일을 보냈으면 그 지원자에게는 더 이상 아무것도 보내지 않는다.**
     떨어뜨려 놓고 면접 안내가 나가는 사고를 막는다.

두 규칙 모두 화면이 아니라 여기서 막는다. 화면에서 감추는 것만으로는 부족하다.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .fsutil import secure_dir, secure_file
from .timeutil import now_kst

#: 본문·제목에 쓰는 자리표시자. {{한글_이름}} 처럼 적는다.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    이름         TEXT NOT NULL UNIQUE,
    제목         TEXT NOT NULL DEFAULT '',
    본문         TEXT NOT NULL DEFAULT '',
    탈락메일      INTEGER DEFAULT 0,
    만든이        TEXT DEFAULT '',
    만든일시      TEXT DEFAULT '',
    수정일시      TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sent (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    지원자_ID    TEXT NOT NULL,
    template_id INTEGER NOT NULL,
    템플릿이름     TEXT DEFAULT '',
    받는사람      TEXT DEFAULT '',
    제목         TEXT DEFAULT '',
    본문         TEXT DEFAULT '',
    상태         TEXT DEFAULT '성공',   -- 성공 / 실패 / 발송안함(dry-run)
    탈락메일      INTEGER DEFAULT 0,
    오류         TEXT DEFAULT '',
    보낸이        TEXT DEFAULT '',
    보낸일시      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sent_cand ON sent (지원자_ID);
"""

#: 다시 보내도 되는 상태 (실패했으면 한 번 더 시도할 수 있어야 한다)
_보낸것 = ("성공", "발송안함")


@dataclass
class Template:
    id: int
    이름: str
    제목: str
    본문: str
    탈락메일: bool
    만든이: str
    만든일시: str
    수정일시: str

    def placeholders(self) -> list[str]:
        """제목·본문에 쓰인 자리표시자 이름들 (순서 유지, 중복 제거)."""
        본 = PLACEHOLDER_RE.findall(self.제목) + PLACEHOLDER_RE.findall(self.본문)
        return list(dict.fromkeys(x.strip() for x in 본 if x.strip()))


def render(text: str, 값들: dict[str, str]) -> tuple[str, list[str]]:
    """자리표시자를 채운다.

    Returns:
        (채운 글, 값이 비어 있던 자리표시자 목록)
    """
    빈것: list[str] = []

    def 바꾸기(m: re.Match) -> str:
        키 = m.group(1).strip()
        값 = str(값들.get(키, "") or "").strip()
        if not 값:
            빈것.append(키)
            return ""
        return 값

    return PLACEHOLDER_RE.sub(바꾸기, text or ""), list(dict.fromkeys(빈것))


class MailStore:
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

    # -- 템플릿 -------------------------------------------------------------
    def add_template(self, 이름: str, 제목: str = "", 본문: str = "",
                     탈락메일: bool = False, 만든이: str = "") -> int:
        이름 = (이름 or "").strip()
        if not 이름:
            raise ValueError("템플릿 이름을 입력하세요")
        if self.template_by_name(이름):
            raise ValueError(f"이미 있는 템플릿 이름입니다: {이름}")
        now = now_kst().strftime("%Y-%m-%d %H:%M:%S")
        cur = self._conn.execute(
            "INSERT INTO templates (이름,제목,본문,탈락메일,만든이,만든일시,수정일시)"
            " VALUES (?,?,?,?,?,?,?)",
            (이름, 제목 or "", 본문 or "", 1 if 탈락메일 else 0, 만든이, now, now),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_template(self, tid: int, *, 이름: str | None = None,
                        제목: str | None = None, 본문: str | None = None,
                        탈락메일: bool | None = None) -> Template | None:
        옛 = self.template(tid)
        if 옛 is None:
            return None
        새이름 = (이름 or 옛.이름).strip() or 옛.이름
        겹침 = self.template_by_name(새이름)
        if 겹침 and 겹침.id != tid:
            raise ValueError(f"이미 있는 템플릿 이름입니다: {새이름}")
        self._conn.execute(
            "UPDATE templates SET 이름=?, 제목=?, 본문=?, 탈락메일=?, 수정일시=? WHERE id=?",
            (
                새이름,
                옛.제목 if 제목 is None else 제목,
                옛.본문 if 본문 is None else 본문,
                (1 if 옛.탈락메일 else 0) if 탈락메일 is None else (1 if 탈락메일 else 0),
                now_kst().strftime("%Y-%m-%d %H:%M:%S"),
                tid,
            ),
        )
        self._conn.commit()
        return self.template(tid)

    def delete_template(self, tid: int) -> str:
        """템플릿만 지운다. **발송 기록은 남긴다** — 누구에게 뭘 보냈는지는 기록이다."""
        t = self.template(tid)
        if t is None:
            return ""
        self._conn.execute("DELETE FROM templates WHERE id=?", (tid,))
        self._conn.commit()
        return t.이름

    def _row(self, row: sqlite3.Row) -> Template:
        d = dict(row)
        d["탈락메일"] = bool(d["탈락메일"])
        return Template(**d)

    def template(self, tid: int) -> Template | None:
        row = self._conn.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
        return self._row(row) if row else None

    def template_by_name(self, 이름: str) -> Template | None:
        row = self._conn.execute(
            "SELECT * FROM templates WHERE 이름=?", ((이름 or "").strip(),)
        ).fetchone()
        return self._row(row) if row else None

    def templates(self) -> list[Template]:
        return [self._row(r) for r in
                self._conn.execute("SELECT * FROM templates ORDER BY 이름 COLLATE NOCASE")]

    # -- 발송 규칙 ----------------------------------------------------------
    def already_sent(self, 지원자_ID: str, template_id: int) -> bool:
        """이 템플릿을 이 지원자에게 이미 보냈나 (실패는 다시 보낼 수 있다)."""
        marks = ",".join("?" * len(_보낸것))
        row = self._conn.execute(
            f"SELECT 1 FROM sent WHERE 지원자_ID=? AND template_id=? AND 상태 IN ({marks})",
            (지원자_ID, template_id, *_보낸것),
        ).fetchone()
        return row is not None

    def rejected(self, 지원자_ID: str) -> bool:
        """탈락 메일을 이미 받았나. 받았으면 더 이상 아무것도 보내지 않는다."""
        marks = ",".join("?" * len(_보낸것))
        row = self._conn.execute(
            f"SELECT 1 FROM sent WHERE 지원자_ID=? AND 탈락메일=1 AND 상태 IN ({marks})",
            (지원자_ID, *_보낸것),
        ).fetchone()
        return row is not None

    def rejected_ids(self) -> set[str]:
        marks = ",".join("?" * len(_보낸것))
        return {
            r["지원자_ID"] for r in self._conn.execute(
                f"SELECT DISTINCT 지원자_ID FROM sent WHERE 탈락메일=1 AND 상태 IN ({marks})",
                _보낸것,
            )
        }

    def sent_ids(self, template_id: int) -> set[str]:
        marks = ",".join("?" * len(_보낸것))
        return {
            r["지원자_ID"] for r in self._conn.execute(
                f"SELECT DISTINCT 지원자_ID FROM sent"
                f" WHERE template_id=? AND 상태 IN ({marks})",
                (template_id, *_보낸것),
            )
        }

    def blocked_reason(self, 지원자_ID: str, tpl: Template) -> str:
        """보낼 수 없으면 이유, 보낼 수 있으면 빈 문자열."""
        if self.rejected(지원자_ID):
            return "탈락 메일을 이미 받은 지원자입니다"
        if self.already_sent(지원자_ID, tpl.id):
            return "이 템플릿을 이미 받았습니다"
        return ""

    # -- 발송 기록 ----------------------------------------------------------
    def record(self, 지원자_ID: str, tpl: Template, 받는사람: str, 제목: str,
               본문: str, 상태: str, 오류: str = "", 보낸이: str = "") -> int:
        cur = self._conn.execute(
            "INSERT INTO sent (지원자_ID,template_id,템플릿이름,받는사람,제목,본문,"
            " 상태,탈락메일,오류,보낸이,보낸일시) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (지원자_ID, tpl.id, tpl.이름, 받는사람, 제목, 본문, 상태,
             1 if tpl.탈락메일 else 0, 오류, 보낸이,
             now_kst().strftime("%Y-%m-%d %H:%M:%S")),
        )
        self._conn.commit()
        return cur.lastrowid

    def history(self, 지원자_ID: str = "", limit: int = 300) -> list[dict]:
        sql = "SELECT * FROM sent"
        args: tuple = ()
        if 지원자_ID:
            sql += " WHERE 지원자_ID=?"
            args = (지원자_ID,)
        sql += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self._conn.execute(sql, (*args, limit))]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) c FROM sent").fetchone()["c"]

    def close(self) -> None:
        self._conn.close()
