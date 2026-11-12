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
    # 전국 지도를 점검할 수 있게, 계산 대상 지역 밖에도 가상의 특보를 몇 건 둔다
    for type_name, level_name, parent, names in _DEMO_NATIONAL:
        for name in names:
            for code in table.resolve(name, parent):
                raws.append(RawWarning("active", type_name, level_name, table.name(code), parent, code))
    return raws


_DEMO_NATIONAL = [
    ("호우", "경보", "경상남도", ["하동", "남해", "사천"]),
    ("호우", "주의보", "경상남도", ["진주", "통영", "거제", "산청"]),
    ("강풍", "주의보", None, ["부산", "울산", "제주도"]),
    ("건조", "주의보", "강원도", ["강릉평지", "동해평지", "삼척평지", "속초평지"]),
]


def live_doc(cfg: Config, now: datetime, forecasts: dict[str, dict[str, ModelSeries]], official: dict[str, RegionOfficial], national: list) -> dict:
    """가상의 실시간 자료(live.json): 지금 날씨와 앞으로 6시간은 가상 예측값의 가까운 시각에서 가져온다."""
    from . import live, storage
    from .timeutil import iso

    status = {"ok": True, "provider": "데모"}
    warnings = storage.build_national(national, cfg, now, status)
    warnings.update({"checked_at": iso(now), "error": None})
    hour = floor_hour(now)
    regions: dict[str, dict] = {}
    for region in cfg.regions:
        entry: dict = {"official": storage.region_official_dict(official[region.id]), "hz": live.hazard_official(official[region.id], cfg)}
        series = forecasts.get("ecmwf_ifs025", {}).get(region.id)
        if series is not None:
            at = {t: i for i, t in enumerate(series.times)}

            def value(name: str, t: datetime) -> float | None:
                column = series.values.get(name)
                return column[at[t]] if column is not None and t in at else None

            rain = value("precip", hour) or 0.0
            entry["now"] = {"temp": value("temp", hour), "rain": rain, "wind": value("wind", hour), "wdir": 200, "rh": int(value("rh", hour) or 0), "pty": 1 if rain >= 0.1 else 0}
            rows = []
            for k in range(1, 7):
                t = hour + k * HOUR
                mm = value("precip", t) or 0.0
                text = None if mm < 0.1 else "1mm 미만" if mm < 1 else "50.0mm 이상" if mm >= 50 else "30.0~50.0mm" if mm >= 30 else f"{mm:.1f}mm"
                rows.append({"t": iso(t), "temp": value("temp", t), "rain": text, "rain_mm": round(mm, 1), "sky": 4 if mm >= 0.1 else 3, "pty": 1 if mm >= 0.1 else 0,
                             "wind": value("wind", t), "wdir": 200, "rh": int(value("rh", t) or 0), "lgt": 0.0})
            entry["fcst"] = rows
        regions[region.id] = entry
    section = {"available": True, "checked_at": iso(now), "provider": "데모", "error": None}
    return {"schema": live.SCHEMA, "generated_at": iso(now), "demo": True, "warnings": warnings,
            "obs": {**section, "base": iso(hour)}, "fcst": {**section, "base": iso(hour - timedelta(minutes=30))}, "regions": regions}


def rail_summary(cfg: Config, now: datetime, days: int = 14) -> dict:
    """가상의 열차 지연 통계(화면 점검용). 비가 온 날에는 전라·경전선 열차가 특보 구간을 지나며 더 늦는다."""
    from . import rail

    settings = cfg.settings.raw.get("rail", {})
    lines = [("경부선", 1, 120, "서울", "부산"), ("호남선", 401, 60, "용산", "목포"), ("전라선", 501, 40, "용산", "여수엑스포"),
             ("경전선", 1941, 14, "순천", "부전"), ("목포보성선", 1971, 8, "목포", "순천"), ("중앙선", 1601, 30, "청량리", "안동"), ("경춘선", 2001, 36, "용산", "춘천")]
    summaries = []
    for back in range(days, 0, -1):
        day = now - timedelta(days=back)
        ymd = day.strftime("%Y%m%d")
        rng = random.Random(f"rail|{ymd}")
        rainy = back in (1, 8)
        runs = []
        for line, first_no, count, origin, dest in lines:
            for i in range(count):
                dep = day.replace(hour=5, minute=0) + timedelta(minutes=int(i * 1000 / count))
                arr = dep + timedelta(minutes=150)
                warned = rainy and line in ("전라선", "경전선", "목포보성선")
                delay = max(-3, int(rng.gauss(14 if warned else 2, 9 if warned else 3)))
                run = rail.TrainRun(ymd=ymd, trn_no=f"{first_no + i:05d}", line=line, direction="D", origin=origin, dest=dest, plan_dep=dep, plan_arr=arr)
                run.stops = [rail.Stop("a", origin, rail.STOP_ORIGIN, None, dep), rail.Stop("b", dest, rail.STOP_TERMINUS, arr + timedelta(minutes=delay), None)]
                if warned:
                    run.warnings = {"호우": "경보" if i % 3 == 0 else "주의보"}
                runs.append(run)
        summaries.append(rail.summarize_day(ymd, runs, settings.get("train_classes", []), now))
    summary = rail.aggregate(summaries)
    latest = summaries[-1]
    idx = {name: i for i, name in enumerate(latest["fields"])}
    top = sorted(latest["trains"], key=lambda t: -(t[idx["arr_delay"]] or 0))[:15]
    summary["latest_day"] = {
        "date": latest["date"], "counts": latest["counts"], "all": latest["all"], "clear": latest["clear"],
        "by_warning": latest["by_warning"], "by_class": latest["by_class"], "by_line": latest["by_line"],
        "top_delayed": [{name: t[idx[name]] for name in ("trn_no", "line", "class", "origin", "dest", "plan_arr", "arr_delay", "warnings")} for t in top if (t[idx["arr_delay"]] or 0) > 0],
    }
    summary["late_minutes"] = settings.get("late_minutes", [5, 10, 20])
    summary["updated_at"] = now.isoformat(timespec="minutes")
    summary["demo"] = True
    return summary

