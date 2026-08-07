"""시간 유틸.

서버 시스템 시계는 UTC 입니다. `datetime.now()` 를 그냥 쓰면 9시간 어긋납니다.
반드시 이 모듈을 통해 KST(Asia/Seoul)를 명시하세요. (기존 RAG 시스템에서 실제로 난 버그)
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import settings

KST = ZoneInfo(settings.timezone)


def now_kst() -> datetime:
    """현재 시각을 KST tz-aware 로 반환."""
    return datetime.now(KST)


def now_utc() -> datetime:
    """현재 시각을 UTC tz-aware 로 반환 (DB 저장용 등)."""
    return datetime.now(timezone.utc)


def to_kst(dt: datetime) -> datetime:
    """임의의 datetime 을 KST 로 변환. naive 는 UTC 로 가정한다."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST)
