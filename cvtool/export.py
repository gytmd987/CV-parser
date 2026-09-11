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
<cellXfs count="4">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">
<alignment wrapText="1" vertical="top"/></xf>
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
            # 줄바꿈이 든 칸은 wrapText 를 걸어야 엑셀에서 줄이 보인다. 안 걸면
            # 값에는 있는데 화면에는 한 줄로 붙어 나온다. **값을 보고** 정하므로
            # 부르는 쪽이 어느 열인지 따로 알려줄 필요가 없다.
            style = (' s="3"' if "\n" in value
                     else ' s="2"' if i in text_idx else "")
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


def records_to_xlsx(records: Iterable[CVRecord], registry=None, custom=None,
                    열=None, 라벨=None) -> bytes:
    """registry 를 주면 대표명·등급 열이, custom 을 주면 사용자 정의 열이 붙는다.

    custom: {필드명 목록} 과 {지원자_ID: {필드: 값}} 을 담은 (이름들, 값맵) 튜플
    열:     내보낼 열과 순서 (안 주면 기본 순서 전부)
    라벨:   {열이름: 머리글} — 화면에서 이름을 바꾼 열은 엑셀에도 그 이름으로
    """
    이름들, 값맵 = custom or ([], {})
    cols = list(열) if 열 is not None else columns(registry) + list(이름들)
    rows = []
    for r in records:
        row = r.to_row(registry)
        row.update(값맵.get(r.지원자_ID, {}))
        rows.append(row)
    if not 라벨:
        return build_xlsx(rows, cols)
    # 머리글만 바꾼다 (값은 내부 열 이름으로 들고 있다)
    보일이름 = [라벨.get(c, c) for c in cols]
    바뀐행 = [{라벨.get(c, c): row.get(c, "") for c in cols} for row in rows]
    return build_xlsx(바뀐행, 보일이름)


def records_to_tsv(records: Iterable[CVRecord], registry=None, custom=None) -> str:
    """엑셀에 그대로 붙여넣을 수 있는 TSV. 셀 안 탭/줄바꿈은 공백으로 치환."""
    def clean(v: str) -> str:
        return v.replace("\t", " ").replace("\r", " ").replace("\n", " ")

    이름들, 값맵 = custom or ([], {})
    cols = columns(registry) + list(이름들)
    lines = ["\t".join(cols)]
    for rec in records:
        row = rec.to_row(registry)
        row.update(값맵.get(rec.지원자_ID, {}))
        lines.append("\t".join(clean(str(row.get(c, "") or "")) for c in cols))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 시트 블록 → 서식까지 살린 엑셀
# ---------------------------------------------------------------------------
# `build_xlsx` 는 **안 건드린다.** 지원자 표가 쓰는 길이라, 거기에 서식을 섞으면
# 멀쩡하던 것이 흔들린다. 시트는 쓰인 서식이 그때그때 다르므로 styles.xml 을
# 통째로 **만들어 낸다**.
_시트_글꼴 = {"고딕": "맑은 고딕", "명조": "바탕", "고정폭": "D2Coding"}


def _argb(색: str) -> str:
    """`#rrggbb` -> `FFrrggbb` (엑셀은 앞에 알파를 붙인다)."""
    return "FF" + (색 or "").lstrip("#").upper()


def _xml(글: str) -> str:
    return (str(글).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_sheet_xlsx(결과, 이름: str = "시트") -> bytes:
    """`dashboards.RenderedSheet` 를 색·굵기·병합까지 살려 엑셀로.

    쓰인 서식 조합만 모아 fonts/fills 를 만들고, 그 짝을 cellXfs 로 잇는다.
    안 쓴 서식까지 다 적으면 파일만 커지고 여는 데 오래 걸린다.
    """
    글꼴들: list[tuple] = [("", 11, False, False, False, "")]   # 0번은 기본
    채움들: list[str] = [""]                                     # 0번은 '안 칠함'
    모양들: list[tuple] = [(0, 0, "")]                           # (글꼴, 채움, 정렬)

    def 모양번호(칸: dict) -> int:
        글꼴 = (_시트_글꼴.get(칸.get("글꼴") or "", ""),
              int(칸.get("크기") or 11),
              bool(칸.get("굵게")), bool(칸.get("기울임")),
              bool(칸.get("밑줄")), 칸.get("글자") or "")
        if 글꼴 not in 글꼴들:
            글꼴들.append(글꼴)
        채움 = 칸.get("배경") or ""
        if 채움 not in 채움들:
            채움들.append(채움)
        모양 = (글꼴들.index(글꼴), 채움들.index(채움), 칸.get("정렬") or "")
        if 모양 not in 모양들:
            모양들.append(모양)
        return 모양들.index(모양)

    # 칸마다 모양 번호를 미리 매긴다 (styles.xml 을 먼저 만들어야 해서).
    번호 = {}
    for 줄 in 결과.행:
        for 주소글, _값, _스타일, _가로, _세로 in 줄:
            번호[주소글] = 모양번호(결과.칸서식.get(주소글) or {})

    def 글꼴XML(f) -> str:
        이름, 크기, 굵게, 기울임, 밑줄, 색 = f
        속 = f"<sz val=\"{크기}\"/><name val=\"{_xml(이름 or '맑은 고딕')}\"/>"
        if 굵게:
            속 = "<b/>" + 속
        if 기울임:
            속 = "<i/>" + 속
        if 밑줄:
            속 = "<u/>" + 속
        if 색:
            속 += f'<color rgb="{_argb(색)}"/>'
        return f"<font>{속}</font>"

    def 채움XML(색: str) -> str:
        if not 색:
            return '<fill><patternFill patternType="none"/></fill>'
        return (f'<fill><patternFill patternType="solid">'
                f'<fgColor rgb="{_argb(색)}"/><bgColor indexed="64"/>'
                f"</patternFill></fill>")

    def 모양XML(m) -> str:
        f, fl, 정렬 = m
        속성 = f'numFmtId="0" fontId="{f}" fillId="{fl}" borderId="1" xfId="0"'
        속성 += ' applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"'
        맞춤 = f' horizontal="{정렬}"' if 정렬 else ""
        return (f"<xf {속성}><alignment{맞춤} vertical=\"center\""
                " wrapText=\"1\"/></xf>")

    # gray125 는 엑셀이 1번 자리에 있기를 기대한다. 자리를 비워 두면 색이 밀린다.
    채움XML목록 = [채움XML(""), '<fill><patternFill patternType="gray125"/></fill>']
    채움자리 = {"": 0}
    for 색 in 채움들[1:]:
        채움자리[색] = len(채움XML목록)
        채움XML목록.append(채움XML(색))
    모양들 = [(f, 채움자리[채움들[fl]], a) for f, fl, a in 모양들]

    styles = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<fonts count="{len(글꼴들)}">' + "".join(글꼴XML(f) for f in 글꼴들) + "</fonts>"
        f'<fills count="{len(채움XML목록)}">' + "".join(채움XML목록) + "</fills>"
        '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
        '<border><left style="thin"><color rgb="FFD6DBE3"/></left>'
        '<right style="thin"><color rgb="FFD6DBE3"/></right>'
        '<top style="thin"><color rgb="FFD6DBE3"/></top>'
        '<bottom style="thin"><color rgb="FFD6DBE3"/></bottom><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        f'<cellXfs count="{len(모양들)}">' + "".join(모양XML(m) for m in 모양들) + "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )

    # -- 시트 본문 --
    칸XML: dict[int, list[str]] = {}
    병합: list[str] = []
    for 줄 in 결과.행:
        for 주소글, 값, _스타일, 가로, 세로 in 줄:
            r = int("".join(ch for ch in 주소글 if ch.isdigit()))
            칸XML.setdefault(r, []).append(
                f'<c r="{주소글}" s="{번호[주소글]}" t="inlineStr">'
                f"<is><t xml:space=\"preserve\">{_xml(값)}</t></is></c>"
            )
            if 가로 > 1 or 세로 > 1:
                글자 = "".join(ch for ch in 주소글 if ch.isalpha())
                끝 = f"{col_letter(col_index_local(글자) + 가로 - 1)}{r + 세로 - 1}"
                병합.append(f'<mergeCell ref="{주소글}:{끝}"/>')

    폭 = "".join(
        f'<col min="{col_index_local(글자) + 1}" max="{col_index_local(글자) + 1}"'
        f' width="{max(4, int(int(px) / 7))}" customWidth="1"/>'
        for 글자, px in sorted(결과.열너비.items())
    )
    본문 = "".join(
        f'<row r="{r}">' + "".join(칸들) + "</row>"
        for r, 칸들 in sorted(칸XML.items())
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + (f"<cols>{폭}</cols>" if 폭 else "")
        + f"<sheetData>{본문}</sheetData>"
        + (f'<mergeCells count="{len(병합)}">' + "".join(병합) + "</mergeCells>"
           if 병합 else "")
        + "</worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{_xml(이름[:31] or "시트")}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        z.writestr("xl/styles.xml", styles)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def col_index_local(letters: str) -> int:
    """A -> 0. `xlsx_read.col_index` 와 같은 일 — 가져오면 순환 import 가 된다."""
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1
