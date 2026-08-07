"""개인정보 보관 기간 정책 (순수 로직).

채용 종료 후 N개월이 지난 CV 는 삭제 대상이다. 실제 DB 삭제는 저장 슬라이스에서
이 판정 로직을 사용해 수행한다. 여기서는 KST 기준의 만료 판정만 담당한다(테스트 가능).
"""

from __future__ import annotations

from datetime import datetime

from .config import settings
from .timeutil import now_kst, to_kst


def expiry_date(closed_at: datetime, retention_months: int | None = None) -> datetime:
    """채용 종료 시각(closed_at) 기준 만료 시각(KST)을 계산한다."""
    months = settings.retention_months if retention_months is None else retention_months
    base = to_kst(closed_at)
    # 월 단위 가산 (일 고정, 말일 넘침은 그 달 말일로 보정)
    total = base.month - 1 + months
    year = base.year + total // 12
    month = total % 12 + 1
    day = min(base.day, _days_in_month(year, month))
    return base.replace(year=year, month=month, day=day)


def is_expired(
    closed_at: datetime,
    *,
    now: datetime | None = None,
    retention_months: int | None = None,
) -> bool:
    """지금(KST) 기준으로 보관 기간이 지났는지 판정."""
    ref = to_kst(now) if now is not None else now_kst()
    return ref >= expiry_date(closed_at, retention_months)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    from datetime import date

    first_next = date(year + (month // 12), (month % 12) + 1, 1)
    from datetime import timedelta

    return (first_next - timedelta(days=1)).day
