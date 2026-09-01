"""4단계: 채용 현황 · 단계별 상태 · 부서/과제 · 첨부파일 · CV 없이 등록."""

from __future__ import annotations

import pytest

from cvtool.recruit import RECRUIT_COLUMNS, STAGES, STATUSES, RecruitStore
from cvtool.store import CandidateStore


@pytest.fixture
def rc(tmp_path):
    return RecruitStore(tmp_path / "r.db")


@pytest.fixture
def store(tmp_path):
    return CandidateStore(tmp_path / "c.db")


# --- 단계 구성 ---------------------------------------------------------------
def test_stage_order_is_fixed():
    assert STAGES == ("서류 검토", "전화 면접", "기술 면접", "HR 면접")


def test_statuses():
    assert set(STATUSES) == {"", "진행중", "합격", "불합격", "보류"}


def test_recruit_columns_include_stages_and_note():
    for c in (*STAGES, "부서", "과제", "채용_비고", "최종상태"):
        assert c in RECRUIT_COLUMNS


# --- 상태 변경 ---------------------------------------------------------------
def test_set_and_get_stage(rc):
    rc.set_stage("A", "서류 검토", "합격", "hr1")
    assert rc.get("A").단계상태["서류 검토"] == "합격"


def test_set_stage_returns_previous(rc):
    rc.set_stage("A", "서류 검토", "진행중", "hr1")
    assert rc.set_stage("A", "서류 검토", "합격", "hr1") == "진행중"


def test_unknown_stage_rejected(rc):
    with pytest.raises(ValueError):
        rc.set_stage("A", "임원 면접", "합격", "hr1")


def test_unknown_status_rejected(rc):
    with pytest.raises(ValueError):
        rc.set_stage("A", "서류 검토", "아마도합격", "hr1")


def test_updater_recorded(rc):
    rc.set_stage("A", "서류 검토", "합격", "hr1")
    assert rc.get("A").갱신자 == "hr1"
    assert rc.get("A").갱신일시


# --- 최종상태 요약 -----------------------------------------------------------
def test_not_started(rc):
    assert rc.get("없는사람").최종상태 == "미시작"


def test_in_progress_shows_latest_stage(rc):
    rc.set_stage("A", "서류 검토", "합격", "hr1")
    rc.set_stage("A", "전화 면접", "진행중", "hr1")
    assert rc.get("A").최종상태 == "전화 면접 진행중"


def test_rejected_shows_where(rc):
    rc.set_stage("A", "서류 검토", "합격", "hr1")
    rc.set_stage("A", "전화 면접", "불합격", "hr1")
    p = rc.get("A")
    assert p.최종상태 == "전화 면접 불합격"
    assert p.탈락 is True


def test_final_pass(rc):
    for s in STAGES:
        rc.set_stage("A", s, "합격", "hr1")
    assert rc.get("A").최종상태 == "최종 합격"


# --- 정렬: 불합격은 아래로 ---------------------------------------------------
def test_rejected_sorts_last(rc):
    rc.set_stage("합격자", "서류 검토", "합격", "hr1")
    rc.set_stage("합격자", "전화 면접", "합격", "hr1")
    rc.set_stage("탈락자", "서류 검토", "불합격", "hr1")
    rc.set_stage("보류자", "서류 검토", "보류", "hr1")
    rc.set_stage("진행자", "서류 검토", "진행중", "hr1")

    순서 = sorted(
        ["탈락자", "보류자", "진행자", "합격자"], key=lambda c: rc.get(c).정렬키()
    )
    assert 순서[-1] == "탈락자"           # 불합격은 항상 맨 아래
    assert 순서[0] == "합격자"            # 많이 진행된 사람이 위로
    assert 순서.index("보류자") > 순서.index("진행자")


def test_rejected_stays_last_even_when_sorting_by_column(rc):
    """열 정렬을 눌러도 불합격자는 아래에 남아야 한다."""
    rc.set_stage("A", "서류 검토", "불합격", "hr1")
    rc.set_stage("B", "서류 검토", "진행중", "hr1")
    키 = lambda c, v: (rc.get(c).정렬키()[0], v)  # noqa: E731 - 화면 정렬과 같은 규칙
    assert sorted([("A", "가"), ("B", "하")], key=lambda t: 키(*t))[-1][0] == "A"


# --- 부서 · 과제 · 비고 ------------------------------------------------------
def test_assignment(rc):
    rc.set_assignment("A", 1, 10, "hr1")
    p = rc.get("A")
    assert (p.부서_id, p.project_id) == (1, 10)


def test_assignment_returns_previous(rc):
    rc.set_assignment("A", 1, 10, "hr1")
    assert rc.set_assignment("A", 2, 20, "hr1") == (1, 10)


def test_note(rc):
    assert rc.set_note("A", "2차 면접 조율중", "hr1") == ""
    assert rc.get("A").채용_비고 == "2차 면접 조율중"


def test_delete_removes_everything(rc):
    rc.set_stage("A", "서류 검토", "합격", "hr1")
    rc.set_note("A", "메모", "hr1")
    rc.delete("A")
    p = rc.get("A")
    assert p.단계상태 == {} and p.채용_비고 == ""


# --- 표 열 구성 --------------------------------------------------------------
def test_default_columns_include_stages(rc):
    cols = rc.columns()
    assert "한글_이름" in cols and "서류 검토" in cols


def test_admin_can_set_columns_and_order(rc):
    rc.set_columns(["최종상태", "한글_이름", "채용_비고"])
    assert rc.columns() == ["최종상태", "한글_이름", "채용_비고"]


def test_setting_columns_replaces_previous(rc):
    rc.set_columns(["한글_이름"])
    rc.set_columns(["채용_비고"])
    assert rc.columns() == ["채용_비고"]


# --- 첨부파일 ---------------------------------------------------------------
def test_multiple_attachments(store):
    rec = store.create_blank()
    store.add_attachment(rec.지원자_ID, "자기소개서.pdf", b"a", "hr1")
    store.add_attachment(rec.지원자_ID, "포트폴리오.pptx", b"b", "hr1")
    names = [a["파일명"] for a in store.attachments(rec.지원자_ID)]
    assert names == ["자기소개서.pdf", "포트폴리오.pptx"]


def test_attachment_stored_under_candidate_id(store):
    """파일명에 지원자 이름이 남지 않게 한다."""
    rec = store.create_blank()
    store.add_attachment(rec.지원자_ID, "홍길동_자소서.pdf", b"a")
    저장명 = store.attachments(rec.지원자_ID)[0]["저장명"]
    assert 저장명.startswith(rec.지원자_ID) and "홍길동" not in 저장명


def test_attachment_rejects_executable(store):
    rec = store.create_blank()
    with pytest.raises(ValueError):
        store.add_attachment(rec.지원자_ID, "virus.exe", b"MZ")


def test_delete_attachment_removes_file(store):
    rec = store.create_blank()
    aid = store.add_attachment(rec.지원자_ID, "a.pdf", b"x")
    경로 = store.files_dir / store.attachment(aid)["저장명"]
    assert 경로.exists()
    store.delete_attachment(aid)
    assert not 경로.exists()
    assert store.attachments(rec.지원자_ID) == []


def test_deleting_candidate_removes_attachments(store):
    rec = store.create_blank()
    store.add_attachment(rec.지원자_ID, "a.pdf", b"x")
    store.add_attachment(rec.지원자_ID, "b.pdf", b"y")
    store.delete(rec.지원자_ID)
    assert list(store.files_dir.iterdir()) == []


def test_attachments_are_not_orphans(store):
    """첨부파일이 고아로 잡혀 기동 때 지워지면 안 된다."""
    rec = store.create_blank()
    store.add_attachment(rec.지원자_ID, "a.pdf", b"x")
    assert store.orphan_files() == []


def test_attachment_is_owner_only(store):
    from cvtool.fsutil import is_world_readable

    rec = store.create_blank()
    aid = store.add_attachment(rec.지원자_ID, "a.pdf", b"x")
    assert not is_world_readable(store.files_dir / store.attachment(aid)["저장명"])


# --- CV 없이 등록 ------------------------------------------------------------
def test_create_blank_candidate(store):
    """다른 지원서로 지원한 경우처럼 CV 파일이 없을 때."""
    rec = store.create_blank()
    assert rec.지원자_ID.startswith("CV-")
    assert store.get(rec.지원자_ID) is not None
    assert rec.검토_필요 == "Y"          # 사람이 채워야 한다는 표시
    assert store.file_path(rec.지원자_ID) is None


def test_blank_candidates_get_unique_ids(store):
    ids = {store.create_blank().지원자_ID for _ in range(5)}
    assert len(ids) == 5
    assert store.count() == 5


def test_blank_candidate_can_be_edited(store):
    from cvtool.edit import apply_edit

    rec = store.create_blank()
    apply_edit(rec, "한글_이름", "홍길동")
    store.save(rec)
    assert store.get(rec.지원자_ID).한글_이름 == "홍길동"
