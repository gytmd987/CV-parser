"""학회·저널 레지스트리 / 엑셀 출력 / 저장소 테스트 (서비스 불필요)."""

from __future__ import annotations

import zipfile

import pytest

from cvtool.export import build_xlsx, col_letter, records_to_tsv, records_to_xlsx
from cvtool.schemas import COLUMNS, CVRecord, Paper
from cvtool.store import CandidateStore
from cvtool.venues import VenueRegistry, apply_registry, normalize


# --- 정규화: 같은 학회의 표기 흔들림을 묶는다 ------------------------------
@pytest.mark.parametrize(
    "a,b",
    [
        ("NeurIPS", "neurips"),
        ("NeurIPS 2023", "NeurIPS"),
        ("Proc. of NeurIPS 2023", "NeurIPS"),
        ("IEEE Transactions on Pattern Analysis", "Transactions on Pattern Analysis"),
    ],
)
def test_normalize_groups_variants(a, b):
    assert normalize(a) == normalize(b)


def test_normalize_keeps_distinct_venues_apart():
    assert normalize("ICML") != normalize("ICLR")


# --- 자동 등록 --------------------------------------------------------------
def test_unknown_venue_is_auto_added_as_unclassified(tmp_path):
    reg = VenueRegistry(tmp_path / "v.db")
    v = reg.observe("NeurIPS")
    assert v.등급 == "미분류"
    assert reg.unclassified_count() == 1


def test_repeat_observation_increments_count(tmp_path):
    reg = VenueRegistry(tmp_path / "v.db")
    reg.observe("NeurIPS 2023")
    v = reg.observe("Proc. of NeurIPS 2024")  # 같은 학회로 묶여야 한다
    assert v.발견횟수 == 2
    assert len(reg.list_all()) == 1


def test_classify_then_unclassified_count_drops(tmp_path):
    reg = VenueRegistry(tmp_path / "v.db")
    v = reg.observe("ICML")
    reg.classify(v.id, 등급="최우수", 국내해외="해외", 유형="학회")
    assert reg.unclassified_count() == 0
    assert reg.get(v.id).등급 == "최우수"


def test_merge_folds_alias_into_target(tmp_path):
    reg = VenueRegistry(tmp_path / "v.db")
    a = reg.observe("NIPS")
    b = reg.observe("Neural Information Processing Systems")
    reg.merge(a.id, b.id)
    assert len(reg.list_all()) == 1
    assert reg.observe("NIPS").id == b.id  # 별칭이 대상으로 해석된다


# --- 사람이 정한 분류가 LLM 추측을 이긴다 -----------------------------------
def test_registry_overrides_llm_guess(tmp_path):
    reg = VenueRegistry(tmp_path / "v.db")
    v = reg.observe("어떤학회")
    reg.classify(v.id, 등급="우수", 국내해외="해외")

    rec = CVRecord(지원자_ID="X", 논문=[Paper(제출처="어떤학회", 연도="2024", 국내해외="국내")])
    apply_registry(rec, reg)
    assert rec.논문[0].국내해외 == "해외"          # 담당자 판별이 우선
    assert rec.해외논문_제출처() == "어떤학회 2024"


def test_unclassified_venue_flags_review(tmp_path):
    reg = VenueRegistry(tmp_path / "v.db")
    rec = CVRecord(지원자_ID="X", 논문=[Paper(제출처="처음보는학회", 국내해외="해외")])
    apply_registry(rec, reg)
    assert rec.검토_필요 == "Y"
    assert "미분류" in rec.검토_사유


# --- 엑셀 ------------------------------------------------------------------
def test_col_letter():
    assert col_letter(0) == "A"
    assert col_letter(25) == "Z"
    assert col_letter(26) == "AA"
    assert col_letter(30) == "AE"


def test_xlsx_is_valid_zip_with_expected_parts():
    import io

    z = zipfile.ZipFile(io.BytesIO(build_xlsx([{c: "" for c in COLUMNS}])))
    assert z.testzip() is None
    names = set(z.namelist())
    assert "xl/worksheets/sheet1.xml" in names
    assert "xl/workbook.xml" in names
    assert "[Content_Types].xml" in names


def test_xlsx_preserves_leading_zero_phone():
    """전화번호 앞자리 0 이 살아야 한다 (엑셀 숫자 변환 방지)."""
    import io

    rec = CVRecord(지원자_ID="X", 전화번호="01012345678", 생년월일="19920315")
    z = zipfile.ZipFile(io.BytesIO(records_to_xlsx([rec])))
    sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "01012345678" in sheet
    assert 't="inlineStr"' in sheet  # 숫자 셀이 아니라 문자열 셀


def test_xlsx_escapes_xml_special_chars():
    import io

    rec = CVRecord(지원자_ID="X", 한글_이름="A & B <중요>")
    z = zipfile.ZipFile(io.BytesIO(records_to_xlsx([rec])))
    sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "&amp;" in sheet and "&lt;" in sheet
    assert "<중요>" not in sheet


def test_tsv_has_no_stray_newlines():
    """셀 안 줄바꿈이 표를 깨뜨리지 않아야 한다."""
    rec = CVRecord(지원자_ID="X", 검토_사유="줄1\n줄2\t탭")
    tsv = records_to_tsv([rec])
    assert len(tsv.splitlines()) == 2  # 헤더 + 1행
    assert tsv.splitlines()[0].split("\t") == COLUMNS


# --- 저장소 / 보관기간 -------------------------------------------------------
def test_store_roundtrip_and_expiry_recorded(tmp_path):
    store = CandidateStore(tmp_path / "c.db")
    rec = CVRecord(지원자_ID="CV-1", 한글_이름="홍길동")
    store.save(rec)
    assert store.count() == 1
    assert store.get("CV-1").한글_이름 == "홍길동"

    row = store._conn.execute("SELECT 보관_만료일 FROM candidates").fetchone()
    assert row["보관_만료일"]  # 엑셀 열에는 없지만 DB 에는 남아야 자동삭제가 된다


def test_purge_expired_removes_only_past_due(tmp_path):
    store = CandidateStore(tmp_path / "c.db")
    store.save(CVRecord(지원자_ID="CV-1"))
    assert store.purge_expired() == []  # 아직 만료 전

    store._conn.execute("UPDATE candidates SET 보관_만료일='2000-01-01'")
    store._conn.commit()
    assert store.purge_expired() == ["CV-1"]
    assert store.count() == 0
