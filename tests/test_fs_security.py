"""파일 권한 / 경로 안전성 테스트.

지원자 DB 에는 이름·연락처·이메일이 들어간다. 기본 umask 로 만들면
-rw-r--r-- 가 되어 서버의 다른 계정이 그대로 읽는다. 웹에 로그인을 걸어도
파일이 열려 있으면 접근 통제가 의미가 없다.
"""

from __future__ import annotations

import stat
from pathlib import Path

from cvtool.fsutil import (
    DIR_MODE,
    FILE_MODE,
    is_world_readable,
    mode_of,
    safe_filename,
    secure_dir,
    secure_file,
)
from cvtool.store import CandidateStore
from cvtool.venues import VenueRegistry


def test_secure_dir_is_owner_only(tmp_path):
    d = secure_dir(tmp_path / "data")
    assert mode_of(d) == DIR_MODE
    assert not is_world_readable(d)


def test_secure_dir_tightens_existing_loose_dir(tmp_path):
    d = tmp_path / "loose"
    d.mkdir(mode=0o755)
    assert is_world_readable(d)
    secure_dir(d)
    assert mode_of(d) == DIR_MODE


def test_secure_file_is_owner_only(tmp_path):
    f = tmp_path / "x.db"
    f.write_text("비밀", encoding="utf-8")
    f.chmod(0o644)
    assert is_world_readable(f)
    secure_file(f)
    assert mode_of(f) == FILE_MODE
    assert not is_world_readable(f)


def test_secure_file_missing_is_noop(tmp_path):
    secure_file(tmp_path / "없는파일")  # 예외가 나면 안 된다


def test_candidate_db_is_not_world_readable(tmp_path):
    """개인정보 DB 가 다른 계정에 열려 있으면 안 된다."""
    store = CandidateStore(tmp_path / "sub" / "candidates.db")
    assert not is_world_readable(store.path), "candidates.db 를 다른 계정이 읽을 수 있다"
    assert not is_world_readable(store.path.parent)


def test_venue_db_is_not_world_readable(tmp_path):
    reg = VenueRegistry(tmp_path / "sub" / "venues.db")
    assert not is_world_readable(reg.path)


def test_parent_dir_created_with_tight_mode(tmp_path):
    CandidateStore(tmp_path / "새폴더" / "c.db")
    assert mode_of(tmp_path / "새폴더") == DIR_MODE


# --- 업로드 파일명 안전성 ----------------------------------------------------
EVIL_NAMES = [
    "../../etc/passwd",
    "/etc/passwd",
    "..\\..\\windows\\system32",
    "..",
    "...",
    "",
    "sub/dir/이력서.pdf",
]


def test_upload_never_escapes_incoming_dir(tmp_path):
    """핵심 성질: 어떤 파일명이 와도 목적지가 incoming 안에 머물러야 한다."""
    incoming = secure_dir(tmp_path / "incoming")
    for evil in EVIL_NAMES:
        dest = (incoming / safe_filename(evil)).resolve()
        assert dest.parent == incoming.resolve(), f"{evil!r} 이 디렉터리를 벗어났다"


def test_safe_filename_has_no_separators():
    for evil in EVIL_NAMES:
        safe = safe_filename(evil)
        assert "/" not in safe and "\\" not in safe
        assert safe and not safe.startswith(".")


def test_safe_filename_keeps_normal_names():
    assert safe_filename("이력서_홍길동.pdf") == "이력서_홍길동.pdf"
    assert safe_filename("CV (final) v2.docx") == "CV (final) v2.docx"


def test_safe_filename_falls_back_when_empty():
    assert safe_filename("") == "upload"
    assert safe_filename("..") == "upload"


def test_dir_mode_constant_is_owner_only():
    assert not DIR_MODE & (stat.S_IRWXG | stat.S_IRWXO)
    assert not FILE_MODE & (stat.S_IRWXG | stat.S_IRWXO)
