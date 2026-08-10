"""지원자 관리 기능 테스트 (삭제·검색·만료·원문 보관)."""

from __future__ import annotations

import dataclasses

import pytest

from cvtool import config as config_mod
from cvtool.schemas import CVRecord
from cvtool.store import CandidateStore


@pytest.fixture
def store(tmp_path):
    return CandidateStore(tmp_path / "c.db")


def _rec(cid: str, **kw) -> CVRecord:
    return CVRecord(지원자_ID=cid, **kw)


# --- 삭제 -------------------------------------------------------------------
def test_delete_one(store):
    store.save(_rec("A"))
    store.save(_rec("B"))
    assert store.delete("A") is True
    assert store.count() == 1
    assert store.get("A") is None


def test_delete_missing_returns_false(store):
    assert store.delete("없음") is False


def test_delete_many(store):
    for cid in "ABC":
        store.save(_rec(cid))
    assert store.delete_many(["A", "C"]) == 2
    assert store.count() == 1


def test_delete_many_empty_is_noop(store):
    store.save(_rec("A"))
    assert store.delete_many([]) == 0
    assert store.count() == 1


def test_delete_all(store):
    for cid in "ABC":
        store.save(_rec(cid))
    assert store.delete_all() == 3
    assert store.count() == 0


# --- 만료 --------------------------------------------------------------------
def test_expired_count_and_purge(store):
    store.save(_rec("A"))
    store.save(_rec("B"))
    assert store.expired_count() == 0

    store._conn.execute("UPDATE candidates SET 보관_만료일='2000-01-01' WHERE 지원자_ID='A'")
    store._conn.commit()
    assert store.expired_count() == 1
    assert store.purge_expired() == ["A"]
    assert store.count() == 1


def test_expiry_map_has_every_candidate(store):
    store.save(_rec("A"))
    m = store.expiry_map()
    assert "A" in m and m["A"]


# --- 검색 / 필터 -------------------------------------------------------------
def test_search_matches_name_and_school(store):
    store.save(_rec("A", 한글_이름="홍길동", 박사_학교="서울대학교"))
    store.save(_rec("B", 한글_이름="김영희", 박사_학교="KAIST"))
    assert [r.지원자_ID for r in store.list_filtered("홍길동")] == ["A"]
    assert [r.지원자_ID for r in store.list_filtered("KAIST")] == ["B"]


def test_search_is_case_insensitive(store):
    store.save(_rec("A", 영문_이름="Gildong Hong"))
    assert len(store.list_filtered("gildong")) == 1


def test_search_matches_filename(store):
    store.save(_rec("A", 원본_파일명="이력서_홍길동.pdf"))
    assert len(store.list_filtered("이력서_홍길동")) == 1


def test_search_no_match_returns_empty(store):
    store.save(_rec("A", 한글_이름="홍길동"))
    assert store.list_filtered("없는사람") == []


def test_review_only_filter(store):
    store.save(_rec("A", 검토_필요="Y"))
    store.save(_rec("B"))
    assert [r.지원자_ID for r in store.list_filtered(review_only=True)] == ["A"]


def test_search_and_review_filter_combine(store):
    store.save(_rec("A", 한글_이름="홍길동", 검토_필요="Y"))
    store.save(_rec("B", 한글_이름="홍길동"))
    result = store.list_filtered("홍길동", review_only=True)
    assert [r.지원자_ID for r in result] == ["A"]


# --- 원문 보관 (개인정보 설정) -----------------------------------------------
def _set_store_cv_text(monkeypatch, value: bool) -> None:
    """Settings 는 frozen 이므로 복사본으로 갈아끼운다."""
    monkeypatch.setattr(
        "cvtool.store.settings", dataclasses.replace(config_mod.settings, store_cv_text=value)
    )


def test_cv_text_not_stored_by_default(store, monkeypatch):
    """기본은 원문 미보관 — 개인정보 최소 수집."""
    assert config_mod.settings.store_cv_text is False  # 기본값 자체를 고정
    _set_store_cv_text(monkeypatch, False)
    store.save(_rec("A"), 원문_텍스트="이력서 전문 내용")
    assert store.get_text("A") == ""
    assert store.meta("A")["원문보유"] == 0


def test_cv_text_stored_when_enabled(store, monkeypatch):
    """켜면 재업로드 없이 재분석할 수 있다."""
    _set_store_cv_text(monkeypatch, True)
    store.save(_rec("A"), 원문_텍스트="이력서 전문 내용")
    assert store.get_text("A") == "이력서 전문 내용"
    assert store.meta("A")["원문보유"] == 1


def test_reanalyze_overwrites_same_id(store, monkeypatch):
    """재분석은 같은 ID 로 덮어써야 한다 (행이 늘어나면 안 된다)."""
    _set_store_cv_text(monkeypatch, True)
    store.save(_rec("A", 한글_이름="구버전"), 원문_텍스트="원문")
    store.save(_rec("A", 한글_이름="새버전"), 원문_텍스트="원문")
    assert store.count() == 1
    assert store.get("A").한글_이름 == "새버전"


# --- 마이그레이션 ------------------------------------------------------------
def test_migration_adds_column_to_old_db(tmp_path):
    """원문_텍스트 열이 없던 기존 DB 도 열려야 한다."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE candidates (지원자_ID TEXT PRIMARY KEY, 등록일시 TEXT NOT NULL,"
        " 원본_파일명 TEXT, 보관_만료일 TEXT, record_json TEXT NOT NULL);"
    )
    conn.execute(
        "INSERT INTO candidates VALUES ('OLD','2026-01-01','a.pdf','2026-07-01',?)",
        (CVRecord(지원자_ID="OLD").model_dump_json(),),
    )
    conn.commit()
    conn.close()

    store = CandidateStore(path)  # 여기서 마이그레이션이 돌아야 한다
    assert store.count() == 1
    assert store.get_text("OLD") == ""
    assert store.get("OLD").지원자_ID == "OLD"


def test_meta_returns_none_for_missing(store):
    assert store.meta("없음") is None


# --- 원본 파일 보관 ----------------------------------------------------------
def test_store_file_names_by_id_not_by_applicant_name(store):
    """파일명에 지원자 이름을 쓰면 파일명 자체가 개인정보가 된다."""
    saved = store.store_file("CV-1", "이력서_홍길동.pdf", b"%PDF-1.4 fake")
    assert saved == "CV-1.pdf"
    assert "홍길동" not in saved


def test_stored_file_is_owner_only(store):
    from cvtool.fsutil import is_world_readable

    store.store_file("CV-1", "a.pdf", b"data")
    assert not is_world_readable(store.files_dir / "CV-1.pdf")


def test_store_file_rejects_unknown_suffix(store):
    """알 수 없는 확장자는 붙이지 않는다 (실행파일 등 방지)."""
    assert store.store_file("CV-1", "evil.exe", b"MZ") == "CV-1"


def test_file_path_roundtrip(store):
    saved = store.store_file("CV-1", "a.pdf", b"content")
    store.save(_rec("CV-1"), 저장_파일명=saved)
    assert store.file_path("CV-1").read_bytes() == b"content"


def test_file_path_none_when_not_stored(store):
    store.save(_rec("CV-1"))
    assert store.file_path("CV-1") is None


def test_delete_removes_original_file(store):
    """DB 행만 지우고 파일이 남으면 개인정보가 그대로 남는다."""
    saved = store.store_file("CV-1", "a.pdf", b"x")
    store.save(_rec("CV-1"), 저장_파일명=saved)
    path = store.files_dir / saved
    assert path.exists()

    store.delete("CV-1")
    assert not path.exists()


def test_delete_many_removes_files(store):
    for cid in ("CV-1", "CV-2"):
        store.save(_rec(cid), 저장_파일명=store.store_file(cid, "a.pdf", b"x"))
    store.delete_many(["CV-1", "CV-2"])
    assert list(store.files_dir.iterdir()) == []


def test_delete_all_removes_files(store):
    store.save(_rec("CV-1"), 저장_파일명=store.store_file("CV-1", "a.pdf", b"x"))
    store.delete_all()
    assert list(store.files_dir.iterdir()) == []


def test_purge_expired_removes_files(store):
    """보관기간이 지나면 원본까지 사라져야 한다."""
    store.save(_rec("CV-1"), 저장_파일명=store.store_file("CV-1", "a.pdf", b"x"))
    store._conn.execute("UPDATE candidates SET 보관_만료일='2000-01-01'")
    store._conn.commit()
    store.purge_expired()
    assert list(store.files_dir.iterdir()) == []


def test_orphan_files_detected(store):
    """DB 에 행이 없는 원본은 고아로 잡혀야 한다."""
    store.store_file("CV-GHOST", "a.pdf", b"x")  # save 하지 않음
    store.save(_rec("CV-OK"), 저장_파일명=store.store_file("CV-OK", "a.pdf", b"y"))
    orphans = [f.name for f in store.orphan_files()]
    assert orphans == ["CV-GHOST.pdf"]


def test_save_keeps_existing_file_on_reanalyze(store):
    """재분석 때 저장_파일명을 넘기지 않아도 원본 연결이 끊기면 안 된다."""
    saved = store.store_file("CV-1", "a.pdf", b"x")
    store.save(_rec("CV-1", 한글_이름="1차"), 저장_파일명=saved)
    store.save(_rec("CV-1", 한글_이름="재분석"))  # 저장_파일명 생략
    assert store.file_path("CV-1") is not None
    assert store.get("CV-1").한글_이름 == "재분석"
