"""시간대/리텐션 로직 테스트 (순수 로직, 서비스 불필요)."""

from __future__ import annotations

from datetime import datetime, timezone

from cvtool.retention import expiry_date, is_expired
from cvtool.timeutil import KST, now_kst, to_kst


def test_now_kst_is_aware_and_seoul():
    dt = now_kst()
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 9 * 3600  # KST = UTC+9


def test_to_kst_from_naive_utc():
    naive = datetime(2026, 1, 1, 0, 0, 0)  # UTC 자정으로 간주
    kst = to_kst(naive)
    assert kst.hour == 9  # +9시간
    assert kst.tzinfo == KST


def test_expiry_date_adds_months():
    closed = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
    exp = expiry_date(closed, retention_months=6)
    assert (exp.year, exp.month, exp.day) == (2026, 7, 15)


def test_expiry_date_month_overflow_to_year():
    closed = datetime(2026, 10, 31, tzinfo=timezone.utc)
    exp = expiry_date(closed, retention_months=4)  # 10월 + 4 = 다음해 2월
    assert (exp.year, exp.month) == (2027, 2)
    assert exp.day == 28  # 2월 말일 보정


def test_is_expired_boundaries():
    closed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    before = datetime(2026, 6, 1, tzinfo=timezone.utc)
    after = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert is_expired(closed, now=after, retention_months=6) is True
    assert is_expired(closed, now=before, retention_months=6) is False
