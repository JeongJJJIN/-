"""데모(가상) 자료 생성기 — 네트워크 없이 화면과 계산을 점검하기 위한 것.

실제 예보가 아니다. `python -m teukbo.run --demo` 로만 쓰이며, 결과에는 demo=true 가 표시되고
실제 이력(data/history)에는 절대 기록되지 않는다(별도 폴더 data-demo 사용).

시나리오: 남해상을 지나는 저기압으로 전남 동부·남해안에 강한 비와 바람.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from .assess import OfficialWarning, RegionOfficial
from .config import Config, Region
from .indicators import HOUR, ModelSeries
from .timeutil import floor_hour

# 비구름의 중심(순천만 부근)과 영향 반경(도 단위)
_CENTER = (34.85, 127.45)
_RADIUS = 0.75

# 모델별 성향: (강수 배율, 시작 시각 편차 h, 바람 배율)
_MODEL_BIAS = {
    "kma_dfs": (1.05, 0, 1.00),
    "ecmwf_ifs025": (0.80, 1, 0.92),
    "ncep_gfs_seamless": (1.25, -2, 1.10),
    "dwd_icon_seamless": (0.95, 2, 1.00),
    "jma_seamless": (1.10, -1, 1.05),
}


def _exposure(region: Region) -> float:
    """비구름 중심에 가까울수록 1, 멀수록 0."""
    d = math.hypot(region.lat - _CENTER[0], (region.lon - _CENTER[1]) * 0.82)
    return math.exp(-((d / _RADIUS) ** 2))


def _pulse(hours_from_peak: float, width: float) -> float:
    return math.exp(-((hours_from_peak / width) ** 2))


def make_series(region: Region, model_id: str, now: datetime, events: list[datetime], cfg: Config) -> ModelSeries:
    st = cfg.settings
    rain_k, shift_h, wind_k = _MODEL_BIAS.get(model_id, (1.0, 0, 1.0))
    start = floor_hour(now)
    # 같은 (지역, 모델, 회차)에는 항상 같은 값이 나오게 한다
    rng = random.Random(f"{region.id}|{model_id}|{start.isoformat()}")
    run_noise = 1.0 + rng.uniform(-0.12, 0.12)
    exposure = _exposure(region)
    is_dfs = model_id == "kma_dfs"
    first = start + HOUR if is_dfs else start - st.past_hours * HOUR
    count = st.forecast_hours - 1 if is_dfs else st.past_hours + st.forecast_hours

    times = [first + k * HOUR for k in range(count)]
    precip, wind, gust, temp, rh, snow = [], [], [], [], [], []
    for t in times:
        rain = 0.0
        blow = 0.0
        for ev in events:
            dt = (t - (ev + shift_h * HOUR)).total_seconds() / 3600.0
            rain += 34.0 * exposure * rain_k * run_noise * _pulse(dt, 3.2)
            blow += 11.0 * (0.35 + 0.65 * exposure) * wind_k * _pulse(dt + 1.0, 6.0)
        rain *= 1.0 + rng.uniform(-0.15, 0.15)
        precip.append(round(rain, 1) if rain >= 0.1 else 0.0)
        speed = 3.2 + blow + rng.uniform(-0.6, 0.6)
        wind.append(round(max(0.3, speed), 1))
        gust.append(round(max(0.5, speed * 1.55 + rng.uniform(-0.8, 0.8)), 1))
        hour = t.hour + t.minute / 60.0
        base = 11.5 + 4.5 * math.sin((hour - 9.0) / 24.0 * 2.0 * math.pi) - 0.08 * rain
        temp.append(round(base + rng.uniform(-0.4, 0.4), 1))
        rh.append(round(min(100.0, 62.0 + 9.0 * math.log1p(rain) + rng.uniform(-4, 4)), 0))
        snow.append(0.0)

    values: dict[str, list[float | None]] = {"precip": precip, "wind": wind, "temp": temp, "rh": rh, "snow": snow}
    if model_id not in ("kma_dfs", "jma_seamless"):  # 실제로도 돌풍 자료가 없는 출처
        values["gust"] = gust
    return ModelSeries(model_id=model_id, times=times, values=values)


def make_official(region: Region, now: datetime, events: list[datetime], cfg: Config) -> RegionOfficial:
    """가상의 기상청 발표: 비 시작 약 20시간 전 예비특보, 3시간 전부터 주의보(가까운 곳은 경보)."""
    exposure = _exposure(region)
    official = RegionOfficial(available=True)
    zone = region.zone_names[0] if region.zone_names else region.name
    th = cfg.thresholds
    for ev in events:
        effective = ev - timedelta(hours=3)
        ends = ev + timedelta(hours=7)
        if exposure >= 0.45 and effective <= now < ends:
            level = "warning" if exposure >= 0.9 and now >= ev - HOUR else "advisory"
            official.active.append(
                OfficialWarning(
                    status="active",
                    type="heavy_rain",
                    type_name=th.type_name("heavy_rain"),
                    level=level,
                    level_name=th.level_name(level),
                    zone_code=region.zone_codes[0] if region.zone_codes else None,
                    zone_name=zone,
                    effective_from=effective if level == "advisory" else ev - HOUR,
                    issued_at=effective - timedelta(hours=1),
                )
            )
        elif exposure >= 0.45 and ev - timedelta(hours=20) <= now < effective:
            official.preliminary.append(
                OfficialWarning(
                    status="preliminary",
                    type="heavy_rain",
                    type_name=th.type_name("heavy_rain"),
                    level=None,
                    level_name="예비",
                    zone_code=region.zone_codes[0] if region.zone_codes else None,
                    zone_name=zone,
                    issued_at=ev - timedelta(hours=20),
                    expected_text=f"{effective.day}일 {_part_of_day(effective.hour)}",
                )
            )
    return official


def _part_of_day(hour: int) -> str:
    if hour < 6:
        return "새벽(00~06시)"
    if hour < 12:
        return "오전(06~12시)"
    if hour < 18:
        return "오후(12~18시)"
    return "밤(18~24시)"


def generate(cfg: Config, now: datetime, events: list[datetime]) -> tuple[dict[str, dict[str, ModelSeries]], dict[str, RegionOfficial]]:
    """반환: (모델 id → 지역 id → ModelSeries, 지역 id → RegionOfficial)"""
    data: dict[str, dict[str, ModelSeries]] = {}
    for model in cfg.settings.models:
        data[model.id] = {r.id: make_series(r, model.id, now, events, cfg) for r in cfg.regions}
    official = {r.id: make_official(r, now, events, cfg) for r in cfg.regions}
    return data, official


def national_raws(cfg: Config, official: dict[str, RegionOfficial]) -> list:
    """가상의 지역별 특보를 전국 특보 목록(말단 구역 코드 단위) 형태로 바꾼다."""
    from .sources.kma_warnings import RawWarning
    from .zones import load_zone_table

    table = load_zone_table()
    raws: list = []
    seen: set[tuple[str, str, str, str]] = set()
    for region in cfg.regions:
        for w in official[region.id].active + official[region.id].preliminary:
            level_name = w.level_name if w.status == "active" else ""
            for code in region.zone_codes:
                if table.leaves(code) != [code] or (w.status, w.type_name, level_name, code) in seen:
                    continue
                seen.add((w.status, w.type_name, level_name, code))
                raws.append(RawWarning(w.status, w.type_name, level_name, table.name(code), None, code, w.effective_from, w.issued_at, w.expected_text))
    return raws

