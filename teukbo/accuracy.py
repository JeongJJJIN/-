"""적중률 집계: 누적 이력(data/history)으로 예측 등급과 실제 특보 발효를 비교한다.

지표
- 사전 포착률: 실제로 특보가 발효된 건 가운데, 발효 12·24시간 전 시점의 등급이 '주의' 이상이었던 비율
- 오경보율: '주의' 이상(또는 '높음')으로 표시한 예측 가운데, 이후 예측 범위(48시간) 안에 실제 특보가 없었던 비율

비교에는 공식 발표를 반영하기 전의 '모델 계산 등급'(mg)을 쓴다. 예비특보로 올린 등급을 쓰면
계산식 자체의 성능을 알 수 없기 때문이다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .storage import SCHEMA, iter_history_days, read_json
from .timeutil import iso, parse_iso

log = logging.getLogger("teukbo.accuracy")

_ALERT_GRADES = ("caution", "high")
_MAX_GAP_HOURS = 9.0  # 직전 회차와 이보다 벌어지면 '새로 발효'인지 판단하지 않는다


def load_runs(history_dir: Path, since: datetime | None = None) -> list[dict[str, Any]]:
    """이력의 모든 회차를 시간순으로 읽는다. 각 회차에 '_t'(datetime)를 붙인다."""
    runs: list[dict[str, Any]] = []
    for path in iter_history_days(history_dir, since):
        try:
            doc = read_json(path) or {}
        except ValueError as e:
            log.warning("이력 파일을 읽지 못해 건너뜁니다: %s (%s)", path.name, e)
            continue
        for run in doc.get("runs", []):
            try:
                run["_t"] = parse_iso(run["t"])
            except (KeyError, ValueError):
                continue
            if since is None or run["_t"] >= since:
                runs.append(run)
    runs.sort(key=lambda r: r["_t"])
    return runs


def _active_rank(cfg: Config, region_rec: dict[str, Any] | None, type_id: str) -> int | None:
    """그 회차·지역에서 해당 종류 특보의 최고 발효 단계(rank). 특보 조회 실패면 None, 없으면 0."""
    if not region_rec or "a" not in region_rec:
        return None
    rank = 0
    for w in region_rec["a"]:
        if w.get("k") == type_id and w.get("l") in cfg.thresholds.levels:
            rank = max(rank, cfg.thresholds.level_rank(w["l"]))
    return rank


def find_events(runs: list[dict[str, Any]], cfg: Config) -> list[dict[str, Any]]:
    """실제 특보 발효 건(새로 발효되었거나 단계가 올라간 경우)을 찾는다."""
    by_key = {(h.type, h.level): h for h in cfg.thresholds.hazards}
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for i in range(1, len(runs)):
        run, prev = runs[i], runs[i - 1]
        if (run["_t"] - prev["_t"]).total_seconds() > _MAX_GAP_HOURS * 3600:
            continue
        for rid, rec in run.get("regions", {}).items():
            for w in rec.get("a", []) if "a" in rec else []:
                hazard = by_key.get((w.get("k"), w.get("l")))
                if hazard is None:
                    continue
                rank = cfg.thresholds.level_rank(hazard.level)
                prev_rank = _active_rank(cfg, prev.get("regions", {}).get(rid), hazard.type)
                if prev_rank is None or prev_rank >= rank:
                    continue  # 직전 상태를 모르거나, 이미 같은 단계 이상이 발효 중이었다
                when = run["_t"]
                if w.get("f"):
                    try:
                        official = parse_iso(w["f"])
                        if official > prev["_t"] - timedelta(hours=_MAX_GAP_HOURS):
                            when = official
                    except ValueError:
                        pass
                key = (rid, hazard.id, iso(when))
                if key in seen:
                    continue
                seen.add(key)
                events.append({"region": rid, "hazard": hazard.id, "type": hazard.type, "time": when, "seen": run["_t"]})
    events.sort(key=lambda e: e["time"])
    return events


def _run_at_lead(runs: list[dict[str, Any]], event_time: datetime, lead_h: float, tolerance_h: float) -> dict[str, Any] | None:
    """발효 lead_h 시간 전 시점에 가장 가까운(그보다 늦지 않은) 회차."""
    target = event_time - timedelta(hours=lead_h)
    best = None
    for run in runs:
        if run["_t"] > target:
            break
        best = run
    if best is None or (target - best["_t"]).total_seconds() > tolerance_h * 3600:
        return None
    return best


def _model_grade(run: dict[str, Any], region: str, hazard_id: str) -> str | None:
    rec = run.get("regions", {}).get(region)
    if rec is None or not (rec.get("x") or rec.get("q")):
        return None  # 그 회차에 이 지역 예측 자료가 없었다 (q: 예측은 있었지만 조용해서 극값을 생략한 지역)
    return rec.get("h", {}).get(hazard_id, {}).get("mg", "low")


def _rate(hit: int, total: int) -> float | None:
    return round(hit / total, 3) if total else None


def compute_accuracy(history_dir: Path, cfg: Config, now: datetime) -> dict[str, Any]:
    acc = cfg.settings.accuracy
    leads = [int(x) for x in acc.get("lead_hours", [12, 24])]
    tolerance = float(acc.get("lead_tolerance_hours", 4))
    window_days = int(acc.get("window_days", 365))
    since = now - timedelta(days=window_days)
    horizon = timedelta(hours=cfg.settings.horizon_hours)
    th = cfg.thresholds

    runs = load_runs(history_dir, since)
    events = find_events(runs, cfg)
    type_ids = list(th.types)

    def blank_capture() -> dict[str, dict[str, int]]:
        return {str(lead): {"events": 0, "captured": 0} for lead in leads}

    capture = blank_capture()
    capture_by_type = {t: blank_capture() for t in type_ids}
    recent: list[dict[str, Any]] = []
    for ev in events:
        row: dict[str, Any] = {"region": ev["region"], "hazard": ev["hazard"], "time": iso(ev["time"]), "grades": {}}
        for lead in leads:
            run = _run_at_lead(runs, ev["time"], lead, tolerance)
            grade = _model_grade(run, ev["region"], ev["hazard"]) if run else None
            row["grades"][str(lead)] = grade
            if grade is None or grade == "unknown":
                continue
            for bucket in (capture, capture_by_type[ev["type"]]):
                bucket[str(lead)]["events"] += 1
                if grade in _ALERT_GRADES:
                    bucket[str(lead)]["captured"] += 1
        recent.append(row)

    # 오경보율: 검증 기간(48시간)이 다 지난 회차의 '주의' 이상 예측을 대상으로 한다.
    def blank_alerts() -> dict[str, dict[str, int]]:
        return {"caution_or_higher": {"alerts": 0, "false": 0}, "high": {"alerts": 0, "false": 0}}

    alerts = blank_alerts()
    alerts_by_type = {t: blank_alerts() for t in type_ids}
    hazards_by_id = {h.id: h for h in th.hazards}
    last_time = runs[-1]["_t"] if runs else None
    for i, run in enumerate(runs):
        if last_time is None or run["_t"] + horizon > last_time:
            break
        for rid, rec in run.get("regions", {}).items():
            if "a" not in rec:
                continue
            for hid, entry in rec.get("h", {}).items():
                grade = entry.get("mg")
                hazard = hazards_by_id.get(hid)
                if hazard is None or grade not in _ALERT_GRADES:
                    continue
                rank = th.level_rank(hazard.level)
                if (_active_rank(cfg, rec, hazard.type) or 0) >= rank:
                    continue  # 이미 발효 중인 특보는 '예측'이 아니다
                hit, known = False, False
                for later in runs[i + 1 :]:
                    if later["_t"] > run["_t"] + horizon:
                        break
                    later_rank = _active_rank(cfg, later.get("regions", {}).get(rid), hazard.type)
                    if later_rank is None:
                        continue
                    known = True
                    if later_rank >= rank:
                        hit = True
                        break
                if not known:
                    continue
                names = ["caution_or_higher"] + (["high"] if grade == "high" else [])
                for bucket in (alerts, alerts_by_type[hazard.type]):
                    for name in names:
                        bucket[name]["alerts"] += 1
                        if not hit:
                            bucket[name]["false"] += 1

    def finish_capture(c: dict[str, dict[str, int]]) -> dict[str, Any]:
        return {k: {**v, "rate": _rate(v["captured"], v["events"])} for k, v in c.items()}

    def finish_alerts(a: dict[str, dict[str, int]]) -> dict[str, Any]:
        return {k: {**v, "rate": _rate(v["false"], v["alerts"])} for k, v in a.items()}

    by_type = {}
    for t in type_ids:
        n_events = sum(1 for e in events if e["type"] == t)
        n_alerts = alerts_by_type[t]["caution_or_higher"]["alerts"]
        if n_events or n_alerts:
            by_type[t] = {
                "name": th.type_name(t),
                "events": n_events,
                "capture": finish_capture(capture_by_type[t]),
                "false_alarm": finish_alerts(alerts_by_type[t]),
            }

    keep = int(acc.get("recent_events", 20))
    return {
        "schema": SCHEMA,
        "updated_at": iso(now),
        "basis": "model_grade",
        "period": {
            "from": iso(runs[0]["_t"]) if runs else None,
            "to": iso(runs[-1]["_t"]) if runs else None,
            "runs": len(runs),
            "window_days": window_days,
        },
        "lead_hours": leads,
        "horizon_hours": cfg.settings.horizon_hours,
        "events": len(events),
        "capture": finish_capture(capture),
        "false_alarm": finish_alerts(alerts),
        "by_type": by_type,
        "recent_events": recent[-keep:][::-1],
    }

