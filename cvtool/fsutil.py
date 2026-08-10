"""파일 권한 유틸.

지원자 DB 에는 이름·연락처·이메일이 들어간다. 공용 워크스테이션에서 기본 umask
(보통 0022)로 만들면 -rw-r--r-- 가 되어 **다른 계정이 그대로 읽는다.**
웹에 로그인을 걸어도 파일이 열려 있으면 접근 통제가 의미가 없다.

디렉터리는 0700, 파일은 0600 으로 강제한다.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

DIR_MODE = 0o700  # 소유자만 접근
FILE_MODE = 0o600  # 소유자만 읽기/쓰기


def secure_dir(path: Path) -> Path:
    """디렉터리를 만들고 소유자 전용으로 권한을 조인다."""
    path.mkdir(parents=True, exist_ok=True)
    _chmod(path, DIR_MODE)
    return path


def secure_file(path: Path) -> Path:
    """이미 있는 파일의 권한을 소유자 전용으로 조인다."""
    if path.exists():
        _chmod(path, FILE_MODE)
    return path


def _chmod(path: Path, mode: int) -> None:
    try:
        if stat.S_IMODE(path.stat().st_mode) != mode:
            os.chmod(path, mode)
    except OSError:
        # 권한 변경이 안 되는 파일시스템(NFS 등)에서도 동작은 계속해야 한다.
        pass


def safe_filename(name: str) -> str:
    """업로드된 파일명을 디렉터리 성분 없는 단일 이름으로 정리한다.

    POSIX 에서 백슬래시는 경로 구분자가 아니라 파일명 문자다. 그래서
    "..\\..\\x" 같은 윈도우식 이름은 잘리지 않고 통째로 남는다. 탈출은
    안 되지만 지저분하므로 둘 다 구분자로 보고 마지막 성분만 취한다.
    """
    candidate = name.replace("\\", "/").split("/")[-1]
    candidate = candidate.replace("\x00", "").strip()
    candidate = candidate.lstrip(".")  # "..", 숨김 파일 방지
    return candidate or "upload"


def mode_of(path: Path) -> int:
    """현재 권한 비트 (테스트/진단용)."""
    return stat.S_IMODE(path.stat().st_mode)


def is_world_readable(path: Path) -> bool:
    return bool(mode_of(path) & (stat.S_IROTH | stat.S_IRGRP))
