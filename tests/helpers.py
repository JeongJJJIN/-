"""테스트 공용 도우미."""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
from typing import Callable, Optional

from teukbo.assess import RegionOfficial
from teukbo.config import Config, Region, load_config
from teukbo.indicators import HOUR, ModelSeries
from teukbo.timeutil import KST


@lru_cache(maxsize=1)
def config() -> Config:
    """저장소의 실제 설정(config/*.json)."""
    return load_config()


@lru_cache(maxsize=1)
def small_config() -> Config:
    """손으로 다듬은 지역(전남 23곳)만 담은 설정 — 수집·계산 전체를 돌리는 테스트가 전국 226곳을 매번 계산하지 않게."""
    import dataclasses

    full = config()
    return dataclasses.replace(full, regions=tuple(r for r in full.regions if not r.id.startswith("L")))


def kst(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=KST)


def region(region_id: str = "suncheon") -> Region:
    return next(r for r in config().regions if r.id == region_id)


def series(
    model_id: str,
    now: datetime,
    *,
    past: int = 36,
    future: int = 72,
    **variables: Callable[[datetime, float], Optional[float]],
) -> ModelSeries:
    """now 기준 −past ~ +future 시간의 시간별 자료. 변수마다 f(시각, now 로부터의 시간) → 값."""
    start = now.replace(minute=0, second=0, microsecond=0)
    times = [start + k * HOUR for k in range(-past, future + 1)]
    values = {name: [fn(t, (t - start) / timedelta(hours=1)) for t in times] for name, fn in variables.items()}
    return ModelSeries(model_id=model_id, times=times, values=values)


def burst(amount: float, start_lead: int, hours: int) -> Callable[[datetime, float], float]:
    """start_lead 시간 뒤부터 hours 시간 동안 매시간 amount, 나머지는 0."""
    return lambda _t, lead: amount if start_lead <= lead < start_lead + hours else 0.0


def const(value: Optional[float]) -> Callable[[datetime, float], Optional[float]]:
    return lambda _t, _lead: value


def no_official(available: bool = True) -> RegionOfficial:
    return RegionOfficial(available=available)
