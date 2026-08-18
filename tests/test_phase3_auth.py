"""3단계: 계정 · 권한 · 조직 · 감사 로그 · 수동 수정 · 동시 편집."""

from __future__ import annotations

import pytest

from cvtool.audit import AuditLog
from cvtool.auth import ROLES, AuthStore, User, can, hash_password, verify_password
from cvtool.edit import (
    CHOICE_FIELDS,
    READONLY_FIELDS,
    ConflictError,
    ValidationError,
    apply_edit,
    field_spec,
    validate,
)
from cvtool.schemas import CVRecord


@pytest.fixture
def auth(tmp_path):
    return AuthStore(tmp_path / "admin.db")


@pytest.fixture
def log(tmp_path):
    return AuditLog(tmp_path / "audit.db")


# --- 비밀번호 ---------------------------------------------------------------
def test_password_is_not_stored_in_plaintext():
    h = hash_password("비밀번호1234")
    assert "비밀번호1234" not in h
    assert verify_password("비밀번호1234", h)
    assert not verify_password("다른비번", h)


def test_same_password_gets_different_hashes():
    """소금이 달라야 한 명이 뚫려도 나머지가 안전하다."""
    assert hash_password("같은비번") != hash_password("같은비번")


def test_broken_hash_does_not_crash():
    assert verify_password("x", "쓰레기값") is False


# --- 권한 -------------------------------------------------------------------
@pytest.mark.parametrize(
    "역할,행동,기대",
    [
        ("관리자", "계정_전체관리", True),
        ("채용담당자", "계정_전체관리", False),   # 관리자 계정은 못 만든다
        ("채용담당자", "계정_현업추가", True),
        ("현업", "계정_현업추가", False),
        ("채용담당자", "지원자_수정", True),
        ("현업", "지원자_수정", False),           # 현업은 지원자 정보를 못 고친다
        ("현업", "채용현황_수정", True),           # 자기 과제 채용 상태는 고칠 수 있다
        ("관리자", "열_구성", True),
        ("채용담당자", "열_구성", False),          # 표 열 구성은 관리자만
        ("현업", "명칭_관리", False),
    ],
)
def test_permission_matrix(역할, 행동, 기대):
    assert can(User("u", "u", 역할), 행동) is 기대


def test_inactive_user_can_do_nothing(auth):
    u = auth.create_user("x", "X", "pw1234", "관리자")
    auth.set_active("x", False)
    assert can(auth.get_user("x"), "지원자_조회") is False


def test_none_user_has_no_permission():
    assert can(None, "지원자_조회") is False


# --- 계정 -------------------------------------------------------------------
def test_create_and_authenticate(auth):
    auth.create_user("hr1", "채용담당", "pw1234", "채용담당자")
    assert auth.authenticate("hr1", "pw1234").역할 == "채용담당자"
    assert auth.authenticate("hr1", "틀림") is None


def test_duplicate_id_rejected(auth):
    auth.create_user("a", "A", "pw1234", "현업")
    with pytest.raises(ValueError):
        auth.create_user("a", "A2", "pw1234", "현업")


def test_short_password_rejected(auth):
    with pytest.raises(ValueError):
        auth.create_user("a", "A", "123", "현업")


def test_unknown_role_rejected(auth):
    with pytest.raises(ValueError):
        auth.create_user("a", "A", "pw1234", "사장님")


def test_inactive_cannot_login(auth):
    auth.create_user("a", "A", "pw1234", "현업")
    auth.set_active("a", False)
    assert auth.authenticate("a", "pw1234") is None


# --- 세션 -------------------------------------------------------------------
def test_session_survives_restart(tmp_path):
    """서버를 재시작해도 로그인이 유지돼야 한다 (메모리 세션이 아니다)."""
    a1 = AuthStore(tmp_path / "admin.db")
    a1.create_user("a", "A", "pw1234", "관리자")
    token = a1.start_session("a")
    a1.close()

    a2 = AuthStore(tmp_path / "admin.db")   # 재시작 흉내
    assert a2.user_for_session(token).아이디 == "a"


def test_logout_invalidates_session(auth):
    auth.create_user("a", "A", "pw1234", "관리자")
    token = auth.start_session("a")
    auth.end_session(token)
    assert auth.user_for_session(token) is None


def test_bad_token_is_rejected(auth):
    assert auth.user_for_session("아무거나") is None


# --- 부서 · 과제 ------------------------------------------------------------
def test_project_belongs_to_department(auth):
    d = auth.add_department("반도체연구소")
    auth.add_project(d, "차세대메모리")
    p = auth.projects(d)[0]
    assert p["부서명"] == "반도체연구소" and p["이름"] == "차세대메모리"


def test_same_project_name_in_different_departments(auth):
    d1, d2 = auth.add_department("A부서"), auth.add_department("B부서")
    auth.add_project(d1, "공통과제")
    auth.add_project(d2, "공통과제")
    assert len(auth.projects()) == 2


def test_deleting_department_removes_its_projects(auth):
    d = auth.add_department("없어질부서")
    auth.add_project(d, "과제")
    auth.delete_department(d)
    assert auth.projects() == []


def test_project_invite_password(auth):
    d = auth.add_department("부서")
    pid = auth.add_project(d, "과제", "초대암호1234")
    assert auth.check_project_password(pid, "초대암호1234")
    assert not auth.check_project_password(pid, "틀림")


# --- 현업의 과제 격리 --------------------------------------------------------
def test_staff_sees_everything(auth):
    for 역할 in ("관리자", "채용담당자"):
        u = auth.create_user(f"u{역할}", "u", "pw1234", 역할)
        assert auth.visible_project_ids(u) is None   # 제한 없음


def test_engineer_sees_only_assigned_projects(auth):
    d = auth.add_department("부서")
    p1, p2 = auth.add_project(d, "과제1"), auth.add_project(d, "과제2")
    u = auth.create_user("eng", "현업", "pw1234", "현업")
    auth.assign("eng", p1)
    assert auth.visible_project_ids(u) == {p1}
    assert p2 not in auth.visible_project_ids(u)


def test_unassign(auth):
    d = auth.add_department("부서")
    p = auth.add_project(d, "과제")
    auth.create_user("eng", "현업", "pw1234", "현업")
    auth.assign("eng", p)
    auth.unassign("eng", p)
    assert auth.project_ids_of("eng") == set()


# --- 감사 로그 ---------------------------------------------------------------
def test_audit_records_who_what_when(log):
    log.record("hr1", "지원자", "CV-1", 항목="박사_학교", 이전값="서울대", 새값="서울대학교")
    e = log.for_target("지원자", "CV-1")[0]
    assert e.사용자 == "hr1"
    assert e.summary() == "박사_학교: 서울대 → 서울대학교"
    assert e.일시


def test_audit_shows_empty_as_placeholder(log):
    log.record("a", "지원자", "CV-1", 항목="이메일", 이전값="", 새값="a@x.com")
    assert "(빈칸) → a@x.com" in log.for_target("지원자", "CV-1")[0].summary()


def test_audit_newest_first(log):
    for i in range(3):
        log.record("a", "지원자", "CV-1", 항목="메모", 새값=str(i))
    assert [e.새값 for e in log.for_target("지원자", "CV-1")] == ["2", "1", "0"]


def test_audit_filter_by_kind(log):
    log.record("a", "지원자", "CV-1", 비고="수정")
    log.record("a", "계정", "u1", 비고="생성")
    assert len(log.recent(대상종류="계정")) == 1
    assert len(log.recent()) == 2


# --- 형식 강제 ---------------------------------------------------------------
def test_choice_field_rejects_free_text():
    with pytest.raises(ValidationError):
        validate("현재_신분", "박사후연구원")


def test_choice_field_accepts_listed_value():
    assert validate("현재_신분", "포닥") == "포닥"


@pytest.mark.parametrize("값", ["언젠가", "abc", "미정"])
def test_yyyymm_rejects_bad_input(값):
    with pytest.raises(ValidationError):
        validate("박사_졸업", 값)


def test_year_only_becomes_yyyy00():
    """연도만 알면 월을 지어내지 않고 00 으로 둔다."""
    assert validate("박사_졸업", "2019년쯤") == "201900"
    assert validate("박사_졸업", "2019") == "201900"


def test_yyyymm_normalizes_good_input():
    assert validate("박사_졸업", "2025.02") == "202502"


def test_birthdate_requires_full_date():
    with pytest.raises(ValidationError):
        validate("생년월일", "1992")


def test_email_must_contain_at():
    with pytest.raises(ValidationError):
        validate("이메일", "골뱅이없음")


def test_email_multiple_normalized():
    assert validate("이메일", "A@x.com, b@Y.com") == "a@x.com | b@y.com"


def test_readonly_fields_cannot_be_edited():
    for f in READONLY_FIELDS:
        with pytest.raises(ValidationError):
            validate(f, "아무값")


def test_field_spec_gives_dropdown_for_choices():
    for f in CHOICE_FIELDS:
        assert field_spec(f).입력 == "select"
    assert field_spec("박사_졸업").입력 == "yyyymm"
    assert field_spec("박사_학교").입력 == "text"


# --- 동시 편집 ---------------------------------------------------------------
def test_edit_returns_old_and_new():
    rec = CVRecord(지원자_ID="T", 박사_학교="서울대")
    assert apply_edit(rec, "박사_학교", "서울대학교") == ("서울대", "서울대학교")


def test_conflict_when_someone_else_changed_it():
    """같은 칸을 동시에 고치면 조용히 덮어쓰지 않는다."""
    rec = CVRecord(지원자_ID="T", 한글_이름="김철수")   # 다른 사람이 이미 바꿔둠
    with pytest.raises(ConflictError) as exc:
        apply_edit(rec, "한글_이름", "박영희", 기대_이전값="홍길동")
    assert "다른 사람이" in str(exc.value)
    assert rec.한글_이름 == "김철수"                    # 값은 그대로


def test_no_conflict_when_value_matches():
    rec = CVRecord(지원자_ID="T", 한글_이름="홍길동")
    apply_edit(rec, "한글_이름", "김철수", 기대_이전값="홍길동")
    assert rec.한글_이름 == "김철수"


def test_different_fields_do_not_conflict():
    """서로 다른 칸을 고치면 충돌이 없어야 한다 (구글 시트 같은 체감)."""
    rec = CVRecord(지원자_ID="T")
    apply_edit(rec, "박사_학교", "서울대학교", 기대_이전값="")
    apply_edit(rec, "이메일", "a@x.com", 기대_이전값="")
    assert rec.박사_학교 == "서울대학교" and rec.이메일 == "a@x.com"


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        apply_edit(CVRecord(지원자_ID="T"), "없는항목", "값")


def test_all_roles_are_covered():
    assert set(ROLES) == {"관리자", "채용담당자", "현업"}
