"""5단계: 사용자 정의 열 · 부서/과제명 수정."""

from __future__ import annotations

import pytest

from cvtool.auth import AuthStore
from cvtool.edit import ValidationError, custom_field_spec, validate_custom
from cvtool.export import records_to_tsv, records_to_xlsx
from cvtool.store import CUSTOM_TYPES, CandidateStore


@pytest.fixture
def store(tmp_path):
    return CandidateStore(tmp_path / "c.db")


@pytest.fixture
def auth(tmp_path):
    return AuthStore(tmp_path / "a.db")


# --- 열 추가 ----------------------------------------------------------------
def test_add_field(store):
    store.add_field("추천인", "텍스트", 만든이="admin")
    assert store.field_names() == ["추천인"]
    assert store.field("추천인")["만든이"] == "admin"


def test_all_types_allowed(store):
    for i, t in enumerate(CUSTOM_TYPES):
        store.add_field(f"열{i}", t, "가|나" if t == "선택" else "")
    assert len(store.fields()) == len(CUSTOM_TYPES)


def test_cannot_shadow_builtin_column(store):
    """기본 열과 같은 이름을 만들면 표가 꼬인다."""
    with pytest.raises(ValueError):
        store.add_field("한글_이름", "텍스트")


def test_duplicate_field_rejected(store):
    store.add_field("추천인", "텍스트")
    with pytest.raises(ValueError):
        store.add_field("추천인", "텍스트")


def test_choice_type_needs_options(store):
    with pytest.raises(ValueError):
        store.add_field("구분", "선택", "")


def test_unknown_type_rejected(store):
    with pytest.raises(ValueError):
        store.add_field("X", "이상한유형")


def test_empty_name_rejected(store):
    with pytest.raises(ValueError):
        store.add_field("  ", "텍스트")


def test_fields_keep_insertion_order(store):
    for n in ("첫째", "둘째", "셋째"):
        store.add_field(n, "텍스트")
    assert store.field_names() == ["첫째", "둘째", "셋째"]


# --- 값 저장 ----------------------------------------------------------------
def test_set_and_get_value(store):
    store.add_field("추천인", "텍스트")
    rec = store.create_blank()
    store.set_custom(rec.지원자_ID, "추천인", "김부장")
    assert store.custom_values(rec.지원자_ID)["추천인"] == "김부장"


def test_set_returns_previous(store):
    store.add_field("추천인", "텍스트")
    rec = store.create_blank()
    store.set_custom(rec.지원자_ID, "추천인", "김부장")
    assert store.set_custom(rec.지원자_ID, "추천인", "이차장") == "김부장"


def test_value_for_unknown_field_rejected(store):
    rec = store.create_blank()
    with pytest.raises(ValueError):
        store.set_custom(rec.지원자_ID, "없는열", "값")


def test_deleting_field_removes_its_values(store):
    store.add_field("추천인", "텍스트")
    rec = store.create_blank()
    store.set_custom(rec.지원자_ID, "추천인", "김부장")
    store.delete_field("추천인")
    assert store.custom_values(rec.지원자_ID) == {}
    assert store.field_names() == []


def test_deleting_candidate_removes_its_values(store):
    store.add_field("추천인", "텍스트")
    rec = store.create_blank()
    store.set_custom(rec.지원자_ID, "추천인", "김부장")
    store.delete(rec.지원자_ID)
    assert store.custom_map() == {}


# --- 형식 강제 ---------------------------------------------------------------
def test_choice_rejects_value_outside_list():
    f = {"이름": "채용경로", "유형": "선택", "선택지": "사내추천|학회"}
    assert validate_custom(f, "사내추천") == "사내추천"
    with pytest.raises(ValidationError):
        validate_custom(f, "지인소개")


def test_choice_allows_empty():
    f = {"이름": "채용경로", "유형": "선택", "선택지": "가|나"}
    assert validate_custom(f, "") == ""


def test_yyyymm_normalized_and_enforced():
    f = {"이름": "입사가능시기", "유형": "연월"}
    assert validate_custom(f, "2026.03") == "202603"
    with pytest.raises(ValidationError):
        validate_custom(f, "내년쯤")


def test_number_strips_commas_and_enforces():
    f = {"이름": "희망연봉", "유형": "숫자"}
    assert validate_custom(f, "8,000") == "8000"
    with pytest.raises(ValidationError):
        validate_custom(f, "많이")


def test_text_collapses_whitespace():
    f = {"이름": "메모", "유형": "텍스트"}
    assert validate_custom(f, " 줄1\n줄2 ") == "줄1 줄2"


def test_field_spec_matches_type():
    assert custom_field_spec({"이름": "a", "유형": "선택", "선택지": "가|나"}).입력 == "select"
    assert custom_field_spec({"이름": "a", "유형": "연월"}).입력 == "yyyymm"
    assert custom_field_spec({"이름": "a", "유형": "숫자"}).입력 == "number"
    assert custom_field_spec({"이름": "a", "유형": "텍스트"}).입력 == "text"


def test_choice_spec_includes_blank_option():
    """비워 둘 수 있어야 한다."""
    assert custom_field_spec({"이름": "a", "유형": "선택", "선택지": "가|나"}).선택지[0] == ""


# --- 엑셀 출력 ---------------------------------------------------------------
def test_custom_columns_in_exports(store):
    store.add_field("추천인", "텍스트")
    rec = store.create_blank()
    store.set_custom(rec.지원자_ID, "추천인", "김부장")
    custom = (store.field_names(), store.custom_map())

    tsv = records_to_tsv(store.list_all(), None, custom)
    assert "추천인" in tsv.splitlines()[0]
    assert "김부장" in tsv

    import io
    import zipfile

    sheet = zipfile.ZipFile(
        io.BytesIO(records_to_xlsx(store.list_all(), None, custom))
    ).read("xl/worksheets/sheet1.xml").decode()
    assert "추천인" in sheet and "김부장" in sheet


def test_exports_without_custom_still_work(store):
    store.create_blank()
    assert records_to_tsv(store.list_all()) is not None


# --- 부서 · 과제명 수정 ------------------------------------------------------
def test_rename_department(auth):
    d = auth.add_department("반도체연구소")
    assert auth.rename_department(d, "반도체사업부") == "반도체연구소"
    assert [x["이름"] for x in auth.departments()] == ["반도체사업부"]


def test_rename_department_rejects_duplicate(auth):
    d1 = auth.add_department("A부서")
    auth.add_department("B부서")
    with pytest.raises(ValueError):
        auth.rename_department(d1, "B부서")


def test_rename_department_rejects_empty(auth):
    d = auth.add_department("부서")
    with pytest.raises(ValueError):
        auth.rename_department(d, "   ")


def test_rename_project(auth):
    d = auth.add_department("부서")
    p = auth.add_project(d, "차세대메모리")
    assert auth.rename_project(p, "차세대 D램") == "차세대메모리"
    assert [x["이름"] for x in auth.projects(d)] == ["차세대 D램"]


def test_rename_project_rejects_duplicate_in_same_department(auth):
    d = auth.add_department("부서")
    p1 = auth.add_project(d, "과제A")
    auth.add_project(d, "과제B")
    with pytest.raises(ValueError):
        auth.rename_project(p1, "과제B")


def test_same_project_name_allowed_across_departments(auth):
    d1, d2 = auth.add_department("A부서"), auth.add_department("B부서")
    p = auth.add_project(d1, "이름바꿀과제")
    auth.add_project(d2, "공통과제")
    auth.rename_project(p, "공통과제")     # 다른 부서면 겹쳐도 된다
    assert len(auth.projects()) == 2


def test_renaming_keeps_assignments(auth):
    """이름을 바꿔도 현업 배정이 풀리면 안 된다."""
    d = auth.add_department("부서")
    p = auth.add_project(d, "과제")
    auth.create_user("eng", "현업", "pw1234", "현업")
    auth.assign("eng", p)
    auth.rename_project(p, "새이름")
    auth.rename_department(d, "새부서")
    assert auth.project_ids_of("eng") == {p}


def test_change_project_invite_password(auth):
    d = auth.add_department("부서")
    p = auth.add_project(d, "과제", "old1234")
    auth.set_project_password(p, "new1234")
    assert auth.check_project_password(p, "new1234")
    assert not auth.check_project_password(p, "old1234")


def test_clear_project_invite_password(auth):
    d = auth.add_department("부서")
    p = auth.add_project(d, "과제", "old1234")
    auth.set_project_password(p, "")
    assert not auth.check_project_password(p, "old1234")
