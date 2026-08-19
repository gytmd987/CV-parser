"""2단계: 명칭 사전 · 표 즉시 반영 · 등급별 개수 열."""

from __future__ import annotations

import pytest

from cvtool.names import GRADED_KINDS, KINDS, NameRegistry, normalize, observe_record
from cvtool.schemas import CVRecord, Paper, columns


@pytest.fixture
def reg(tmp_path):
    return NameRegistry(tmp_path / "n.db")


# --- 정규화 -----------------------------------------------------------------
def test_parentheses_merge_automatically():
    """'포항공과대학교(POSTECH)' 와 '포항공과대학교' 는 저절로 묶여야 한다."""
    assert normalize("포항공과대학교(POSTECH)", "학교") == normalize("포항공과대학교", "학교")


def test_different_spellings_stay_separate_until_merged():
    """'포항공대' 와 'POSTECH' 은 자동으로 묶으면 위험하다. 사람이 묶는다."""
    assert normalize("포항공대", "학교") != normalize("POSTECH", "학교")


def test_venue_noise_removed():
    assert normalize("Proc. of ICML 2023", "학회") == normalize("ICML", "학회")


# --- 등록 · 병합 ------------------------------------------------------------
def test_observe_registers_and_counts(reg):
    a = reg.observe("학교", "서울대학교")
    b = reg.observe("학교", "서울대학교")
    assert a.id == b.id and b.발견횟수 == 2


def test_conference_and_journal_share_one_entry(reg):
    """같은 곳을 어떤 CV 는 학회로, 어떤 CV 는 저널로 적는다. 갈라지면 안 된다."""
    학회 = reg.observe("학회", "ICML")
    저널 = reg.observe("저널", "ICML")
    assert 학회.id == 저널.id
    assert reg.get(학회.id).유형 == "학회"        # 처음 본 쪽이 남는다


def test_subtype_filled_in_when_first_seen_unknown(reg):
    나 = reg.observe("소속", "가나다")           # 유형이 의미 없는 종류
    assert reg.get(나.id).유형 == "불명"


def test_kinds_and_subtypes_are_different_axes():
    from cvtool.names import GRADED_KINDS, KINDS, SUBTYPES

    assert KINDS == ("소속", "학회·저널", "전공")
    assert GRADED_KINDS == ("학회·저널",)
    assert SUBTYPES == ("학회", "저널", "불명")




def test_unknown_name_falls_back_to_raw(reg):
    assert reg.display("학교", "듣도보도못한대학교") == "듣도보도못한대학교"


def test_only_graded_kinds_get_tier(reg):
    assert reg.observe("학회", "ICML").등급 == "미분류"
    assert reg.observe("학교", "서울대학교").등급 == ""
    assert set(GRADED_KINDS) <= set(KINDS)


# --- 표 즉시 반영 (핵심) ----------------------------------------------------
def _rec() -> CVRecord:
    return CVRecord(
        지원자_ID="T",
        박사_학교="포항공과대학교(POSTECH)",
        박사_전공="전기공학",
        논문=[
            Paper(제출처="International Conference on Machine Learning", 연도="2024",
                  유형="학회", 국내해외="해외"),
            Paper(제출처="NeurIPS", 연도="2023", 유형="학회", 국내해외="해외"),
        ],
    )


def test_editing_registry_updates_existing_rows(reg):
    """관리화면에서 고치면 **이미 등록된 지원자 표**가 바로 바뀌어야 한다."""
    rec = _rec()
    observe_record(rec, reg)
    assert rec.to_row(reg)["박사_학교"] == "포항공과대학교(POSTECH)"

    학교 = reg.lookup("학교", "포항공과대학교")
    reg.classify(학교.id, 표시명="POSTECH")
    # 레코드를 다시 저장하거나 재분석하지 않았는데도 바뀐다
    assert rec.to_row(reg)["박사_학교"] == "POSTECH"


def test_venue_display_name_updates_in_table(reg):
    rec = _rec()
    observe_record(rec, reg)
    icml = reg.lookup("학회", "International Conference on Machine Learning")
    reg.classify(icml.id, 표시명="ICML", 국내해외="해외")
    assert "ICML 2024" in rec.to_row(reg)["1저자_해외논문_제출처"]


def test_observe_does_not_mutate_the_record(reg):
    """원문 표기는 절대 바뀌면 안 된다. 그래야 나중에 재분류가 가능하다."""
    rec = _rec()
    observe_record(rec, reg)
    icml = reg.lookup("학회", "International Conference on Machine Learning")
    reg.classify(icml.id, 표시명="ICML", 등급="최우수", 국내해외="국내")

    assert rec.논문[0].제출처 == "International Conference on Machine Learning"
    assert rec.논문[0].국내해외 == "해외"      # 저장값은 그대로
    assert rec.박사_학교 == "포항공과대학교(POSTECH)"
    # 화면에서만 담당자 판별이 적용된다
    assert "ICML" not in rec.to_row(reg)["1저자_해외논문_제출처"]  # 국내로 바꿨으니 빠짐


def test_reviewer_classification_beats_llm_guess(reg):
    rec = CVRecord(지원자_ID="T", 논문=[Paper(제출처="어떤학회", 유형="학회", 국내해외="국내")])
    observe_record(rec, reg)
    v = reg.lookup("학회", "어떤학회")
    reg.classify(v.id, 등급="우수", 국내해외="해외")
    assert rec.to_row(reg)["1저자_해외논문_제출처"].startswith("어떤학회")


# --- 등급별 개수 열 ---------------------------------------------------------
def test_default_tier_columns_are_top_two(reg):
    assert reg.column_tiers() == ["최우수", "우수"]


def test_tier_columns_appear_in_table(reg):
    cols = columns(reg)
    assert "1저자_해외논문_최우수" in cols
    assert "1저자_해외논문_우수" in cols
    assert "1저자_해외논문_제외" not in cols   # 제외는 열로 내지 않는다


def test_tier_counts(reg):
    rec = _rec()
    observe_record(rec, reg)
    for 제출처, 등급 in [("International Conference on Machine Learning", "최우수"),
                       ("NeurIPS", "우수")]:
        v = reg.lookup("학회", 제출처)
        reg.classify(v.id, 등급=등급, 국내해외="해외")
    row = rec.to_row(reg)
    assert row["1저자_해외논문_최우수"] == "1"
    assert row["1저자_해외논문_우수"] == "1"


def test_toggling_tier_changes_columns(reg):
    reg.set_tier_column("일반", True)
    assert "1저자_해외논문_일반" in columns(reg)
    reg.set_tier_column("우수", False)
    assert "1저자_해외논문_우수" not in columns(reg)


def test_columns_without_registry_have_no_tier_columns():
    assert not any("최우수" in c for c in columns(None))


# --- 워크샵·세션 분리 --------------------------------------------------------
def test_session_names_stay_separate_from_main_venue():
    """워크샵 논문을 본 학회와 같은 등급으로 세면 최우수 개수가 부풀어 오른다."""
    from cvtool.names import normalize

    본학회 = normalize("ICML", "학회")
    for 같음 in ("ICML 2023", "Proc. of ICML 2023", "ICML (Oral)"):
        assert normalize(같음, "학회") == 본학회
    for 다름 in ("ICML Workshop on Federated Learning",
                 "NeurIPS Datasets and Benchmarks Track"):
        assert normalize(다름, "학회") != 본학회



# --- '학교' -> '소속' 이름 변경 ------------------------------------------------

def test_company_and_school_share_one_dictionary():
    """지원자의 현재 소속은 학교일 수도 회사일 수도 있다."""
    from cvtool.schemas import NAME_COLUMNS

    assert NAME_COLUMNS["현재_소속"] == NAME_COLUMNS["박사_학교"] == "소속"


# --- 표기마다 한 줄 (합치지 않는다) --------------------------------------------
def test_every_spelling_gets_its_own_row(reg):
    """여러 표기를 한 줄로 합치면 잘못 묶인 걸 떼어낼 수 없다."""
    a = reg.observe("소속", "포항공과대학교(POSTECH)")
    b = reg.observe("소속", "포항공과대학교")
    assert a.id != b.id
    assert {n.원표기 for n in reg.list_all("소속")} == {
        "포항공과대학교(POSTECH)", "포항공과대학교",
    }


def test_new_spelling_inherits_the_group_name(reg):
    """묶기 키가 같으면 이미 정해둔 이름을 물려받는다."""
    나 = reg.observe("학회", "ICML")
    reg.classify(나.id, 표시명="ICML (Intl. Conf. on ML)")
    나중 = reg.observe("학회", "Proc. of ICML 2023")
    assert 나중.표시명 == "ICML (Intl. Conf. on ML)"
    assert 나중.원표기 == "Proc. of ICML 2023"          # 원문은 그대로


def test_wrong_grouping_can_be_pulled_apart(reg):
    """자동으로 물려받은 이름이 틀렸으면 그 줄만 고치면 된다."""
    reg.observe("소속", "가나다대학교")
    잘못 = reg.observe("소속", "가나다 대학교")            # 키가 같아 이름을 물려받음
    assert 잘못.표시명 == "가나다대학교"
    reg.classify(잘못.id, 표시명="가나다 주식회사")
    assert reg.get(잘못.id).표시명 == "가나다 주식회사"
    assert reg.display("소속", "가나다대학교") == "가나다대학교"   # 옆줄은 그대로


def test_classification_follows_the_display_name(reg):
    """ICML 은 어떻게 적혀 있든 최우수다."""
    a = reg.observe("학회", "ICML")
    b = reg.observe("학회", "국제기계학습학회")
    reg.classify(b.id, 표시명="ICML")                    # 같은 이름으로 부르기로
    reg.classify(a.id, 등급="최우수", 국내해외="해외")
    assert reg.get(b.id).등급 == "최우수"
    assert reg.get(b.id).국내해외 == "해외"


def test_siblings_lists_the_other_spellings(reg):
    a = reg.observe("소속", "서울대학교")
    b = reg.observe("소속", "SNU")
    reg.classify(b.id, 표시명="서울대학교")
    assert [n.원표기 for n in reg.siblings(a.id)] == ["SNU"]


def test_forget_removes_one_spelling(reg):
    a = reg.observe("소속", "오타대학굑")
    assert reg.forget(a.id) == "오타대학굑"
    assert reg.get(a.id) is None


def test_listed_in_display_name_order(reg):
    for s in ("힘찬대학교", "가나다대학교", "마루대학교"):
        reg.observe("소속", s)
    assert [n.표시명 for n in reg.list_all("소속")] == [
        "가나다대학교", "마루대학교", "힘찬대학교",
    ]


def test_old_two_layer_db_migrates_to_one_row_per_spelling(tmp_path):
    """예전 구조(대표명 한 줄 + 별칭표)에서 숨어 있던 표기도 각자 줄로 되살린다."""
    import sqlite3

    from cvtool.names import NameRegistry

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
    conn.execute("INSERT INTO names (종류,정규화키,표시명,등급,국내해외,발견횟수)"
                 " VALUES ('학회','icml','ICML','최우수','해외',9)")
    conn.execute("INSERT INTO name_aliases VALUES ('학회','국제기계학습학회',1)")
    conn.commit(); conn.close()

    reg = NameRegistry(path)
    표기들 = {n.원표기: n for n in reg.list_all("학회·저널")}
    assert set(표기들) == {"ICML", "국제기계학습학회"}       # 별칭도 각자 줄이 된다
    assert all(n.표시명 == "ICML" for n in 표기들.values())
    assert 표기들["ICML"].등급 == "최우수"                  # 분류는 이름에 붙어 유지
    assert 표기들["국제기계학습학회"].등급 == "최우수"
    assert reg.display("학회", "국제기계학습학회") == "ICML"
