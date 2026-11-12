"""시각 처리 도우미. 모든 내부 시각은 한국표준시(KST, UTC+9) 기준의 aware datetime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")


def now_kst() -> datetime:
    return datetime.now(KST)


def to_kst(dt: datetime) -> datetime:
    """naive 값은 KST로 간주하고, aware 값은 KST로 변환한다."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def parse_iso(text: str) -> datetime:
    """ISO 8601 문자열을 KST aware datetime으로 읽는다. 오프셋이 없으면 KST로 본다."""
    return to_kst(datetime.fromisoformat(text))


def iso(dt: datetime) -> str:
    """분 단위 ISO 8601 (예: 2026-11-12T21:00+09:00)."""
    return to_kst(dt).isoformat(timespec="minutes")


def floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def parse_compact(text: str) -> datetime | None:
    """기상청 표기(YYYYMMDDHHMM 또는 YYYYMMDDHH)를 KST datetime으로 읽는다. 빈 값은 None."""
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    if len(digits) >= 12:
        fmt, digits = "%Y%m%d%H%M", digits[:12]
    elif len(digits) == 10:
        fmt = "%Y%m%d%H"
    elif len(digits) == 8:
        fmt = "%Y%m%d"
    else:
        return None
    try:
        return datetime.strptime(digits, fmt).replace(tzinfo=KST)
    except ValueError:
        return None
