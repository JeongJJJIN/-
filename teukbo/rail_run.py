"""열차 지연 집계 실행: 전날까지의 운행 기록을 받아 날짜별 요약과 누적 통계를 만든다.

    python3 -m teukbo.rail_run            (GitHub Actions 가 수집 뒤에 실행)

- data/rail/days/날짜.json   그날 열차별 종착 지연과 지나간 구간의 특보 (커밋, 누적)
- data/rail/events/연-월.json 특보 발표·해제 이력 보관 (공식 조회 한도가 약 60일이라 받아 둔다)
- data/rail/summary.json     누적 통계 — 웹이 읽는다

아직 처리하지 않은 날이 있으면 한 번에 max_days_per_run 일씩, time_budget_seconds 안에서 채운다
(처음 며칠 동안 과거분이 차례로 쌓인다). 기상 수집·배포를 늦추지 않으려는 것이다.
인증키가 없거나 코레일 API 활용신청이 안 되어 있으면 아무것도 하지 않고 정상 종료한다.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from . import rail, storage
from .config import ROOT, Config, load_config
from .http import FetchError, normalize_service_key
from .sources import korail, kma_warnings
from .timeutil import iso, now_kst, parse_compact

log = logging.getLogger("teukbo.rail_run")

STATIONS_FILE = ROOT / "web" / "geo" / "stations.json"
TOP_DELAYED = 15


def rail_dir(data_dir: Path) -> Path:
    return data_dir / "rail"


def day_path(data_dir: Path, ymd: str) -> Path:
    return rail_dir(data_dir) / "days" / f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}.json"


def load_station_zones(path: Path = STATIONS_FILE) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """역 → 특보구역. (역코드 → [구역], 역 이름 → [구역])"""
    by_code: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    for s in storage.read_json(path, []) or []:
        if not s.get("zone"):
            continue
        if s.get("code"):
            by_code[str(s["code"])] = [s["zone"]]
        by_name.setdefault(str(s["name"]), [s["zone"]])
    return by_code, by_name


# ---------------------------------------------------------------- 특보 발표 이력 보관


def _events_path(data_dir: Path, ymd: str) -> Path:
    return rail_dir(data_dir) / "events" / f"{ymd[:4]}-{ymd[4:6]}.json"


def _pack(ev: Mapping[str, Any]) -> list[Any]:
    fmt = lambda t: t.strftime("%Y%m%d%H%M") if t else None  # noqa: E731
    return [ev["code"], ev["type"], ev["level"], ev["command"], fmt(ev["tm"]), fmt(ev["start"]), fmt(ev["end"])]


def _unpack(row: list[Any]) -> dict[str, Any]:
    code, type_name, level, command, tm, start, end = row
    return {"code": code, "type": type_name, "level": level, "command": command, "tm": parse_compact(tm), "start": parse_compact(start or ""), "end": parse_compact(end or "")}


def refresh_events(data_dir: Path, key: str, days: list[str], now: datetime, http_opts: Mapping[str, Any]) -> list[dict[str, Any]]:
    """필요한 날의 특보 발표 이력을 채우고(이미 받은 지난 날은 다시 받지 않는다) 전체 이력을 돌려준다."""
    today = now.strftime("%Y%m%d")
    months: dict[Path, dict[str, Any]] = {}
    fetched = failed = 0
    for ymd in sorted(set(days) | {today}):
        path = _events_path(data_dir, ymd)
        doc = months.setdefault(path, storage.read_json(path) or {"schema": 1, "month": f"{ymd[:4]}-{ymd[4:6]}", "days": {}, "fetched": {}})
        got_on = doc["fetched"].get(ymd)
        if got_on and got_on > ymd:
            continue  # 그날이 지난 뒤에 받은 것 → 더 바뀌지 않는다
        try:
            rows = kma_warnings.fetch_pwn_events(key, ymd, http_opts)
        except FetchError as e:
            failed += 1
            log.warning("특보 발표 이력 %s 조회 실패: %s", ymd, e)
            continue
        events = [e for e in (rail.normalize_event(r, kma_warnings.WARN_VAR, kma_warnings.WARN_STRESS) for r in rows) if e]
        doc["days"][ymd] = [_pack(e) for e in sorted(events, key=lambda e: e["tm"])]
        doc["fetched"][ymd] = today
        doc["_dirty"] = True
        fetched += 1
    for path, doc in months.items():
        if doc.pop("_dirty", False):
            storage.write_json(path, doc)
    if fetched or failed:
        log.info("특보 발표 이력: %d일 조회, %d일 실패", fetched, failed)
    return load_events(data_dir)


def load_events(data_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    folder = rail_dir(data_dir) / "events"
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            for rows in (storage.read_json(path) or {}).get("days", {}).values():
                out.extend(_unpack(r) for r in rows)
    return [e for e in out if e["tm"] is not None]


# ---------------------------------------------------------------- 하루 처리


def process_day(cfg: Config, key: str, ymd: str, timeline: rail.WarningTimeline, stations: tuple[dict, dict], now: datetime) -> dict[str, Any] | None:
    """그날의 운행계획·운행 기록을 받아 요약을 만든다. 아직 자료가 없으면 None."""
    http_opts = cfg.settings.http
    info = korail.fetch_run_info(key, ymd, http_opts)
    if not info:
        return None
    plan = korail.fetch_run_plan(key, ymd, http_opts)
    runs = rail.build_runs(plan, info)
    level_rank = {str(v["name"]): int(v["rank"]) for v in cfg.thresholds.levels.values()}
    unmatched = rail.attach_warnings(runs, timeline, stations[0], level_rank, stations[1])
    settings = cfg.settings.raw.get("rail", {})
    day = rail.summarize_day(ymd, runs, settings.get("train_classes", []), now)
    day["unmatched_stations"] = dict(unmatched.most_common(40))
    return day


def build_summary(data_dir: Path, cfg: Config, now: datetime) -> dict[str, Any] | None:
    folder = rail_dir(data_dir) / "days"
    days = [storage.read_json(p) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []
    days = [d for d in days if d]
    if not days:
        return None
    summary = rail.aggregate(days)
    latest = days[-1]
    idx = {name: i for i, name in enumerate(latest["fields"])}
    measured = [t for t in latest["trains"] if t[idx["arr_delay"]] is not None]
    top = sorted(measured, key=lambda t: -t[idx["arr_delay"]])[:TOP_DELAYED]
    summary["latest_day"] = {
        "date": latest["date"],
        "counts": latest["counts"],
        "all": latest["all"],
        "clear": latest["clear"],
        "by_warning": latest["by_warning"],
        "by_class": latest["by_class"],
        "by_line": latest["by_line"],
        "top_delayed": [{name: t[idx[name]] for name in ("trn_no", "line", "class", "origin", "dest", "plan_arr", "arr_delay", "warnings")} for t in top if t[idx["arr_delay"]] > 0],
    }
    summary["late_minutes"] = cfg.settings.raw.get("rail", {}).get("late_minutes", [5, 10, 20])
    summary["updated_at"] = iso(now)
    return summary


def run(cfg: Config, now: datetime, data_dir: Path, env: Mapping[str, str], max_days: int | None = None) -> int:
    key = normalize_service_key(env.get("DATA_GO_KR_SERVICE_KEY"))
    if not key:
        log.info("인증키가 없어 열차 지연 집계를 건너뜁니다.")
        return 0
    settings = cfg.settings.raw.get("rail", {})
    back = min(int(settings.get("back_days", 59)), 59)
    limit = int(max_days if max_days is not None else settings.get("max_days_per_run", 12))
    budget = float(settings.get("time_budget_seconds", 360))
    started = time.monotonic()
    days = rail.window_days(now, back)
    todo = [d for d in days if not day_path(data_dir, d).exists()][:limit]
    if not todo:
        log.info("새로 처리할 날이 없습니다.")
    else:
        try:
            events = refresh_events(data_dir, key, days, now, cfg.settings.http)
        except FetchError as e:
            log.error("특보 발표 이력을 받지 못해 열차 지연 집계를 미룹니다: %s", e)
            return 0
        timeline = rail.WarningTimeline(rail.build_intervals(events))
        stations = load_station_zones()
        done = 0
        for ymd in todo:
            if time.monotonic() - started > budget:
                log.info("이번 실행에 쓸 시간(%d초)을 다 써서 나머지는 다음 실행에 처리합니다.", budget)
                break
            try:
                day = process_day(cfg, key, ymd, timeline, stations, now)
            except FetchError as e:
                log.error("열차 운행 기록 %s 처리 실패(다음 실행에 다시 시도): %s", ymd, e)
                if "REGISTERED" in str(e) or "DENIED" in str(e):
                    log.error("→ 공공데이터포털에서 '한국철도공사_열차운행정보' 활용신청이 필요합니다.")
                    break
                continue
            if day is None:
                log.info("열차 운행 기록 %s: 아직 제공되지 않음", ymd)
                continue
            storage.write_json(day_path(data_dir, ymd), day)
            done += 1
            unmatched = day.get("unmatched_stations") or {}
            log.info("열차 운행 기록 %s: 열차 %d편, 지연 계산 %d편, 평균 %.1f분, 특보 구간 통과 %d편%s", ymd, day["counts"]["recorded"],
                     day["counts"]["measured"], day["all"].get("mean", 0.0), sum(v["n"] for v in day["by_warning"].values()),
                     f" (특보구역 미연결 역: {', '.join(list(unmatched)[:8])})" if unmatched else "")
        log.info("열차 지연 집계: %d일 처리 (남은 과거분 %d일)", done, len([d for d in days if not day_path(data_dir, d).exists()]))
    summary = build_summary(data_dir, cfg, now)
    if summary is not None:
        path = rail_dir(data_dir) / "summary.json"
        previous = storage.read_json(path) or {}
        if {k: v for k, v in previous.items() if k != "updated_at"} != {k: v for k, v in summary.items() if k != "updated_at"}:
            storage.write_json(path, summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="열차 지연 집계 (전날까지의 공식 운행 기록)")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--max-days", type=int, default=None, help="이번 실행에 새로 처리할 최대 일수")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("teukbo.http").setLevel(logging.WARNING)  # 요청마다 찍으면 수백 줄이 된다
    return run(load_config(), now_kst().replace(second=0, microsecond=0), args.data_dir, os.environ, args.max_days)


if __name__ == "__main__":
    sys.exit(main())
