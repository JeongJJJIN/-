"""과거 검증(백테스트): 지난 예보로 가능성을 다시 계산해 실제 특보 기록과 맞춰 본다.

    python3 -m teukbo.backtest --out data/backtest.json

무엇을 하나
1. 실제 특보 기록: data/rail/events 에 쌓아 둔 기상청 특보 발표·해제 이력(전국, 구역별 발효 구간).
2. 그때의 예보: Open-Meteo Previous Runs API 에서 '그 시각의 값을 하루 전·이틀 전에 예측한 값'을 받는다.
3. 다시 계산: 6시간마다 그 시점으로 돌아가(재현 시점), 그때 볼 수 있었던 예보만으로 지금과 똑같은 계산식
   (teukbo.assess)을 돌려 지역별 가능성을 낸다.
4. 맞춰 보기: 적중률 집계(teukbo.accuracy)를 그대로 써서 사전 포착률·오경보율을 내고,
   가능성 구간별로 실제 특보가 뒤따른 비율(보정표)을 만든다.

정직하게 적어 둘 조건
- 미래를 훔쳐보지 않는다: 재현 시점 T 에서, T+24시간까지는 '하루 전에 예측한 값', 그 뒤 T+48시간까지는
  '이틀 전에 예측한 값'만 쓴다. 실제 서비스는 이보다 새 예보(0~48시간 전)를 쓰므로, 이 검증은 실제보다 불리한 조건이다.
- 기상청 단기예보는 과거분을 받을 수 없어 해외 수치모델(ECMWF·GFS·ICON·JMA)만으로 계산했다. 실제 서비스는 단기예보까지 5개를 쓴다.
- 공식 예비특보로 등급을 올리는 부분은 넣지 않았다(계산식 자체의 성능만 본다).
- 표본은 서로 독립이 아니다(한 번의 비·바람이 여러 지역·여러 시점에 걸친다). 그래서 특보가 있었던 '날 수'도 함께 적는다.
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing
import os
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import accuracy, rail, rail_run, storage
from .assess import OfficialWarning, RegionOfficial, assess_region
from .config import ROOT, Config, ModelDef, Region, load_config
from .http import FetchError, get_json
from .indicators import ModelSeries
from .sources.open_meteo import HOURLY_VARS
from .timeutil import KST, iso, now_kst, parse_iso

log = logging.getLogger("teukbo.backtest")

ENDPOINT = "https://previous-runs-api.open-meteo.com/v1/forecast"
LEADS = (1, 2)  # 하루 전·이틀 전에 예측한 값
BUCKETS = ((0, 0, "0%"), (1, 29, "1~29% (낮음)"), (30, 59, "30~59% (주의)"), (60, 100, "60~100% (높음)"))
STEP_HOURS = 6
REPLAY_HOURS = (3, 9, 15, 21)  # 재현 시점(한국 시각)


# ---------------------------------------------------------------- 과거 예보 받기


def build_params(regions: Sequence[Region], model_id: str, past_days: int) -> dict[str, Any]:
    return {
        "latitude": ",".join(f"{r.lat:.4f}" for r in regions),
        "longitude": ",".join(f"{r.lon:.4f}" for r in regions),
        "hourly": ",".join(f"{name}_previous_day{lead}" for name in HOURLY_VARS for lead in LEADS),
        "models": model_id,
        "wind_speed_unit": "ms",
        "timezone": "Asia/Seoul",
        "past_days": past_days,
        "forecast_days": 1,
    }


def parse_archive(payload: Any, regions: Sequence[Region], model_id: str) -> dict[str, dict[int, ModelSeries]]:
    """응답 → 지역 id → {1: 하루 전 예측 시계열, 2: 이틀 전 예측 시계열}. 값이 하나도 없는 선행은 뺀다."""
    if isinstance(payload, dict):
        if payload.get("error"):
            raise FetchError(f"open-meteo 과거 예보[{model_id}]: {payload.get('reason', '알 수 없는 오류')}")
        payload = [payload]
    if not isinstance(payload, list) or len(payload) != len(regions):
        raise FetchError(f"open-meteo 과거 예보[{model_id}]: 응답 지점 수가 요청과 다릅니다")
    out: dict[str, dict[int, ModelSeries]] = {}
    for region, loc in zip(regions, payload):
        hourly = (loc or {}).get("hourly") or {}
        raw_times = hourly.get("time") or []
        if not raw_times:
            continue
        times = [datetime.fromisoformat(t).replace(tzinfo=KST) for t in raw_times]
        leads: dict[int, ModelSeries] = {}
        for lead in LEADS:
            values: dict[str, list[float | None]] = {}
            for om_name, std_name in HOURLY_VARS.items():
                key = f"{om_name}_previous_day{lead}"
                series = hourly.get(f"{key}_{model_id}", hourly.get(key))
                if isinstance(series, list) and len(series) == len(times) and any(v is not None for v in series):
                    values[std_name] = [float(v) if v is not None else None for v in series]
            if values:
                leads[lead] = ModelSeries(model_id=model_id, times=times, values=values)
        if leads:
            out[region.id] = leads
    return out


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def fetch_archive(regions: Sequence[Region], model: ModelDef, past_days: int, http_opts: Mapping[str, Any] | None = None,
                  chunk: int = 50, pause: float = 30.0) -> dict[str, dict[int, ModelSeries]]:
    """한 모델의 과거 예보를 전 지역에 대해 받는다. 지점을 chunk 곳씩 나눠 pause 초 간격으로(무료 API 한도).
    일부만 실패하면 받은 지역만 돌려주고, 한도 초과(429)를 만나면 남은 묶음은 보내지 않는다."""
    opts = http_opts or {}
    out: dict[str, dict[int, ModelSeries]] = {}
    errors: list[str] = []
    parts = [regions[i:i + chunk] for i in range(0, len(regions), chunk)]
    for index, part in enumerate(parts):
        if index and pause > 0:
            _pause(pause)
        try:
            payload = get_json(ENDPOINT, build_params(part, model.id, past_days), timeout=float(opts.get("timeout_seconds", 30)) * 3,
                               retries=int(opts.get("retries", 3)), backoff=float(opts.get("backoff_seconds", 3)), label=f"open-meteo-past:{model.id}")
            out.update(parse_archive(payload, part, model.id))
        except FetchError as e:
            errors.append(str(e))
            if getattr(e, "status", None) == 429:
                break
    if not out:
        raise FetchError(errors[0] if errors else f"open-meteo 과거 예보[{model.id}]: 자료 없음")
    if errors:
        log.warning("과거 예보 %s: 일부 지역만 받았습니다(%d/%d곳) — %s", model.id, len(out), len(regions), errors[0])
    return out


# ---------------------------------------------------------------- 그 시점에 볼 수 있었던 예보


def stitch(leads: Mapping[int, ModelSeries], issue: datetime, past_hours: int, horizon_hours: int) -> ModelSeries | None:
    """재현 시점 issue 에 이미 나와 있던 예보만으로 시계열을 잇는다.

    issue+24시간까지는 '하루 전에 예측한 값'(늦어도 issue 에 나온 예보), 그 뒤는 '이틀 전에 예측한 값'.
    지난 시각도 하루 전 예측값을 쓴다. 어느 쪽에도 값이 없으면 None.
    """
    day1, day2 = leads.get(1), leads.get(2)
    base = day1 or day2
    if base is None:
        return None
    first = base.times[0]
    lo = max(0, int((issue - timedelta(hours=past_hours) - first).total_seconds() // 3600))
    hi = min(len(base.times) - 1, int((issue + timedelta(hours=horizon_hours) - first).total_seconds() // 3600))
    if hi <= lo:
        return None
    switch = issue + timedelta(hours=24)
    times = base.times[lo:hi + 1]
    values: dict[str, list[float | None]] = {}
    for name in {*(day1.values if day1 else {}), *(day2.values if day2 else {})}:
        near = day1.values.get(name) if day1 else None
        far = day2.values.get(name) if day2 else None
        column: list[float | None] = []
        for i in range(lo, hi + 1):
            source = near if base.times[i] <= switch or far is None else far
            if source is None:
                source = far
            column.append(source[i] if source is not None else None)
        if any(v is not None for v in column):
            values[name] = column
    return ModelSeries(model_id=base.model_id, times=times, values=values) if values else None


# ---------------------------------------------------------------- 실제 특보(지역별 발효 구간)


def region_intervals(cfg: Config, intervals: Sequence[rail.Interval]) -> dict[str, list[tuple[str, str, datetime, datetime | None, str]]]:
    """지역 id → [(특보 종류 id, 단계 id, 시작, 끝, 구역 코드)]. 계산 대상이 아닌 종류(풍랑·건조 등)는 뺀다.

    특보는 말단 구역·시군·도 어느 단위로도 내려진다. 지역은 자기 구역과 그 위의 시·군(zone_codes), 도·광역시(group_codes)의
    코드를 갖고 있으므로, 발표된 구역 코드가 그 가운데 있으면 그 지역의 특보다(실시간 수집의 연결 규칙과 같다).
    """
    th = cfg.thresholds
    type_by_name = {info["name"]: type_id for type_id, info in th.types.items()}
    level_by_name = {info["name"]: level_id for level_id, info in th.levels.items()}
    owners: dict[str, list[str]] = {}
    for region in cfg.regions:
        for code in {*region.zone_codes, *region.group_codes}:
            owners.setdefault(code, []).append(region.id)
    out: dict[str, list[tuple[str, str, datetime, datetime | None, str]]] = {r.id: [] for r in cfg.regions}
    for interval in intervals:
        type_id, level_id = type_by_name.get(interval.type_name), level_by_name.get(interval.level_name)
        if type_id is None or level_id is None:
            continue
        for rid in owners.get(interval.code, []):
            out[rid].append((type_id, level_id, interval.start, interval.end, interval.code))
    return out


def official_at(cfg: Config, rows: Sequence[tuple[str, str, datetime, datetime | None, str]], when: datetime) -> RegionOfficial:
    th = cfg.thresholds
    active = [OfficialWarning("active", t, th.type_name(t), level, th.level_name(level), code, "", start)
              for t, level, start, end, code in rows if start <= when and (end is None or when < end)]
    return RegionOfficial(available=True, active=active)


# ---------------------------------------------------------------- 재현


_SHARED: dict[str, Any] = {}  # 작업 프로세스가 함께 쓰는 자료(fork 로 넘어간다)


def _bucket(p: int) -> int:
    return next(i for i, (lo, hi, _) in enumerate(BUCKETS) if lo <= p <= hi)


def replay_times(first: datetime, last: datetime) -> list[datetime]:
    """재현 시점들: first~last 사이의 03·09·15·21시(한국 시각)."""
    out: list[datetime] = []
    day = first.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= last:
        for hour in REPLAY_HOURS:
            t = day.replace(hour=hour)
            if first <= t <= last:
                out.append(t)
        day += timedelta(days=1)
    return out


def replay_one(issue: datetime) -> tuple[dict[str, Any], dict[str, list[list[int]]], dict[str, dict[int, set[str]]]]:
    """한 시점의 재현: (이력 형식의 회차, 종류별 보정표 [[표본 수, 실제 특보 수] × 구간], 종류별·구간별 실제 특보가 있었던 날)"""
    cfg: Config = _SHARED["cfg"]
    archive: Mapping[str, Mapping[str, Mapping[int, ModelSeries]]] = _SHARED["archive"]
    truth: Mapping[str, Sequence[tuple[str, str, datetime, datetime | None, str]]] = _SHARED["truth"]
    th, st = cfg.thresholds, cfg.settings
    horizon = timedelta(hours=st.horizon_hours)
    assessments = []
    table: dict[str, list[list[int]]] = {}
    days: dict[str, dict[int, set[str]]] = {}
    for region in cfg.regions:
        models = {}
        for model_id, per_region in archive.items():
            series = stitch(per_region.get(region.id) or {}, issue, st.past_hours, st.horizon_hours)
            if series is not None:
                models[model_id] = series
        if not models:
            continue
        rows = truth.get(region.id, [])
        ra = assess_region(region, models, official_at(cfg, rows, issue), cfg, issue)
        assessments.append(ra)
        for a in ra.hazards:
            if a.hazard.method != "model" or a.probability is None:
                continue
            rank = th.level_rank(a.hazard.level)
            same = [(start, end, th.level_rank(level)) for t, level, start, end, _ in rows if t == a.hazard.type]
            if any(start <= issue and (end is None or issue < end) and r >= rank for start, end, r in same):
                continue  # 이미 발효 중인 특보는 '예측'이 아니다
            hits = [start for start, _, r in same if r >= rank and issue < start <= issue + horizon]
            b = _bucket(int(a.probability))
            cell = table.setdefault(a.hazard.type, [[0, 0] for _ in BUCKETS])[b]
            cell[0] += 1
            if hits:
                cell[1] += 1
                days.setdefault(a.hazard.type, {}).setdefault(b, set()).add(min(hits).strftime("%Y-%m-%d"))
    run = storage.build_history_run(assessments, cfg, issue, {"open_meteo": True, "kma_forecast": False, "kma_warnings": True})
    return run, table, days


def replay(cfg: Config, archive: Mapping[str, Any], truth: Mapping[str, Any], times: Sequence[datetime], workers: int = 0) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    _SHARED.update(cfg=cfg, archive=archive, truth=truth)
    workers = workers or min(4, os.cpu_count() or 1)
    if workers > 1 and len(times) > 8 and "fork" in multiprocessing.get_all_start_methods():
        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("fork")) as pool:
            results = list(pool.map(replay_one, times, chunksize=max(1, len(times) // (workers * 4))))
    else:
        results = [replay_one(t) for t in times]
    runs: list[dict[str, Any]] = []
    table: dict[str, list[list[int]]] = {}
    days: dict[str, dict[int, set[str]]] = {}
    for run, part, part_days in results:
        runs.append(run)
        for type_id, cells in part.items():
            total = table.setdefault(type_id, [[0, 0] for _ in BUCKETS])
            for i, (n, hit) in enumerate(cells):
                total[i][0] += n
                total[i][1] += hit
        for type_id, by_bucket in part_days.items():
            for b, names in by_bucket.items():
                days.setdefault(type_id, {}).setdefault(b, set()).update(names)
    return runs, table, days


# ---------------------------------------------------------------- 결과 문서


def _rate(hit: int, total: int) -> float | None:
    return round(hit / total, 3) if total else None


def calibration_rows(cells: Sequence[Sequence[int]], days: Mapping[int, set[str]] | None = None) -> list[dict[str, Any]]:
    return [{"bucket": label, "from": lo, "to": hi, "cases": cells[i][0], "hits": cells[i][1], "rate": _rate(cells[i][1], cells[i][0]), "event_days": len((days or {}).get(i, ()))}
            for i, (lo, hi, label) in enumerate(BUCKETS)]


def build_document(cfg: Config, runs: Sequence[dict[str, Any]], table: Mapping[str, Any], days: Mapping[str, Any], truth: Mapping[str, Any],
                   models: Sequence[str], regions_used: int, now: datetime) -> dict[str, Any]:
    """재현한 회차들을 적중률 집계에 그대로 넣고, 보정표를 붙인다."""
    th = cfg.thresholds
    with tempfile.TemporaryDirectory() as tmp:
        for run in runs:
            storage.append_history(Path(tmp), run, parse_iso(run["t"]))
        last = parse_iso(runs[-1]["t"]) if runs else now
        result = accuracy.compute_accuracy(Path(tmp), cfg, last + timedelta(hours=1))
    total = [[0, 0] for _ in BUCKETS]
    all_days: dict[int, set[str]] = {}
    for type_id, cells in table.items():
        for i, (n, hit) in enumerate(cells):
            total[i][0] += n
            total[i][1] += hit
        for b, names in (days.get(type_id) or {}).items():
            all_days.setdefault(b, set()).update(names)
    first, end = (runs[0]["t"], runs[-1]["t"]) if runs else (None, None)
    event_days = sorted({start.strftime("%Y-%m-%d") for rows in truth.values() for _, _, start, _, _ in rows
                         if first and parse_iso(first) <= start <= parse_iso(end) + timedelta(hours=cfg.settings.horizon_hours)})
    return {
        "schema": 1,
        "updated_at": iso(now),
        "period": {"from": first, "to": end, "replays": len(runs), "step_hours": STEP_HOURS},
        "regions": regions_used,
        "models": list(models),
        "horizon_hours": cfg.settings.horizon_hours,
        "lead_hours": result.get("lead_hours"),
        "events": result.get("events"),
        "event_days": len(event_days),
        "capture": result.get("capture"),
        "false_alarm": result.get("false_alarm"),
        "by_type": {type_id: {**info, "calibration": calibration_rows(table.get(type_id) or [[0, 0] for _ in BUCKETS], days.get(type_id))} for type_id, info in (result.get("by_type") or {}).items()},
        "calibration": calibration_rows(total, all_days),
        "types_without_events": sorted(th.type_name(t) for t in table if t not in (result.get("by_type") or {})),
        "notes": [
            "미래를 훔쳐보지 않게, 재현 시점 기준 24시간까지는 하루 전에 예측한 값, 그 뒤는 이틀 전에 예측한 값만 썼습니다(실제 서비스보다 오래된 예보 — 불리한 조건).",
            "기상청 단기예보는 과거분을 받을 수 없어 해외 수치모델만으로 계산했습니다(실제 서비스는 단기예보까지 5개).",
            "공식 예비특보로 등급을 올리는 부분은 넣지 않았습니다 — 계산식 자체의 성능입니다.",
            "표본은 서로 독립이 아닙니다(한 번의 비·바람이 여러 지역·여러 시점에 걸칩니다). 특보가 있었던 날 수를 함께 보세요.",
        ],
    }


# ---------------------------------------------------------------- 실행


def run(cfg: Config, data_dir: Path, now: datetime, past_days: int, chunk: int, pause: float, limit_regions: int = 0, workers: int = 0) -> dict[str, Any]:
    events = rail_run.load_events(data_dir)
    if not events:
        raise SystemExit("실제 특보 기록(data/rail/events)이 없습니다. 열차 운행 기록 집계가 한 번은 돌아야 합니다.")
    intervals = rail.build_intervals(events)
    first_event = min(e["tm"] for e in events)
    regions = list(cfg.regions[:: max(1, len(cfg.regions) // limit_regions)][:limit_regions]) if limit_regions else list(cfg.regions)
    small = Config(regions=tuple(regions), thresholds=cfg.thresholds, settings=cfg.settings)
    truth = region_intervals(small, intervals)
    past_days = min(past_days, max(3, (now - first_event).days + 1), 92)

    archive: dict[str, dict[str, dict[int, ModelSeries]]] = {}
    models = [m for m in cfg.settings.models if m.source == "open_meteo"]
    for index, model in enumerate(models):
        if index and pause > 0:
            _pause(pause)
        try:
            archive[model.id] = fetch_archive(regions, model, past_days, cfg.settings.http, chunk, pause)
            log.info("과거 예보 %s: %d개 지역", model.id, len(archive[model.id]))
        except FetchError as e:
            log.error("과거 예보 %s 실패: %s", model.id, e)
    if not archive:
        raise SystemExit("과거 예보를 하나도 받지 못했습니다.")

    # 재현 구간: 특보 기록이 시작된 뒤 ~ 실제 결과를 48시간 지켜볼 수 있는 시점까지
    sample = next(iter(next(iter(archive.values())).values()))
    series = sample.get(1) or sample.get(2)
    start = max(first_event + timedelta(hours=12), series.times[0] + timedelta(hours=cfg.settings.past_hours + 48))
    end = min(series.times[-1], now) - timedelta(hours=cfg.settings.horizon_hours)
    times = replay_times(start, end)
    log.info("재현 %d회 (%s ~ %s), 지역 %d곳, 모델 %s", len(times), iso(times[0]) if times else "-", iso(times[-1]) if times else "-", len(regions), ", ".join(archive))
    started = time.monotonic()
    runs, table, days = replay(small, archive, truth, times, workers)
    log.info("재현 계산 %.0f초", time.monotonic() - started)
    return build_document(small, runs, table, days, truth, list(archive), len(regions), now)


def summary_lines(doc: Mapping[str, Any]) -> list[str]:
    lines = [f"과거 검증 {doc['period']['from']} ~ {doc['period']['to']} · 재현 {doc['period']['replays']}회 · 지역 {doc['regions']}곳 · 모델 {', '.join(doc['models'])}",
             f"실제 특보 {doc['events']}건(특보가 시작된 날 {doc['event_days']}일)"]
    for lead, c in (doc.get("capture") or {}).items():
        lines.append(f"  {lead}시간 전 사전 포착률: {c['captured']}/{c['events']}" + (f" = {c['rate']:.0%}" if c.get("rate") is not None else ""))
    for name, a in (doc.get("false_alarm") or {}).items():
        lines.append(f"  오경보율({name}): {a['false']}/{a['alerts']}" + (f" = {a['rate']:.0%}" if a.get("rate") is not None else ""))
    for row in doc.get("calibration") or []:
        lines.append(f"  가능성 {row['bucket']}: 표본 {row['cases']}, 48시간 안에 실제 특보 {row['hits']}" + (f" ({row['rate']:.1%})" if row.get("rate") is not None else "") + f", 특보일 {row['event_days']}일")
    for type_id, info in (doc.get("by_type") or {}).items():
        cap = (info.get("capture") or {}).get("24") or {}
        fa = (info.get("false_alarm") or {}).get("caution_or_higher") or {}
        lines.append(f"  [{info.get('name', type_id)}] 실제 {info.get('events')}건 · 24시간 전 포착 {cap.get('captured')}/{cap.get('events')} · '주의' 이상 {fa.get('alerts')}회 중 오경보 {fa.get('false')}회")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="과거 검증(백테스트): 지난 예보로 가능성을 다시 계산해 실제 특보와 맞춰 본다")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--out", type=Path, default=None, help="결과 파일 (기본: <data-dir>/backtest.json)")
    parser.add_argument("--days", type=int, default=75, help="거슬러 올라갈 일수(특보 기록이 있는 기간 안으로 줄인다)")
    parser.add_argument("--chunk", type=int, default=50, help="한 번에 요청할 지점 수")
    parser.add_argument("--pause", type=float, default=30.0, help="요청 사이에 쉴 초")
    parser.add_argument("--regions", type=int, default=0, help="시험용: 전국에서 고르게 뽑은 N곳만")
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("teukbo.http").setLevel(logging.WARNING)
    cfg = load_config()
    now = now_kst().replace(second=0, microsecond=0)
    doc = run(cfg, args.data_dir, now, args.days, args.chunk, args.pause, args.regions, args.workers)
    out = args.out or args.data_dir / "backtest.json"
    storage.write_json(out, doc, pretty=True)
    lines = summary_lines(doc)
    print("\n".join(lines))
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
