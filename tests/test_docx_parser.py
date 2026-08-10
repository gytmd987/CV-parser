"""워드(docx) 파서 테스트.

한국 이력서는 표로 된 것이 아주 흔하다. python-docx 의 document.paragraphs 는
**표 안의 글자를 포함하지 않아서**, 문단만 읽으면 이름·연락처·학력이 통째로
사라진다. 실제로 그런 버그가 있었다.
"""

from __future__ import annotations

import pytest

from cvtool.ingestion.parsers import UnsupportedFormat, extract_text

docx = pytest.importorskip("docx", reason="python-docx 미설치 (폐쇄망 환경에서는 건너뜀)")


@pytest.fixture
def table_cv(tmp_path):
    """표로 된 전형적인 한국식 이력서."""
    d = docx.Document()
    d.add_heading("이력서", 0)
    t = d.add_table(rows=4, cols=2)
    for (k, v), row in zip(
        [
            ("성명", "홍길동 (Gildong Hong)"),
            ("생년월일", "1992.03.15"),
            ("연락처", "010-1234-5678"),
            ("이메일", "hong@example.com"),
        ],
        t.rows,
    ):
        row.cells[0].text = k
        row.cells[1].text = v
    d.add_paragraph("[학력]")
    t2 = d.add_table(rows=1, cols=3)
    t2.rows[0].cells[0].text = "2019.03-2025.02"
    t2.rows[0].cells[1].text = "서울대학교 컴퓨터공학 박사"
    t2.rows[0].cells[2].text = "지도교수 김철수"
    path = tmp_path / "표이력서.docx"
    d.save(str(path))
    return path


def test_table_contents_are_extracted(table_cv):
    """표만 읽지 못하면 CV 가 통째로 비어 LLM 이 아무것도 못 한다."""
    text = extract_text(table_cv)
    for expected in (
        "홍길동",
        "1992.03.15",
        "010-1234-5678",
        "hong@example.com",
        "서울대학교",
        "김철수",
    ):
        assert expected in text, f"{expected} 가 누락됐다"


def test_paragraphs_and_tables_keep_document_order(table_cv):
    text = extract_text(table_cv)
    assert text.index("이력서") < text.index("홍길동") < text.index("[학력]") < text.index("서울대학교")


def test_table_row_keeps_cell_separation(table_cv):
    """셀 경계가 사라지면 '성명홍길동' 처럼 붙어 모델이 헷갈린다."""
    text = extract_text(table_cv)
    assert "성명 | 홍길동 (Gildong Hong)" in text


def test_nested_table_is_extracted(tmp_path):
    d = docx.Document()
    outer = d.add_table(rows=1, cols=1)
    inner = outer.rows[0].cells[0].add_table(rows=1, cols=2)
    inner.rows[0].cells[0].text = "전공"
    inner.rows[0].cells[1].text = "컴퓨터공학"
    path = tmp_path / "중첩.docx"
    d.save(str(path))
    assert "컴퓨터공학" in extract_text(path)


def test_header_footer_contact_is_extracted(tmp_path):
    """연락처를 머리글에 넣는 이력서가 있다."""
    d = docx.Document()
    d.add_paragraph("경력 사항")
    d.sections[0].header.paragraphs[0].text = "홍길동 / 010-9999-8888"
    path = tmp_path / "머리글.docx"
    d.save(str(path))
    text = extract_text(path)
    assert "010-9999-8888" in text


def test_plain_paragraph_docx_still_works(tmp_path):
    d = docx.Document()
    d.add_paragraph("홍길동")
    d.add_paragraph("서울대학교 박사")
    path = tmp_path / "문단.docx"
    d.save(str(path))
    text = extract_text(path)
    assert "홍길동" in text and "서울대학교 박사" in text


def test_empty_docx_returns_empty(tmp_path):
    d = docx.Document()
    path = tmp_path / "빈문서.docx"
    d.save(str(path))
    assert extract_text(path) == ""


def test_unsupported_format_rejected(tmp_path):
    path = tmp_path / "a.hwp"
    path.write_bytes(b"x")
    with pytest.raises(UnsupportedFormat):
        extract_text(path)
