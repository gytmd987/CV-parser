"""6단계: 학회·저널 통합, 표 공통 기능, 채용 현황 일괄 저장, 파일 완전 삭제."""

from __future__ import annotations

import sqlite3

import pytest

from cvtool.names import NameRegistry
from cvtool.normalize import phone
from cvtool.schemas import COLUMNS, CVRecord, columns
from cvtool.store import CandidateStore


# --- 전화번호 국가번호 --------------------------------------------------------
@pytest.mark.parametrize(
    "입력, 기대",
    [
        ("+82 10-1234-5678", "010-1234-5678"),
        ("+82-10-1234-5678", "010-1234-5678"),
        ("+821012345678", "010-1234-5678"),
        ("0082-10-9876-5432", "010-9876-5432"),
        ("82 10 1234 5678", "010-1234-5678"),
        ("+82 (0)10-1234-5678", "010-1234-5678"),   # 국가번호와 0 이 같이 적힌 경우
        ("+82 2 880 1234", "02-880-1234"),          # 서울 유선
        ("+82-31-123-4567", "031-123-4567"),        # 지역 유선
        ("+82 070 4123 4567", "070-4123-4567"),
        ("01012345678", "010-1234-5678"),
        ("1588-1234", "1588-1234"),
    ],
)
def test_korean_country_code_becomes_local(입력, 기대):
    assert phone(입력) == 기대


@pytest.mark.parametrize("입력", ["+1 415 555 0123", "+44 20 7123 4567"])
def test_foreign_numbers_left_alone(입력):
    """한국 번호가 아니면 손대지 않는다. 지어내는 것보다 원문이 낫다."""
    assert phone(입력) == 입력


# --- 지원자_ID 열 제거 --------------------------------------------------------
def test_candidate_id_not_a_table_column():
    assert "지원자_ID" not in COLUMNS
    assert "지원자_ID" not in columns()
    assert "지원자_ID" not in CVRecord(지원자_ID="CV-1").to_row()


def test_candidate_id_still_on_the_record():
    """표에서만 뺀다. 내부 키라서 레코드에는 그대로 있어야 한다."""
    assert CVRecord(지원자_ID="CV-1").지원자_ID == "CV-1"


def test_xlsx_has_no_id_column():
    from cvtool.export import records_to_xlsx

    data = records_to_xlsx([CVRecord(지원자_ID="CV-1", 한글_이름="홍길동")])
    assert b"CV-1" not in data


# --- 학회·저널 한 사전 --------------------------------------------------------
@pytest.fixture
def reg(tmp_path):
    return NameRegistry(tmp_path / "n.db")


def test_journal_and_conference_are_one_entry(reg):
    a = reg.observe("학회", "Nature Communications")
    b = reg.observe("저널", "Nature Communications")
    assert a.id == b.id


def test_subtype_is_a_column_not_a_kind(reg):
    나 = reg.observe("저널", "IEEE TPAMI")
    assert reg.get(나.id).유형 == "저널"
    reg.classify(나.id, 유형="학회")
    assert reg.get(나.id).유형 == "학회"


def test_unknown_subtype_filled_when_learned(reg):
    """처음에 유형을 모른 채 들어왔어도 나중에 알게 되면 채운다."""
    나 = reg.observe("학회·저널", "Some Venue")
    assert reg.get(나.id).유형 == "불명"
    reg.observe("저널", "Some Venue")
    assert reg.get(나.id).유형 == "저널"


def test_impact_factor_saved_and_cleared(reg):
    나 = reg.observe("저널", "Nature")
    reg.classify(나.id, IF="64.8")
    assert reg.get(나.id).IF == "64.8"
    reg.classify(나.id, IF="")          # 지울 수 있어야 한다
    assert reg.get(나.id).IF == ""


def test_google_link_prefills_the_search(reg):
    나 = reg.observe("저널", "Nature Communications")
    url = reg.get(나.id).google_url()
    assert url.startswith("https://www.google.com/search?q=")
    assert "Nature+Communications" in url and "impact+factor" in url


def test_merge_keeps_the_side_that_has_information(reg):
    a = reg.observe("학회", "ICML")
    b = reg.observe("학회", "Intl Conf on Machine Learning")
    reg.classify(b.id, 등급="최우수", IF="1.0", 유형="저널")
    reg.merge(b.id, a.id)              # 정보가 있는 쪽을 별칭으로 넣어도
    남은 = reg.get(a.id)
    assert (남은.등급, 남은.IF) == ("최우수", "1.0")
    assert 남은.유형 == "학회"          # 대표가 이미 알고 있던 값은 지키다


# --- 중복 정리 ----------------------------------------------------------------
def test_duplicate_rows_are_merged_on_open(tmp_path):
    """학회/저널이 따로 관리되던 시절 같은 이름이 양쪽에 하나씩 생겼다."""
    path = tmp_path / "n.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE names (id INTEGER PRIMARY KEY AUTOINCREMENT, 종류 TEXT NOT NULL,"
        " 정규화키 TEXT NOT NULL, 표시명 TEXT NOT NULL, 등급 TEXT DEFAULT '미분류',"
        " 국내해외 TEXT DEFAULT '불명', 발견횟수 INTEGER DEFAULT 0,"
        " 최초등록 TEXT DEFAULT '', UNIQUE(종류, 정규화키));"
        "CREATE TABLE name_aliases (종류 TEXT NOT NULL, 별칭키 TEXT NOT NULL,"
        " name_id INTEGER NOT NULL, PRIMARY KEY (종류, 별칭키));"
    )
    conn.execute("INSERT INTO names (종류,정규화키,표시명,등급,발견횟수)"
                 " VALUES ('학회','natcomm','Nat. Comm.','미분류',2)")
    conn.execute("INSERT INTO names (종류,정규화키,표시명,등급,국내해외,발견횟수)"
                 " VALUES ('저널','natcomm','Nature Communications','최우수','해외',5)")
    conn.commit()
    conn.close()

    reg = NameRegistry(path)
    남은 = reg.list_all("학회·저널")
    assert len(남은) == 1
    n = 남은[0]
    assert n.표시명 == "Nature Communications"     # 많이 나온 쪽이 대표
    assert n.발견횟수 == 7                          # 횟수는 합친다
    assert (n.등급, n.국내해외) == ("최우수", "해외")   # 분류해 둔 값이 살아남는다


def test_no_duplicates_left_anywhere(reg):
    for n in ("ICML", "ICML 2023", "Proc. of ICML"):
        reg.observe("학회", n)
    reg.observe("저널", "ICML")
    키들 = [(i.종류, i.정규화키) for i in reg.list_all()]
    assert len(키들) == len(set(키들))


# --- 삭제하면 파일이 남지 않는다 ------------------------------------------------
def test_delete_removes_every_file_of_the_candidate(tmp_path):
    store = CandidateStore(tmp_path / "c.db", tmp_path / "files")
    저장 = store.store_file("CV-1", "a.pdf", b"cv")
    store.save(CVRecord(지원자_ID="CV-1"), 저장_파일명=저장)
    store.add_attachment("CV-1", "포트폴리오.pdf", b"att")
    # DB 와 연결이 끊긴 파일(재분석 중 오류 등)도 남아 있을 수 있다
    (store.files_dir / "CV-1.docx").write_bytes(b"orphan")

    assert len(store.files_of("CV-1")) == 3
    store.delete("CV-1")
    assert store.files_of("CV-1") == []
    assert list(store.files_dir.iterdir()) == []


def test_delete_does_not_touch_other_candidates(tmp_path):
    store = CandidateStore(tmp_path / "c.db", tmp_path / "files")
    for cid in ("CV-1", "CV-2"):
        store.save(CVRecord(지원자_ID=cid),
                   저장_파일명=store.store_file(cid, "a.pdf", b"x"))
    store.delete("CV-1")
    assert [f.name for f in store.files_dir.iterdir()] == ["CV-2.pdf"]
