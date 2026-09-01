"""엑셀(.xlsx) 읽기 — 표준 라이브러리만 사용.

`export.py` 의 반대쪽이다. openpyxl 이 폐쇄망에 없을 수 있어 zipfile + XML 로
직접 읽는다.

사람이 엑셀로 열어 저장한 파일은 우리가 쓴 것과 모양이 다르다. 여기서 꼭
넘겨야 하는 세 가지:

1. **공유 문자열**(`xl/sharedStrings.xml`). 우리가 쓸 때는 셀에 글자를 그대로
   박지만(`inlineStr`), 엑셀은 글자를 한 군데 모아 두고 셀에는 번호만 남긴다.
2. **빈 칸은 아예 없다.** `A1, B1, D1` 처럼 건너뛰고 저장하므로, 나오는 순서대로
   담으면 D 열 값이 C 자리에 들어간다. 셀 주소(`r="D1"`)를 풀어 제자리에 넣는다.
3. **글자가 조각나 있다.** 셀 안에서 서식이 바뀌면 `<t>` 가 여러 개로 쪼개진다.
   이어 붙여야 한 값이 된다.

모든 값을 **문자열 그대로** 돌려준다. 전화번호 앞자리 0 이나 202403 이 숫자로
바뀌면 안 되기 때문이다 — 쓰는 쪽이 같은 이유로 inlineStr 을 쓴다.
"""

from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from io import BytesIO

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_PKG_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CELL_RE = re.compile(r"^([A-Z]+)(\d+)$")

#: 지나치게 큰 파일은 읽지 않는다 (압축을 풀면 수백 배가 되는 zip 이 있다)
MAX_CELLS = 200_000


class XlsxError(ValueError):
    """엑셀 파일로 읽을 수 없다."""


def col_index(letters: str) -> int:
    """A -> 0, B -> 1, ... Z -> 25, AA -> 26 (`export.col_letter` 의 반대)."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _text(node: ET.Element) -> str:
    """`<si>` 나 `<is>` 안의 글자. 조각나 있으면 이어 붙인다."""
    return "".join(t.text or "" for t in node.iter(f"{_NS}t"))


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        raw = z.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    return [_text(si) for si in root.findall(f"{_NS}si")]


def _first_sheet_name(z: zipfile.ZipFile) -> str:
    """워크북이 가리키는 **첫 시트**의 파일 이름.

    `xl/worksheets/sheet1.xml` 이 늘 첫 시트인 것은 아니다 — 시트를 지웠다
    만들면 번호가 어긋난다. 워크북이 적어 둔 순서를 따른다.
    """
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    except KeyError as exc:
        raise XlsxError("엑셀 파일이 아닙니다 (xlsx 로 저장해 주세요).") from exc
    sheets = wb.find(f"{_NS}sheets")
    첫시트 = list(sheets or [])
    if not 첫시트:
        raise XlsxError("시트가 없는 엑셀 파일입니다.")
    rid = 첫시트[0].get(
        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    for r in rels.findall(f"{_PKG_NS}Relationship"):
        if r.get("Id") == rid:
            target = r.get("Target") or ""
            return target[1:] if target.startswith("/") else "xl/" + target.lstrip("./")
    return "xl/worksheets/sheet1.xml"


def read_sheet(data: bytes) -> list[list[str]]:
    """첫 시트를 문자열 표로. 빈 칸은 빈 문자열, 뒤쪽 빈 줄은 버린다."""
    try:
        z = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise XlsxError("엑셀 파일이 아닙니다 (xlsx 로 저장해 주세요).") from exc
    with z:
        공유 = _shared_strings(z)
        try:
            sheet = z.read(_first_sheet_name(z))
        except KeyError as exc:
            raise XlsxError("시트를 읽을 수 없습니다.") from exc

        표: list[list[str]] = []
        칸수 = 0
        for row in ET.fromstring(sheet).iter(f"{_NS}row"):
            줄: list[str] = []
            for n, c in enumerate(row.findall(f"{_NS}c")):
                m = _CELL_RE.match(c.get("r") or "")
                자리 = col_index(m.group(1)) if m else n
                while len(줄) < 자리:
                    줄.append("")
                값 = ""
                유형 = c.get("t") or ""
                if 유형 == "inlineStr":
                    안 = c.find(f"{_NS}is")
                    값 = _text(안) if 안 is not None else ""
                elif 유형 == "s":
                    v = c.find(f"{_NS}v")
                    자리번호 = int(v.text) if v is not None and (v.text or "").isdigit() else -1
                    값 = 공유[자리번호] if 0 <= 자리번호 < len(공유) else ""
                else:
                    v = c.find(f"{_NS}v")
                    값 = (v.text or "") if v is not None else ""
                줄.append(값)
                칸수 += 1
                if 칸수 > MAX_CELLS:
                    raise XlsxError(
                        f"칸이 너무 많습니다 ({MAX_CELLS:,}칸까지). 나눠서 올려 주세요.")
            표.append(줄)

    while 표 and not any(v.strip() for v in 표[-1]):
        표.pop()
    return 표
