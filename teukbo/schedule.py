"""정기 수집·계산을 지금 돌려야 하는지 판단한다.

GitHub 의 예약 실행(cron)은 제시간을 보장하지 않는다 — 한 시간 넘게 늦거나 아예 건너뛰는 일이 있다.
그래서 예약은 30분마다 걸어 두고, 실제 수집·계산은 '정해진 시각(3시간 간격)이 지났는데 아직 그 회차를 돌리지 않았을 때'만 한다.
제시간에 온 예약은 그대로 돌고, 빠진 예약은 다음 30분 안에 따라잡는다. 때가 아니면 아무 자료도 받지 않고 바로 끝난다(API 호출 없음).

    python3 -m teukbo.schedule                  # 수집·계산(collect.yml) 안에서: 결과를 run=true|false 로 $GITHUB_OUTPUT 에 적는다
    python3 -m teukbo.schedule --start-collect  # 실시간 수집(live.yml) 끝에서: 정기 계산이 밀렸으면 수집·계산을 시작시킨다

두 번째는 예약에 기대지 않는 길이다: 실시간 수집은 몇 분마다 돌므로(GitHub 예약 또는 바깥 시계 — tools/cloud_clock.gs),
그때마다 '정해진 시각이 지났는데 아직 안 돌았는지' 보고, 그렇다면 GitHub 에 수집·계산 실행 신호를 보낸다.
같은 회차를 두 번 시작하지 않으려고 그 회차 뒤로 시작된 실행이 있는지 먼저 확인하고, 실패했으면 얼마 뒤(collect_retry_minutes) 다시 시작시킨다.

정해진 시각은 settings.json 의 local.trigger.collect_utc_hours·collect_minute (기상청 단기예보 발표 약 25분 뒤)와 같다.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import ROOT, load_config
from .storage import iter_history_days, read_json
from .timeutil import KST, parse_iso

DEFAULT_HOURS = [2, 5, 8, 11, 14, 17, 20, 23]
DEFAULT_MINUTE = 25


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


def should_start(now: datetime, slot: datetime, last: datetime | None, runs: Sequence[Mapping[str, Any]], retry: timedelta) -> tuple[bool, str]:
    """밀린 정기 계산을 지금 시작시켜야 하는지와 그 까닭.

    runs: GitHub 의 수집·계산 실행 기록(최근 것부터, created_at·status). 이 회차(slot) 뒤로 시작된 실행이
    아직 돌고 있으면 기다리고, 끝났는데도 이력이 없으면(실패) retry 가 지난 뒤에 다시 시작시킨다."""
    if last is not None and last >= slot:
        return False, "이 회차는 이미 계산했습니다"
    since: list[datetime] = []
    for run in runs:
        try:
            created = parse_iso(str(run["created_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            continue
        if created < slot:
            continue
        if run.get("status") != "completed":
            return False, "이 회차의 수집·계산이 이미 시작되어 돌고 있습니다"
        since.append(created)
    if since and now - max(since) < retry:
        return False, f"이 회차의 수집·계산이 방금 끝났는데 결과가 없습니다(실패했을 수 있음) — {retry.total_seconds() / 60:g}분 간격으로만 다시 시작시킵니다"
    return True, "정해진 시각이 지났는데 아직 계산하지 않았습니다"


def start_collect(trigger: Mapping[str, Any], now: datetime, last: datetime | None, repo: str, token: str, request: Callable[..., tuple[int | None, Any]]) -> tuple[bool, str]:
    """밀렸으면 수집·계산 실행 신호를 보낸다. 반환: (보냈는지, 사람이 읽을 설명). 실패해도 예외를 내지 않는다(실시간 수집을 멈추지 않으려고)."""
    hours = [int(h) for h in trigger.get("collect_utc_hours", DEFAULT_HOURS)]
    slot = latest_slot(now, hours, int(trigger.get("collect_minute", DEFAULT_MINUTE)))
    if last is not None and last >= slot:
        return False, "이 회차는 이미 계산했습니다"
    workflow = str(trigger.get("collect_workflow", "collect.yml"))
    status, body = request("GET", f"/repos/{repo}/actions/workflows/{workflow}/runs?per_page=20", token)
    if status != 200:
        return False, f"수집·계산 실행 기록을 읽지 못했습니다(코드 {status}) — 다음 차례에 다시 봅니다"
    retry = timedelta(minutes=float(trigger.get("collect_retry_minutes", 40)))
    start, why = should_start(now, slot, last, (body or {}).get("workflow_runs") or [], retry)
    if not start:
        return False, why
    # if_due: 받는 쪽(collect.yml)이 최신 이력을 받아 한 번 더 확인한다 — 그 사이에 다른 실행이 계산을 끝냈으면 몇 초 만에 끝난다.
    status, _ = request("POST", f"/repos/{repo}/actions/workflows/{workflow}/dispatches", token, {"ref": str(trigger.get("ref", "main")), "inputs": {"if_due": "true"}})
    if status not in (200, 201, 204):
        return False, f"수집·계산 실행 신호를 보내지 못했습니다(코드 {status}) — 다음 차례에 다시 시도합니다"
    return True, f"{why} → 수집·계산을 시작시켰습니다"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="정기 수집·계산을 지금 돌릴 때인지 판단")
    parser.add_argument("--start-collect", action="store_true", help="밀렸으면 GitHub 에 수집·계산 실행 신호를 보낸다(GITHUB_TOKEN·GITHUB_REPOSITORY 필요)")
    args = parser.parse_args(argv)
    cfg = load_config()
    trigger = cfg.settings.raw.get("local", {}).get("trigger", {})
    hours = [int(h) for h in trigger.get("collect_utc_hours", DEFAULT_HOURS)]
    minute = int(trigger.get("collect_minute", DEFAULT_MINUTE))
    now = datetime.now(timezone.utc)
    last = last_run_time(ROOT / "data" / "history")
    slot = latest_slot(now, hours, minute)
    head = f"가장 최근 정기 시각 {slot.astimezone(KST):%m-%d %H:%M} KST · 마지막 계산 {last.astimezone(KST).strftime('%m-%d %H:%M') + ' KST' if last else '없음'}"
    if args.start_collect:
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        repo = os.environ.get("GITHUB_REPOSITORY", "").strip() or str(trigger.get("repository", ""))
        if not token or not repo:
            print(f"{head} → 확인하지 않음(GITHUB_TOKEN 또는 저장소 이름이 없습니다)")
            return 0
        from .local import github_request  # 여기서만 쓴다

        _, text = start_collect(trigger, now, last, repo, token, github_request)
        print(f"{head} → {text}")
        return 0
    due = is_due(now, last, hours, minute)
    print(f"{head} → {'돌립니다' if due else '이번에는 건너뜁니다(이미 돌았음)'}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"run={'true' if due else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
