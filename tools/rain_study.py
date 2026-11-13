"""호우특보 사전 포착 연구 — 과거 예보에서 '무엇이 얼마였을 때 실제 호우특보가 났는가'를 본다(분석 도구, 서비스에는 쓰이지 않는다).

    python3 tools/rain_study.py [--days 62] [--extra kma_seamless,jma_msm,...]

묻는 것
1. 실제 호우특보가 나기 하루 전, 예보 모델들은 그 지역에 비를 얼마나 예보하고 있었나(특보 기준 대비 비율).
2. 그 지역만 보지 않고 주변 수십 km 까지 보면(비구름의 위치가 어긋나는 것을 감안) 얼마나 더 잡히나.
3. 모델을 더하면(한국에 더 촘촘한 모델 등) 얼마나 더 잡히나.
4. '이 값 이상이면 알린다'는 기준을 어디에 두면 몇 건을 잡고(포착), 알린 것 가운데 몇 번이 맞나(적중).

방법
- 실제 특보: data/rail/events 의 기상청 발표 기록(teukbo.backtest.truth_events 와 같은 '건').
- 예보: Open-Meteo Previous Runs API 의 강수량만. 재현 시점 R 에서 R+24시간까지는 '하루 전에 예측한 값',
  그 뒤 R+48시간까지는 '이틀 전에 예측한 값'(미래를 훔쳐보지 않는다 — teukbo.backtest 와 같다).
- 예보 값: 앞으로 48시간 안의 3시간·12시간 누적 최대를 호우주의보 기준(3시간 60mm · 12시간 110mm)으로 나눈 '도달률'.
- 표본은 서로 독립이 아니다(한 번의 비가 여러 지역·시점에 걸친다). 특보가 시작된 날 수를 함께 본다.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from bisect import bisect_right
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from teukbo import backtest, rail, rail_run  # noqa: E402
from teukbo.config import Region, load_config  # noqa: E402
from teukbo.http import FetchError, get_json  # noqa: E402
from teukbo.timeutil import KST  # noqa: E402

RAIN = "heavy_rain"
STEP = 6  # 재현 간격(시간)
HORIZON = 48
EXTRA = "kma_seamless,kma_ldps,kma_gdps,jma_msm,ukmo_seamless,gem_seamless,meteofrance_seamless,cma_grapes_global"
THRESHOLDS = (0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0)
RADII = (0, 30, 60)


# ---------------------------------------------------------------- 과거 예보(강수량만)


def fetch_precip(regions: Sequence[Region], model_id: str, past_days: int, chunk: int, pause: float) -> tuple[list[datetime], dict[str, dict[int, list[float | None]]]]:
    """한 모델의 강수량 과거 예보 → (시각들, 지역 id → {1: 하루 전 예측, 2: 이틀 전 예측})."""
    times: list[datetime] = []
    out: dict[str, dict[int, list[float | None]]] = {}
    for start in range(0, len(regions), chunk):
        part = regions[start:start + chunk]
        if start and pause > 0:
            time.sleep(pause)
        params = backtest.build_params(part, model_id, past_days)
        params["hourly"] = "precipitation_previous_day1,precipitation_previous_day2"
        payload = get_json(backtest.ENDPOINT, params, timeout=90, retries=3, backoff=5, label=f"rain-study:{model_id}")
        if isinstance(payload, dict):
            if payload.get("error"):
                raise FetchError(f"{model_id}: {payload.get('reason')}")
            payload = [payload]
        if len(payload) != len(part):
            raise FetchError(f"{model_id}: 응답 지점 수가 다릅니다")
        for region, loc in zip(part, payload):
            hourly = (loc or {}).get("hourly") or {}
            raw = hourly.get("time") or []
            if not raw:
                continue
            if not times:
                times = [datetime.fromisoformat(t).replace(tzinfo=KST) for t in raw]
            leads = {}
            for lead in (1, 2):
                key = f"precipitation_previous_day{lead}"
                series = hourly.get(f"{key}_{model_id}", hourly.get(key))
                if isinstance(series, list) and len(series) == len(times) and any(v is not None for v in series):
                    leads[lead] = [float(v) if v is not None else None for v in series]
            if leads:
                out[region.id] = leads
    return times, out


def rolling_reach(series: list[float | None]) -> list[float]:
    """시각 i 에서 끝나는 3시간·12시간 누적을 호우주의보 기준으로 나눈 값 가운데 큰 쪽. 빈 값은 0으로 본다."""
    n = len(series)
    prefix = [0.0] * (n + 1)
    for i, v in enumerate(series):
        prefix[i + 1] = prefix[i] + (v or 0.0)
    return [max((prefix[i + 1] - prefix[max(0, i - 2)]) / 60.0, (prefix[i + 1] - prefix[max(0, i - 11)]) / 110.0) for i in range(n)]


def reach_at(leads: dict[int, list[float]], r: int) -> float:
    """재현 시점(시각 번호 r)에서 본 앞으로 48시간의 최대 도달률: 24시간까지는 하루 전 예측, 그 뒤는 이틀 전 예측."""
    near, far = leads.get(1), leads.get(2)
    best = 0.0
    if near is not None:
        best = max(best, max(near[r + 1:r + 25], default=0.0))
    source = far if far is not None else near
    if source is not None:
        best = max(best, max(source[r + 25:r + HORIZON + 1], default=0.0))
    return best


# ---------------------------------------------------------------- 분석


def distance_km(a: Region, b: Region) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b.lon - a.lon) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def quantiles(values: Sequence[float]) -> str:
    if not values:
        return "–"
    v = sorted(values)
    pick = lambda q: v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))]  # noqa: E731
    return " / ".join(f"{pick(q):.2f}" for q in (0.1, 0.25, 0.5, 0.75, 0.9))


def study(regions: Sequence[Region], times: Sequence[datetime], reach: dict[str, dict[str, dict[int, list[float]]]], truth: dict[str, Any],
          events: Sequence[dict[str, Any]], model_sets: dict[str, list[str]], out: list[str]) -> None:
    """reach: 모델 → 지역 id → {선행: 도달률 시계열}. 표를 out 에 줄 단위로 적는다."""
    index = {t: i for i, t in enumerate(times)}
    first = times[0] + timedelta(hours=60)
    replays = [t for t in backtest.replay_times(first, times[-1] - timedelta(hours=HORIZON)) if t in index]
    if not replays:
        out.append("재현 시점이 없습니다.")
        return
    rid = [r.id for r in regions]
    near = {radius: [[j for j, b in enumerate(regions) if distance_km(a, b) <= radius] for a in regions] for radius in RADII if radius}

    # 지역별 호우특보: 발효 구간(이미 발효 중인 시점은 예측 표본에서 뺀다)과 새로 시작한 시각
    spans = {r: [(s, e) for t, _, s, e, _ in truth.get(r, []) if t == RAIN] for r in rid}
    starts = {r: sorted(ev["time"] for ev in events if ev["region"] == r) for r in rid}
    lo, hi = replays[0] + timedelta(hours=24), replays[-1] + timedelta(hours=12)
    counted = [ev for ev in events if lo <= ev["time"] <= hi]
    days = sorted({ev["time"].astimezone(KST).strftime("%m-%d") for ev in counted})
    out.append(f"재현 {len(replays)}회({replays[0]:%m-%d %H시} ~ {replays[-1]:%m-%d %H시}) · 지역 {len(regions)}곳 · 실제 호우특보 {len(counted)}건(특보가 시작된 날 {len(days)}일: {', '.join(days)})")

    active = [[any(s <= t and (e is None or t < e) for s, e in spans[r]) for t in replays] for r in rid]
    hit = [[bisect_right(starts[r], t + timedelta(hours=HORIZON)) > bisect_right(starts[r], t) for t in replays] for r in rid]
    replay_index = {t: k for k, t in enumerate(replays)}

    def lead_replay(when: datetime, lead: int) -> int | None:
        """발효 lead 시간 전(그보다 늦지 않은 가장 가까운) 재현의 번호"""
        target = when - timedelta(hours=lead)
        k = bisect_right(replays, target) - 1
        return k if k >= 0 and target - replays[k] < timedelta(hours=STEP) else None

    region_index = {r: i for i, r in enumerate(rid)}
    for set_name, models in model_sets.items():
        models = [m for m in models if m in reach]
        if not models:
            continue
        # 모델별·지역별·재현별 도달률
        per_model = []
        for m in models:
            rows = []
            for r in rid:
                leads = reach[m].get(r)
                rows.append([reach_at(leads, index[t]) if leads else None for t in replays])
            per_model.append(rows)
        variants: dict[str, list[list[float]]] = {}
        for name, combine in (("평균", lambda v: sum(v) / len(v)), ("최대", max), ("상위2 평균", lambda v: sum(sorted(v)[-2:]) / min(2, len(v)))):
            variants[name] = [[(lambda v: combine(v) if v else 0.0)([rows[i][k] for rows in per_model if rows[i][k] is not None]) for k in range(len(replays))] for i in range(len(rid))]
        out.append("")
        out.append(f"### 모델 묶음: {set_name} ({', '.join(models)})")
        out.append("")
        out.append("| 예보 값 | 주변 반경 | 하루 전 값의 분포(10/25/50/75/90%) | 기준 | 하루 전 포착 | 반나절 전 포착 | 알린 횟수 | 그중 맞음 | 적중률 | 잡은 특보일 |")
        out.append("|---|---|---|---|---|---|---|---|---|---|")
        for name, grid in variants.items():
            for radius in RADII:
                if radius:
                    table = [[max(grid[j][k] for j in near[radius][i]) for k in range(len(replays))] for i in range(len(rid))]
                else:
                    table = grid
                before = []
                for ev in counted:
                    k = lead_replay(ev["time"], 24)
                    if k is not None:
                        before.append(table[region_index[ev["region"]]][k])
                spread = quantiles(before)
                for threshold in THRESHOLDS:
                    cap24 = cap12 = total24 = total12 = 0
                    got_days = set()
                    for ev in counted:
                        i = region_index[ev["region"]]
                        k24, k12 = lead_replay(ev["time"], 24), lead_replay(ev["time"], 12)
                        if k24 is not None:
                            total24 += 1
                            if table[i][k24] >= threshold:
                                cap24 += 1
                                got_days.add(ev["time"].astimezone(KST).strftime("%m-%d"))
                        if k12 is not None:
                            total12 += 1
                            cap12 += table[i][k12] >= threshold
                    alerts = hits = 0
                    for i in range(len(rid)):
                        row, act, ok = table[i], active[i], hit[i]
                        for k in range(len(replays)):
                            if row[k] >= threshold and not act[k]:
                                alerts += 1
                                hits += ok[k]
                    out.append(f"| {name} | {radius or '그 지역만'}{'km' if radius else ''} | {spread} | {threshold:.2f} | {cap24}/{total24} ({cap24 / total24:.0%}) | {cap12}/{total12} ({cap12 / total12:.0%}) | {alerts} | {hits} | "
                               f"{(hits / alerts if alerts else 0):.0%} | {len(got_days)}/{len(days)} |" if total24 and total12 else f"| {name} | {radius} | – | {threshold:.2f} | – | – | {alerts} | {hits} | – | – |")
                    spread = ""
    _ = replay_index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="호우특보 사전 포착 연구")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--days", type=int, default=62)
    parser.add_argument("--chunk", type=int, default=50)
    parser.add_argument("--pause", type=float, default=6.0)
    parser.add_argument("--extra", default=EXTRA, help="더해 볼 모델(쉼표로). 받을 수 없는 모델은 건너뛴다")
    parser.add_argument("--regions", type=int, default=0, help="시험용: 고르게 뽑은 N곳만")
    args = parser.parse_args(argv)

    cfg = load_config()
    regions = list(cfg.regions[:: max(1, len(cfg.regions) // args.regions)][:args.regions]) if args.regions else list(cfg.regions)
    raw_events = rail_run.load_events(args.data_dir)
    truth = backtest.region_intervals(cfg, rail.build_intervals(raw_events))
    events = [ev for ev in backtest.truth_events(cfg, truth) if ev["type"] == RAIN and ev["region"] in {r.id for r in regions}]

    base = [m.id for m in cfg.settings.models if m.source == "open_meteo"]
    extra = [m for m in (x.strip() for x in args.extra.split(",")) if m and m not in base]
    out: list[str] = ["## 호우특보 사전 포착 연구", ""]
    times: list[datetime] = []
    reach: dict[str, dict[str, dict[int, list[float]]]] = {}
    for model_id in base + extra:
        try:
            if model_id in extra:  # 받을 수 있는 모델인지 한 곳으로 먼저 확인
                fetch_precip(regions[:1], model_id, 5, 1, 0)
                time.sleep(2)
            model_times, data = fetch_precip(regions, model_id, args.days, args.chunk, args.pause)
        except FetchError as e:
            out.append(f"- {model_id}: 받지 못함 — {str(e)[:160]}")
            print(out[-1], file=sys.stderr, flush=True)
            continue
        if not data:
            out.append(f"- {model_id}: 자료 없음")
            continue
        if not times:
            times = model_times
        if model_times != times:
            out.append(f"- {model_id}: 시각이 다른 모델과 달라 뺌")
            continue
        reach[model_id] = {r: {lead: rolling_reach(series) for lead, series in leads.items()} for r, leads in data.items()}
        filled = sum(1 for leads in data.values() for series in leads.values() for v in series if v is not None)
        out.append(f"- {model_id}: {len(data)}곳, 값 {filled:,}개")
        print(out[-1], file=sys.stderr, flush=True)
        time.sleep(args.pause)
    if not reach:
        print("과거 예보를 하나도 받지 못했습니다.", file=sys.stderr)
        return 1
    out.append("")
    got_extra = [m for m in extra if m in reach]
    sets = {"지금 쓰는 모델": base}
    for m in got_extra:
        sets[f"지금 + {m}"] = base + [m]
    if len(got_extra) > 1:
        sets["지금 + 받은 모델 전부"] = base + got_extra
    study(regions, times, reach, truth, events, sets, out)
    text = "\n".join(out)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
