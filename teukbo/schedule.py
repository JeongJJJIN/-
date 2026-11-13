"""정기 수집·계산을 지금 돌려야 하는지 판단한다.

GitHub 의 예약 실행(cron)은 제시간을 보장하지 않는다 — 한 시간 넘게 늦거나 아예 건너뛰는 일이 있다.
그래서 예약은 30분마다 걸어 두고, 실제 수집·계산은 '정해진 시각(3시간 간격)이 지났는데 아직 그 회차를 돌리지 않았을 때'만 한다.
제시간에 온 예약은 그대로 돌고, 빠진 예약은 다음 30분 안에 따라잡는다. 때가 아니면 아무 자료도 받지 않고 바로 끝난다(API 호출 없음).

    python3 -m teukbo.schedule      # GitHub Actions 에서: 결과를 run=true|false 로 $GITHUB_OUTPUT 에 적는다

정해진 시각은 settings.json 의 local.trigger.collect_utc_hours·collect_minute (기상청 단기예보 발표 약 25분 뒤)와 같다.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from .config import ROOT, load_config
from .storage import iter_history_days, read_json
from .timeutil import parse_iso


def latest_slot(now: datetime, utc_hours: Sequence[int], minute: int) -> datetime:
    """now 이전(같은 시각 포함)의 가장 가까운 정기 시각(UTC)."""
    now = now.astimezone(timezone.utc)
    for back in (0, 1):
        day = (now - timedelta(days=back)).date()
        for hour in sorted(utc_hours, reverse=True):
            slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)
            if slot <= now:
                return slot
    raise ValueError("정기 시각이 비어 있습니다")


def last_run_time(history_dir: Path) -> datetime | None:
    """이력에 남은 가장 최근 회차의 시각. 이력이 없거나 읽지 못하면 None."""
    for path in reversed(iter_history_days(history_dir)):
        try:
            runs = (read_json(path) or {}).get("runs", [])
        except ValueError:
            continue
        times = []
        for run in runs:
            try:
                times.append(parse_iso(run["t"]))
            except (KeyError, ValueError, TypeError):
                continue
        if times:
            return max(times)
    return None


def is_due(now: datetime, last: datetime | None, utc_hours: Sequence[int], minute: int) -> bool:
    """가장 최근 정기 시각 이후로 아직 돌린 적이 없으면 True."""
    return last is None or last < latest_slot(now, utc_hours, minute)


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    trigger = cfg.settings.raw.get("local", {}).get("trigger", {})
    hours = [int(h) for h in trigger.get("collect_utc_hours", [2, 5, 8, 11, 14, 17, 20, 23])]
    minute = int(trigger.get("collect_minute", 25))
    now = datetime.now(timezone.utc)
    last = last_run_time(ROOT / "data" / "history")
    slot = latest_slot(now, hours, minute)
    due = is_due(now, last, hours, minute)
    kst = timezone(timedelta(hours=9))
    print(f"가장 최근 정기 시각 {slot.astimezone(kst):%m-%d %H:%M} KST · 마지막 계산 {last.astimezone(kst).strftime('%m-%d %H:%M') + ' KST' if last else '없음'} → {'돌립니다' if due else '이번에는 건너뜁니다(이미 돌았음)'}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"run={'true' if due else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
