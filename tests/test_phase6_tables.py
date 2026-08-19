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



# --- 중복 정리 ----------------------------------------------------------------


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


# --- 대표명을 고쳐도 원래 표기는 남는다 ---------------------------------------
def test_original_spelling_survives_a_rename(reg):
    """대표명을 고치고 나면 CV 에 뭐라고 적혀 있었는지 알 길이 없어지면 안 된다."""
    나 = reg.observe("소속", "포항공과대학교(POSTECH)")
    reg.classify(나.id, 표시명="포항공대")
    이후 = reg.get(나.id)
    assert 이후.표시명 == "포항공대"
    assert 이후.원표기 == "포항공과대학교(POSTECH)"     # 원문은 그대로
    assert 이후.정규화키 == "포항공과대학교"             # 매칭 키도 그대로


def test_same_name_rows_are_kept_apart(reg):
    """'포항공과대학교'와 'POSTECH'을 둘 다 '포항공대'로 불러도 줄은 남아 있어야 한다."""
    a = reg.observe("소속", "포항공과대학교")
    reg.observe("소속", "포항공과대학교")
    b = reg.observe("소속", "POSTECH")
    reg.classify(a.id, 표시명="포항공대")
    reg.classify(b.id, 표시명="포항공대")

    남은 = reg.list_all("소속")
    assert len(남은) == 2                                  # 합치지 않는다
    assert {n.원표기 for n in 남은} == {"포항공과대학교", "POSTECH"}
    assert {n.발견횟수 for n in 남은} == {2, 1}             # 각자 세던 횟수도 그대로
    # 표에는 둘 다 같은 이름으로 나온다
    assert reg.display("소속", "포항공과대학교") == "포항공대"
    assert reg.display("소속", "POSTECH") == "포항공대"


def test_same_display_groups_reports_the_overlap(reg):
    a = reg.observe("소속", "포항공과대학교")
    b = reg.observe("소속", "POSTECH")
    reg.observe("소속", "서울대학교")
    assert reg.same_display_groups("소속") == {}

    reg.classify(a.id, 표시명="포항공대")
    reg.classify(b.id, 표시명="포항공대")
    겹침 = reg.same_display_groups("소속")
    assert list(겹침) == ["포항공대"]
    assert sorted(겹침["포항공대"]) == sorted([a.id, b.id])



def test_old_db_gets_an_original_spelling_column(tmp_path):
    """원표기 열이 없던 DB 도 열려야 하고, 되살릴 수 있는 만큼 되살린다."""
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
    # 이름을 안 고친 줄과, 이미 '포항공대'로 고쳐버린 줄
    conn.execute("INSERT INTO names (종류,정규화키,표시명) VALUES ('소속','서울대학교','서울대학교')")
    conn.execute("INSERT INTO names (종류,정규화키,표시명) VALUES ('소속','postech','포항공대')")
    conn.commit(); conn.close()

    reg = NameRegistry(path)
    원표기 = {n.표시명: n.원표기 for n in reg.list_all("소속")}
    assert 원표기["서울대학교"] == "서울대학교"      # 안 고친 줄은 그대로
    assert 원표기["포항공대"] == "postech"          # 고친 줄은 키가 유일한 흔적


# --- 표 열 설정 (기본 열 + 추가 열) ---------------------------------------------
@pytest.fixture
def store(tmp_path):
    return CandidateStore(tmp_path / "c.db")


def test_column_label_defaults_to_the_column_name(store):
    assert store.label("한글_이름") == "한글_이름"


def test_column_can_be_renamed_for_display(store):
    store.set_column("영문_이름", 표시이름="English Name")
    assert store.label("영문_이름") == "English Name"
    assert store.labels(["영문_이름", "한글_이름"]) == {
        "영문_이름": "English Name", "한글_이름": "한글_이름",
    }


def test_hidden_column_drops_out_of_the_table(store):
    cols = ["A", "B", "C"]
    store.set_column("B", 숨김=True)
    assert store.arrange(cols) == ["A", "C"]


def test_order_moves_one_column_without_shuffling_the_rest(store):
    cols = ["A", "B", "C", "D"]
    store.set_column("D", 순서=1)
    assert store.arrange(cols) == ["D", "A", "B", "C"]


def test_untouched_columns_keep_their_original_order(store):
    cols = ["A", "B", "C"]
    assert store.arrange(cols) == cols


def test_column_settings_survive_a_reopen(tmp_path):
    path = tmp_path / "c.db"
    s1 = CandidateStore(path)
    s1.set_column("한글_이름", 표시이름="이름", 순서=2)
    s2 = CandidateStore(path)
    assert s2.label("한글_이름") == "이름"
    assert s2.column_config()["한글_이름"]["순서"] == 2


def test_renamed_column_goes_into_the_excel_header():
    import io
    import re
    import zipfile

    from cvtool.export import records_to_xlsx

    data = records_to_xlsx([CVRecord(지원자_ID="CV-1", 한글_이름="홍길동")],
                           열=["한글_이름", "영문_이름"],
                           라벨={"한글_이름": "이름", "영문_이름": "Name"})
    sheet = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode()
    assert re.findall(r"<t[^>]*>([^<]*)</t>", sheet) == ["이름", "Name", "홍길동"]
