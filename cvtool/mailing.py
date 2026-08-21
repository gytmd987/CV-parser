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

from .fsutil import safe_filename, secure_dir, secure_file
from .timeutil import now_kst

#: 본문·제목에 쓰는 자리표시자. {{한글_이름}} 처럼 적는다.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

#: 첨부로 받을 확장자. 실행파일은 받지 않는다.
ATTACHMENT_SUFFIXES = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".hwp", ".hwpx",
    ".txt", ".md", ".csv", ".png", ".jpg", ".jpeg", ".gif", ".zip",
}

#: 첨부 한 개 최대 크기(바이트). 메일 서버가 대개 여기서 막힌다.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

#: 본문 그림으로 받을 형식과 최대 크기
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif"}
MAX_IMAGE_BYTES = 2 * 1024 * 1024

#: 본문 그림을 **메일에 어떻게 실을지.**
#:
#: 그림을 본문에 base64 로 박아 보내면, 받는 쪽 메일 프로그램이나 중간의 메일
#: API 가 그걸 어딘가에 올려두고 주소로 바꿔치는 일이 있다. 그 주소가 없어지면
#: **나중에 열었을 때 그림이 깨진다.** 우리가 어쩌지 못하는 영역이라, 파일을
#: 같이 보내 두는 선택지를 준다. 첨부는 메일 안에 남으므로 사라지지 않는다.
IMAGE_MODES = ("본문", "본문+첨부", "첨부만")
DEFAULT_IMAGE_MODE = "본문+첨부"

#: 본문 안 그림 참조. 우리 DB 에 있는 파일을 가리킨다.
BODY_IMAGE_RE = re.compile(r"/mail/image\?id=(\d+)")
#: 편집기가 예전에 남긴, 본문에 박힌 그림
_DATA_IMG_RE = re.compile(
    r"""<img\b[^>]*?\bsrc\s*=\s*(['"])data:image/(png|jpe?g|gif);base64,"""
    r"""([A-Za-z0-9+/=\s]+?)\1[^>]*>""",
    re.IGNORECASE,
)
#: 그림 태그 하나 (첨부만 모드에서 통째로 걷어낸다)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)

#: 이미 HTML 로 쓰인 본문인지 가늠하는 흔적
_LOOKS_HTML_RE = re.compile(
    r"</?(p|br|div|span|font|b|i|u|strong|em|table|tr|td|ul|ol|li|img|a)\b",
    re.IGNORECASE,
)

#: execCommand 가 남기는 옛 태그. 메일 클라이언트마다 해석이 달라 인라인 스타일로 바꾼다.
_FONT_SIZE_RE = re.compile(r"<font([^>]*?)\ssize=[\"\']?([1-7])[\"\']?([^>]*)>",
                           re.IGNORECASE)
#: <font size=N> 의 N 을 실제 크기로. 메일에서 눈에 보이는 값이어야 한다.
_FONT_SIZE_PT = {"1": "8pt", "2": "10pt", "3": "12pt", "4": "14pt",
                 "5": "18pt", "6": "24pt", "7": "32pt"}

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>|</tr>|</li>", re.IGNORECASE)
_PARA_RE = re.compile(r"</p>|</div>|</table>", re.IGNORECASE)


def html_to_text(html_본문: str) -> str:
    """꾸민 본문을 글자만 남긴 형태로. 미리보기·이력에 쓴다."""
    s = _PARA_RE.sub("\n\n", html_본문 or "")
    s = _BR_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    for 엔티티, 글자 in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                      ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        s = s.replace(엔티티, 글자)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def looks_like_html(본문: str) -> bool:
    return bool(_LOOKS_HTML_RE.search(본문 or ""))


def text_to_html(본문: str) -> str:
    """글자로 쓰인 본문을 HTML 로. 줄바꿈이 살아야 한다."""
    from html import escape

    return escape(본문 or "").replace("\n", "<br>")


def modernize(본문: str) -> str:
    """옛 <font size=N> 을 인라인 스타일로 바꾼다.

    메일 클라이언트는 <style> 블록을 지우는 경우가 많아 **인라인 스타일**이 가장
    안전하다. <font> 는 클라이언트마다 크기 해석이 달라 결과가 들쭉날쭉하다.
    """
    def 바꾸기(m: re.Match) -> str:
        앞, 크기, 뒤 = m.group(1) or "", m.group(2), m.group(3) or ""
        나머지 = (앞 + 뒤).strip()
        스타일 = f"font-size:{_FONT_SIZE_PT.get(크기, '12pt')}"
        return f"<font {나머지} style='{스타일}'>".replace("  ", " ")

    return _FONT_SIZE_RE.sub(바꾸기, 본문 or "")


def split_addresses(raw: str) -> list[str]:
    """쉼표·세미콜론·줄바꿈 아무거나로 구분해 적어도 되게."""
    조각 = re.split(r"[,;\n]+", raw or "")
    return [x.strip() for x in 조각 if x.strip()]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    이름         TEXT NOT NULL UNIQUE,
    제목         TEXT NOT NULL DEFAULT '',
    본문         TEXT NOT NULL DEFAULT '',
    탈락메일      INTEGER DEFAULT 0,
    참조         TEXT DEFAULT '',      -- CC. 이 템플릿으로 보내는 모든 메일에 붙는다
    본문형식      TEXT DEFAULT 'HTML',  -- HTML / TEXT
    만든이        TEXT DEFAULT '',
    만든일시      TEXT DEFAULT '',
    수정일시      TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS mail_attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL,
    파일명        TEXT NOT NULL,
    저장명        TEXT NOT NULL,
    크기         INTEGER DEFAULT 0,
    올린이        TEXT DEFAULT '',
    올린일시      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS attach_tpl ON mail_attachments (template_id);
CREATE TABLE IF NOT EXISTS sent (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    지원자_ID    TEXT NOT NULL,
    template_id INTEGER NOT NULL,
    템플릿이름     TEXT DEFAULT '',
    받는사람      TEXT DEFAULT '',
    제목         TEXT DEFAULT '',
    본문         TEXT DEFAULT '',
    상태         TEXT DEFAULT '성공',   -- 성공 / 실패 / 발송안함(dry-run)
    참조         TEXT DEFAULT '',
    첨부         TEXT DEFAULT '',
    탈락메일      INTEGER DEFAULT 0,
    오류         TEXT DEFAULT '',
    보낸이        TEXT DEFAULT '',
    보낸일시      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sent_cand ON sent (지원자_ID);
CREATE TABLE IF NOT EXISTS body_images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL,
    파일명        TEXT NOT NULL,
    저장명        TEXT NOT NULL,
    크기         INTEGER DEFAULT 0,
    올린이        TEXT DEFAULT '',
    올린일시      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS bodyimg_tpl ON body_images (template_id);
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
    참조: str
    본문형식: str
    만든이: str
    만든일시: str
    수정일시: str
    그림방식: str = DEFAULT_IMAGE_MODE

    @property
    def 그림보내기(self) -> str:
        return self.그림방식 if self.그림방식 in IMAGE_MODES else DEFAULT_IMAGE_MODE

    @property
    def html(self) -> bool:
        return (self.본문형식 or "HTML").upper() == "HTML"

    def cc(self) -> list[str]:
        return split_addresses(self.참조)

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
    def __init__(self, db_path: str | Path, files_dir: str | Path | None = None) -> None:
        self.path = Path(db_path)
        self.files_dir = Path(files_dir) if files_dir else self.path.parent / "mail_files"
        secure_dir(self.path.parent)
        secure_dir(self.files_dir)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._add_missing_columns()
        self._conn.commit()
        self.import_inline_images()
        for suffix in ("", "-wal", "-shm"):
            secure_file(Path(str(self.path) + suffix))

    def _add_missing_columns(self) -> None:
        """예전 DB 에 없던 열을 붙인다."""
        for 표, 열들 in (
            ("templates", (("참조", "''"), ("본문형식", "'HTML'"),
                           ("그림방식", f"'{DEFAULT_IMAGE_MODE}'"))),
            ("sent", (("참조", "''"), ("첨부", "''"))),
        ):
            있는열 = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({표})")}
            for 열, 기본 in 열들:
                if 열 not in 있는열:
                    self._conn.execute(
                        f"ALTER TABLE {표} ADD COLUMN {열} TEXT DEFAULT {기본}"
                    )
        self._upgrade_bodies_to_html()

    def _upgrade_bodies_to_html(self) -> None:
        """본문을 전부 HTML 로 맞춘다.

        편집기가 꾸미기 전용(HTML)인데 본문형식이 TEXT 로 남아 있으면, 애써 꾸민
        글이 **태그가 그대로 보이는 메일**로 나간다. 실제로 그 사고가 났다.

        이미 HTML 로 쓰인 본문은 그대로 두고, 순수한 글자만 줄바꿈을 살려 HTML 로
        바꾼다. 옛 <font size=N> 은 인라인 스타일로 옮긴다.
        """
        rows = self._conn.execute(
            "SELECT id, 본문, 본문형식 FROM templates"
        ).fetchall()
        for row in rows:
            본문 = row["본문"] or ""
            형식 = (row["본문형식"] or "").upper()
            새본문 = 본문 if (형식 == "HTML" or looks_like_html(본문)) else text_to_html(본문)
            새본문 = modernize(새본문)
            if 새본문 != 본문 or 형식 != "HTML":
                self._conn.execute(
                    "UPDATE templates SET 본문=?, 본문형식='HTML' WHERE id=?",
                    (새본문, row["id"]),
                )

    # -- 템플릿 -------------------------------------------------------------
    def add_template(self, 이름: str, 제목: str = "", 본문: str = "",
                     탈락메일: bool = False, 만든이: str = "",
                     참조: str = "", 본문형식: str = "HTML") -> int:
        이름 = (이름 or "").strip()
        if not 이름:
            raise ValueError("템플릿 이름을 입력하세요")
        if self.template_by_name(이름):
            raise ValueError(f"이미 있는 템플릿 이름입니다: {이름}")
        now = now_kst().strftime("%Y-%m-%d %H:%M:%S")
        cur = self._conn.execute(
            "INSERT INTO templates"
            " (이름,제목,본문,탈락메일,참조,본문형식,만든이,만든일시,수정일시)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (이름, 제목 or "", 본문 or "", 1 if 탈락메일 else 0, 참조 or "",
             (본문형식 or "HTML").upper(), 만든이, now, now),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_template(self, tid: int, *, 이름: str | None = None,
                        제목: str | None = None, 본문: str | None = None,
                        탈락메일: bool | None = None, 참조: str | None = None,
                        본문형식: str | None = None,
                        그림방식: str | None = None) -> Template | None:
        옛 = self.template(tid)
        if 옛 is None:
            return None
        새이름 = (이름 or 옛.이름).strip() or 옛.이름
        겹침 = self.template_by_name(새이름)
        if 겹침 and 겹침.id != tid:
            raise ValueError(f"이미 있는 템플릿 이름입니다: {새이름}")
        새그림방식 = 옛.그림보내기 if 그림방식 is None else 그림방식
        if 새그림방식 not in IMAGE_MODES:
            raise ValueError(f"그림 방식은 {'/'.join(IMAGE_MODES)} 중 하나여야 합니다")
        self._conn.execute(
            "UPDATE templates SET 이름=?, 제목=?, 본문=?, 탈락메일=?, 참조=?,"
            " 본문형식=?, 그림방식=?, 수정일시=? WHERE id=?",
            (
                새이름,
                옛.제목 if 제목 is None else 제목,
                옛.본문 if 본문 is None else 본문,
                (1 if 옛.탈락메일 else 0) if 탈락메일 is None else (1 if 탈락메일 else 0),
                옛.참조 if 참조 is None else 참조,
                (옛.본문형식 if 본문형식 is None else 본문형식).upper(),
                새그림방식,
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
        for att in self.attachments(tid):
            (self.files_dir / att["저장명"]).unlink(missing_ok=True)
        self._conn.execute("DELETE FROM mail_attachments WHERE template_id=?", (tid,))
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
               본문: str, 상태: str, 오류: str = "", 보낸이: str = "",
               참조: str = "", 첨부: str = "") -> int:
        cur = self._conn.execute(
            "INSERT INTO sent (지원자_ID,template_id,템플릿이름,받는사람,제목,본문,"
            " 상태,탈락메일,오류,보낸이,보낸일시,참조,첨부)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (지원자_ID, tpl.id, tpl.이름, 받는사람, 제목, 본문, 상태,
             1 if tpl.탈락메일 else 0, 오류, 보낸이,
             now_kst().strftime("%Y-%m-%d %H:%M:%S"), 참조, 첨부),
        )
        self._conn.commit()
        return cur.lastrowid

    # -- 첨부파일 (템플릿별) -------------------------------------------------
    def add_attachment(self, template_id: int, 파일명: str, content: bytes,
                       올린이: str = "") -> int:
        안전 = safe_filename(파일명)
        suffix = Path(안전).suffix.lower()
        if suffix not in ATTACHMENT_SUFFIXES:
            raise ValueError(
                f"받지 않는 형식입니다: {suffix or '(확장자 없음)'} "
                f"(허용: {', '.join(sorted(ATTACHMENT_SUFFIXES))})"
            )
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"파일이 너무 큽니다 ({len(content) // (1024 * 1024)}MB). "
                f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB 까지만 붙일 수 있습니다."
            )
        cur = self._conn.execute(
            "INSERT INTO mail_attachments (template_id,파일명,저장명,크기,올린이,올린일시)"
            " VALUES (?,?,?,?,?,?)",
            (template_id, 안전, "", len(content), 올린이,
             now_kst().strftime("%Y-%m-%d %H:%M:%S")),
        )
        att_id = cur.lastrowid
        저장명 = f"MT{template_id}-{att_id}{suffix}"
        dest = self.files_dir / 저장명
        dest.write_bytes(content)
        secure_file(dest)
        self._conn.execute(
            "UPDATE mail_attachments SET 저장명=? WHERE id=?", (저장명, att_id)
        )
        self._conn.commit()
        return att_id

    # -- 본문 그림 -----------------------------------------------------------
    def add_body_image(self, template_id: int, 파일명: str, content: bytes,
                       올린이: str = "") -> int:
        """본문에 넣을 그림을 **파일로** 보관하고 id 를 돌려준다.

        예전에는 편집기가 base64 를 본문에 그대로 박았다. 그러면 원본이
        본문 글자 안에만 있어서, 본문이 한 번 상해도 되돌릴 방법이 없다.
        파일로 두면 원본은 그대로 남고 본문에는 짧은 참조만 들어간다.
        """
        안전 = safe_filename(파일명) or "image.png"
        suffix = Path(안전).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            raise ValueError(
                f"본문 그림은 {', '.join(sorted(IMAGE_SUFFIXES))} 만 됩니다: "
                f"{suffix or '(확장자 없음)'}"
            )
        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError(
                f"그림이 너무 큽니다 ({len(content) // 1024}KB). "
                f"{MAX_IMAGE_BYTES // (1024 * 1024)}MB 까지입니다. "
                "큰 파일은 첨부로 붙이세요."
            )
        cur = self._conn.execute(
            "INSERT INTO body_images (template_id,파일명,저장명,크기,올린이,올린일시)"
            " VALUES (?,?,?,?,?,?)",
            (template_id, 안전, "", len(content), 올린이,
             now_kst().strftime("%Y-%m-%d %H:%M:%S")),
        )
        img_id = cur.lastrowid
        저장명 = f"MI{template_id}-{img_id}{suffix}"
        dest = self.files_dir / 저장명
        dest.write_bytes(content)
        secure_file(dest)
        self._conn.execute(
            "UPDATE body_images SET 저장명=? WHERE id=?", (저장명, img_id)
        )
        self._conn.commit()
        return img_id

    def body_image(self, img_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM body_images WHERE id=?", (img_id,)
        ).fetchone()
        return dict(row) if row else None

    def body_images(self, template_id: int) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM body_images WHERE template_id=? ORDER BY id",
            (template_id,),
        )]

    def body_image_bytes(self, img_id: int) -> bytes | None:
        img = self.body_image(img_id)
        if not img:
            return None
        path = self.files_dir / img["저장명"]
        return path.read_bytes() if path.is_file() else None

    def used_body_images(self, 본문: str) -> list[dict]:
        """본문이 실제로 쓰고 있는 그림 (쓴 순서, 중복 제거)."""
        out, 봤다 = [], set()
        for m in BODY_IMAGE_RE.finditer(본문 or ""):
            img_id = int(m.group(1))
            if img_id in 봤다:
                continue
            봤다.add(img_id)
            img = self.body_image(img_id)
            if img:
                out.append(img)
        return out

    def import_inline_images(self) -> int:
        """본문에 박혀 있던 base64 그림을 파일로 옮긴다 (한 번만).

        옛 템플릿을 새 방식으로 끌어올린다. 본문에는 짧은 참조가 남고 원본
        바이트는 파일로 보관된다. 실패하면 본문을 건드리지 않는다.
        """
        import base64
        import binascii

        옮긴것 = 0
        for row in self._conn.execute("SELECT id, 본문 FROM templates").fetchall():
            본문 = row["본문"] or ""
            if not _DATA_IMG_RE.search(본문):
                continue

            def 바꾸기(m: re.Match) -> str:
                nonlocal 옮긴것
                확장자 = {"png": ".png", "jpg": ".jpg", "jpeg": ".jpg",
                       "gif": ".gif"}[m.group(2).lower()]
                try:
                    내용 = base64.b64decode("".join(m.group(3).split()))
                except (binascii.Error, ValueError):
                    return m.group(0)          # 못 읽으면 그대로 둔다
                try:
                    img_id = self.add_body_image(
                        row["id"], f"본문그림{확장자}", 내용, 올린이="(옛 본문에서 옮김)"
                    )
                except ValueError:
                    return m.group(0)
                옮긴것 += 1
                return (f"<img src='/mail/image?id={img_id}' "
                        "style='max-width:100%'>")

            새본문 = _DATA_IMG_RE.sub(바꾸기, 본문)
            if 새본문 != 본문:
                self._conn.execute(
                    "UPDATE templates SET 본문=? WHERE id=?", (새본문, row["id"])
                )
        if 옮긴것:
            self._conn.commit()
        return 옮긴것

    def prepare_body(self, 본문: str, 방식: str) -> tuple[str, list[tuple[str, bytes]]]:
        """보내기 직전에 본문 그림을 실제로 실을 모양으로 바꾼다.

        Returns:
            (내보낼 본문, 함께 붙일 (파일명, 내용) 목록)

        - `본문`      : 예전과 같이 base64 로 박는다. 메일 API 나 받는 쪽이
                       그림을 어디로 옮기든 우리가 관여하지 않는다.
        - `본문+첨부` : 박아 넣고 **같은 파일을 첨부로도** 보낸다. 본문 그림이
                       나중에 깨져도 받은 사람 손에 파일은 남는다. (기본값)
        - `첨부만`    : 본문에서 그림을 빼고 첨부로만 보낸다. 본문이 가벼워지고
                       깨질 그림 자체가 없다.
        """
        import base64

        방식 = 방식 if 방식 in IMAGE_MODES else DEFAULT_IMAGE_MODE
        쓴그림 = self.used_body_images(본문)
        붙일것: list[tuple[str, bytes]] = []
        for img in 쓴그림:
            내용 = self.body_image_bytes(img["id"])
            if 내용 is None:
                continue
            if 방식 in ("본문+첨부", "첨부만"):
                붙일것.append((f"{img['id']}_{img['파일명']}", 내용))

        if 방식 == "첨부만":
            나온본문 = _IMG_TAG_RE.sub(
                lambda m: ("<p style='color:#666;font-size:10pt'>"
                           "[그림은 첨부파일을 봐 주세요]</p>")
                if BODY_IMAGE_RE.search(m.group(0)) else m.group(0),
                본문 or "",
            )
            return 나온본문, 붙일것

        # 본문에 박아 넣는다 — 참조를 실제 바이트로 되살린다
        내용맵 = {img["id"]: self.body_image_bytes(img["id"]) for img in 쓴그림}
        타입맵 = {".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".gif": "image/gif"}

        def 되살리기(m: re.Match) -> str:
            img_id = int(m.group(1))
            내용 = 내용맵.get(img_id)
            if 내용 is None:
                return m.group(0)
            img = self.body_image(img_id) or {}
            mime = 타입맵.get(Path(img.get("저장명") or "").suffix.lower(), "image/png")
            b64 = base64.b64encode(내용).decode("ascii")
            return f"data:{mime};base64,{b64}"

        return BODY_IMAGE_RE.sub(되살리기, 본문 or ""), 붙일것

    def delete_body_image(self, img_id: int) -> str:
        img = self.body_image(img_id)
        if not img:
            return ""
        (self.files_dir / img["저장명"]).unlink(missing_ok=True)
        self._conn.execute("DELETE FROM body_images WHERE id=?", (img_id,))
        self._conn.commit()
        return img["파일명"]

    def attachments(self, template_id: int) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM mail_attachments WHERE template_id=? ORDER BY id",
            (template_id,),
        )]

    def attachment(self, att_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM mail_attachments WHERE id=?", (att_id,)
        ).fetchone()
        return dict(row) if row else None

    def attachment_bytes(self, template_id: int) -> list[tuple[str, bytes]]:
        """발송에 실어 보낼 (파일명, 내용) 목록."""
        out = []
        for att in self.attachments(template_id):
            path = self.files_dir / att["저장명"]
            if path.is_file():
                out.append((att["파일명"], path.read_bytes()))
        return out

    def delete_attachment(self, att_id: int) -> str:
        att = self.attachment(att_id)
        if not att:
            return ""
        (self.files_dir / att["저장명"]).unlink(missing_ok=True)
        self._conn.execute("DELETE FROM mail_attachments WHERE id=?", (att_id,))
        self._conn.commit()
        return att["파일명"]

    def sent_summary(self) -> dict[str, str]:
        """{지원자_ID: "서류합격 안내(08-21) | 면접 안내(08-25)"}

        인재 Pool 표에 한 열로 낸다. 누구에게 무엇을 보냈는지 표에서 바로
        보이지 않으면, 같은 메일을 두 번 보내거나 안 보낸 사람을 놓친다.
        """
        보낸것: dict[str, list[str]] = {}
        for r in self._conn.execute(
            "SELECT 지원자_ID, 템플릿이름, 상태, 탈락메일, 보낸일시 FROM sent"
            " ORDER BY 보낸일시, id"
        ):
            이름 = r["템플릿이름"] or "(이름 없음)"
            날 = (r["보낸일시"] or "")[5:10]        # MM-DD
            꼬리 = "" if r["상태"] == "성공" else f"[{r['상태']}]"
            if r["탈락메일"]:
                꼬리 += "[탈락]"
            보낸것.setdefault(r["지원자_ID"], []).append(
                f"{이름}({날}){꼬리}" if 날 else f"{이름}{꼬리}"
            )
        return {cid: " | ".join(v) for cid, v in 보낸것.items()}

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
