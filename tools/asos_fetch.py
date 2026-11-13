"""기상청 종관기상관측(ASOS) 시간자료를 기간을 정해 받아 저장한다 — 과거 검증의 '실제 결과'로 쓴다.

    DATA_GO_KR_SERVICE_KEY=… python3 tools/asos_fetch.py --start 2024-07-01 --end 2024-12-31 --out tools/cache/asos_2024-07-01.json.gz

- 지점 목록: config/asos_stations.json (지점번호 → 우리 지역)
- 한 번에 받을 수 있는 행이 999개라, 지점마다 30일씩 끊어 받는다(지점은 몇 곳씩 동시에).
- 저장 형식(.json.gz): {"first": 첫 시각, "hours": 시간 수, "stations": {지점번호: {"name", "region", "rows": 관측이 있는 시간 수,
  "precip": […], "temp": […], "wind": […], "rh": […], "snow3h": […]}}} — 시간별 배열, 값이 없으면 null.
  강수량의 null 은 '그 시각에 비가 기록되지 않음'이다(관측 자체가 빠졌는지는 rows 와 기온 값으로 가늠한다).
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from teukbo.http import FetchError, normalize_service_key  # noqa: E402
from teukbo.sources import asos  # noqa: E402
from teukbo.timeutil import KST  # noqa: E402

VARS = ("precip", "temp", "wind", "rh", "snow3h")
WINDOW_DAYS = 30


def load_stations() -> list[dict]:
    return json.loads((ROOT / "config" / "asos_stations.json").read_text(encoding="utf-8"))["stations"]


def fetch_station(key: str, station: dict, start: datetime, end: datetime) -> tuple[dict, list[str]]:
    """한 지점의 start ~ end 를 시간별 배열로. 반환: (저장할 기록, 알림 글)"""
    hours = int((end - start).total_seconds() // 3600) + 1
    series = {name: [None] * hours for name in VARS}
    notes: list[str] = []
    rows = 0
    names: set[str] = set()
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=WINDOW_DAYS) - timedelta(hours=1))
        try:
            got = asos.fetch_hourly(key, station["stn"], cursor, stop, {"timeout_seconds": 40, "retries": 3, "backoff_seconds": 4})
        except FetchError as e:
            notes.append(f"{station['stn']} {station['name']} {cursor:%Y-%m-%d}~: 받지 못함 — {str(e)[:120]}")
            got = []
        for row in got:
            i = int((row["t"] - start).total_seconds() // 3600)
            if 0 <= i < hours:
                rows += 1
                names.add(row["name"])
                for name in VARS:
                    if row[name] is not None:
                        series[name][i] = round(row[name], 1)
        cursor = stop + timedelta(hours=1)
    if names and station["name"] not in names:
        notes.append(f"{station['stn']}: 지점 이름이 설정({station['name']})과 다릅니다 — 응답 {sorted(names)}")
    return {"name": station["name"], "region": station["region"], "rows": rows, **series}, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ASOS 시간자료 받아 저장")
    parser.add_argument("--start", required=True, help="첫날 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="마지막 날 YYYY-MM-DD (어제 이전)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--stations", default="", help="시험용: 쉼표로 적은 지점번호만")
    args = parser.parse_args(argv)
    key = normalize_service_key(os.environ.get("DATA_GO_KR_SERVICE_KEY"))
    if not key:
        print("인증키(DATA_GO_KR_SERVICE_KEY)가 없습니다.", file=sys.stderr)
        return 1
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=KST)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(hour=23, tzinfo=KST)
    stations = load_stations()
    if args.stations:
        only = set(args.stations.split(","))
        stations = [s for s in stations if s["stn"] in only]
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(lambda s: fetch_station(key, s, start, end), stations))
    doc = {"first": start.isoformat(), "hours": int((end - start).total_seconds() // 3600) + 1, "vars": list(VARS), "stations": {}}
    empty = 0
    for station, (record, notes) in zip(stations, results):
        for note in notes:
            print("알림:", note, file=sys.stderr)
        if record["rows"]:
            doc["stations"][station["stn"]] = record
        else:
            empty += 1
    if not doc["stations"]:
        print("관측 자료를 하나도 받지 못했습니다(활용신청·인증키를 확인하세요).", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.out, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(doc, f, separators=(",", ":"))
    total = sum(r["rows"] for r in doc["stations"].values())
    print(f"{args.start} ~ {args.end}: {len(doc['stations'])}곳({empty}곳은 자료 없음), 관측 {total:,}시간, {args.out.stat().st_size / 1024:.0f}KB, {time.monotonic() - started:.0f}초")
    return 0


if __name__ == "__main__":
    sys.exit(main())
