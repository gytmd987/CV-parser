"""엑셀(.xlsx) 출력 — 표준 라이브러리만 사용.

openpyxl 이 폐쇄망에 없을 수 있어 zipfile + XML 로 직접 쓴다.
xlsx 는 사실 XML 몇 개를 담은 zip 이라 이 정도는 어렵지 않다.

모든 셀을 inlineStr(문자열)로 쓰는 게 핵심이다. 그래야
  - 전화번호 01012345678 의 앞자리 0 이 살아남고
  - 202403 이 날짜로 자동 변환되지 않는다
"""

from __future__ import annotations

import io
import zipfile
from typing import Iterable, Sequence

from .schemas import COLUMNS, TEXT_COLUMNS, CVRecord, columns

_ESCAPE = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;"}


def _esc(text: str) -> str:
    out = []
    for ch in text:
        if ch in _ESCAPE:
            out.append(_ESCAPE[ch])
        elif ch in "\t\n\r" or ord(ch) >= 0x20:
            out.append(ch)
        # 그 외 제어문자는 xlsx 에서 파일 손상을 일으키므로 버린다
    return "".join(out)


def col_letter(idx: int) -> str:
    """0-based 열 번호 -> A, B, ... Z, AA, AB ..."""
    letters = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="지원자" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

# 0: 기본, 1: 헤더(굵게), 2: 텍스트 강제(@)
_STYLES = """<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="@"/></numFmts>
<fonts count="2"><font><sz val="11"/><name val="맑은 고딕"/></font>
<font><b/><sz val="11"/><name val="맑은 고딕"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFE8EEF7"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="3">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _sheet_xml(header: Sequence[str], rows: Sequence[dict[str, str]]) -> str:
    text_idx = {i for i, c in enumerate(header) if c in TEXT_COLUMNS}
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<cols>",
    ]
    for i, name in enumerate(header):
        width = 30 if name in ("1저자_해외논문_제출처", "경력_요약", "검토_사유") else 14
        parts.append(f'<col min="{i+1}" max="{i+1}" width="{width}" customWidth="1"/>')
    parts.append("</cols><sheetData>")

    # 헤더 행
    parts.append('<row r="1">')
    for i, name in enumerate(header):
        ref = f"{col_letter(i)}1"
        parts.append(f'<c r="{ref}" s="1" t="inlineStr"><is><t>{_esc(name)}</t></is></c>')
    parts.append("</row>")

    # 데이터 행
    for r, row in enumerate(rows, start=2):
        parts.append(f'<row r="{r}">')
        for i, name in enumerate(header):
            value = str(row.get(name, "") or "")
            if not value:
                continue
            style = ' s="2"' if i in text_idx else ""
            ref = f"{col_letter(i)}{r}"
            parts.append(
                f'<c r="{ref}"{style} t="inlineStr"><is><t xml:space="preserve">'
                f"{_esc(value)}</t></is></c>"
            )
        parts.append("</row>")

    parts.append("</sheetData></worksheet>")
    return "".join(parts)


def build_xlsx(rows: Sequence[dict[str, str]], header: Sequence[str] | None = None) -> bytes:
    """행 목록을 xlsx 바이트로 만든다."""
    cols = list(header or COLUMNS)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", _WORKBOOK)
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml", _sheet_xml(cols, rows))
    return buf.getvalue()


def records_to_xlsx(records: Iterable[CVRecord], registry=None) -> bytes:
    """registry 를 주면 대표명·등급 열이 반영된다."""
    cols = columns(registry)
    return build_xlsx([r.to_row(registry) for r in records], cols)


def records_to_tsv(records: Iterable[CVRecord], registry=None) -> str:
    """엑셀에 그대로 붙여넣을 수 있는 TSV. 셀 안 탭/줄바꿈은 공백으로 치환."""
    def clean(v: str) -> str:
        return v.replace("\t", " ").replace("\r", " ").replace("\n", " ")

    cols = columns(registry)
    lines = ["\t".join(cols)]
    for rec in records:
        row = rec.to_row(registry)
        lines.append("\t".join(clean(str(row.get(c, "") or "")) for c in cols))
    return "\n".join(lines)
