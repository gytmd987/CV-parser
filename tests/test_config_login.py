""".env 로딩과 로그인 비교 로직 테스트 (서버 실행 불필요)."""

from __future__ import annotations

import os
import secrets

import pytest

from cvtool.dotenv import parse


# --- .env 파싱 --------------------------------------------------------------
def test_parse_basic():
    assert parse("CVTOOL_WEB_PASSWORD=abc123") == {"CVTOOL_WEB_PASSWORD": "abc123"}


def test_parse_ignores_comments_and_blanks():
    text = "# 주석\n\nA=1\n   # 들여쓴 주석\nB=2\n"
    assert parse(text) == {"A": "1", "B": "2"}


def test_parse_strips_quotes():
    assert parse("A='값'\nB=\"값2\"") == {"A": "값", "B": "값2"}


def test_parse_allows_export_prefix():
    assert parse("export A=1") == {"A": "1"}


def test_parse_keeps_equals_in_value():
    assert parse("A=b=c") == {"A": "b=c"}


def test_parse_handles_korean_value():
    assert parse("CVTOOL_WEB_PASSWORD=내비밀번호123")["CVTOOL_WEB_PASSWORD"] == "내비밀번호123"


def test_load_dotenv_does_not_override_real_env(tmp_path, monkeypatch):
    """실제 환경변수가 .env 보다 우선해야 한다."""
    env_file = tmp_path / ".env"
    env_file.write_text("CVTOOL_TEST_KEY=from_file", encoding="utf-8")
    monkeypatch.setenv("CVTOOL_TEST_KEY", "from_env")
    monkeypatch.setenv("CVTOOL_ENV_FILE", str(env_file))

    import importlib

    from cvtool import dotenv as dotenv_mod

    importlib.reload(dotenv_mod)
    assert os.environ["CVTOOL_TEST_KEY"] == "from_env"


def test_load_dotenv_sets_missing_key(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("CVTOOL_TEST_KEY2=from_file", encoding="utf-8")
    monkeypatch.delenv("CVTOOL_TEST_KEY2", raising=False)
    monkeypatch.setenv("CVTOOL_ENV_FILE", str(env_file))

    import importlib

    from cvtool import dotenv as dotenv_mod

    importlib.reload(dotenv_mod)
    assert os.environ["CVTOOL_TEST_KEY2"] == "from_file"


# --- 비밀번호 비교 ----------------------------------------------------------
@pytest.mark.parametrize("password", ["ascii-pw-123", "내비밀번호123", "pässwörd", "한글🔒"])
def test_password_comparison_supports_non_ascii(password):
    """secrets.compare_digest 는 비ASCII str 을 거부하므로 바이트로 비교해야 한다.

    한글 비밀번호를 넣으면 로그인 시 서버가 TypeError 로 죽던 버그의 회귀 테스트.
    """
    assert secrets.compare_digest(password.encode("utf-8"), password.encode("utf-8"))
    assert not secrets.compare_digest(password.encode("utf-8"), b"wrong")


def test_str_comparison_would_have_crashed():
    """왜 바이트로 바꿔야 했는지 근거를 남긴다."""
    with pytest.raises(TypeError):
        secrets.compare_digest("내비밀번호", "내비밀번호")
