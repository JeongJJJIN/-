"""열차 지연 집계와 특보 영향 분석 (전날까지의 공식 운행 기록 기준).

자료: 한국철도공사 열차운행정보(운행계획 + 실제 운행 기록, 전날까지 제공), 기상청 특보 발표 이력(getPwnCd).
실시간 분석이 아니다. '어제 운행한 열차가 계획보다 얼마나 늦게 도착했는지'와
'그 열차가 지나간 역에 그 시각 어떤 특보가 발효 중이었는지'를 묶어 통계를 낸다.

용어
- 종착 지연: 종착역 실제 도착 시각 − 계획 도착 시각 (분). 역별 계획 시각은 공개되지 않아 종착역 기준만 계산한다.
- 특보 구간 통과: 정차한 역이 속한 특보구역에, 그 역을 지난 시각에 특보가 발효 중이었던 경우.
  (통과만 한 역은 운행 기록에 없으므로 정차역 기준이다.)
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

from .timeutil import iso, parse_compact

STOP_ORIGIN = "01"  # 시발
STOP_TERMINUS = "05"  # 종착
_MAX_SANE_DELAY_MIN = 12 * 60  # 이보다 큰 차이는 날짜 불일치 등 자료 오류로 본다
ISSUE_COMMANDS = {"1", "7"}  # 발표, 변경발표
RELEASE_COMMANDS = {"2", "8"}  # 해제, 변경해제


def parse_dt(text: Any) -> datetime | None:
    """'2026-11-11 05:13:00.0' → KST datetime. 빈 값은 None."""
    if text in (None, "", "null"):
        return None
    return parse_compact(str(text))


# ---------------------------------------------------------------- 열차 운행 기록


@dataclass(frozen=True)
class Stop:
    code: str
    name: str
    kind: str  # 정차구분코드 (01 시발, 05 종착, 11 여객승하차 …)
    arr: datetime | None
    dep: datetime | None

    @property
    def time(self) -> datetime | None:
        return self.arr or self.dep


@dataclass
class TrainRun:
    ymd: str
    trn_no: str
    line: str = ""  # 주운행선명 (경부선, 전라선 …)
    direction: str = ""  # U 상행 / D 하행
    origin: str = ""
    dest: str = ""
    plan_dep: datetime | None = None
    plan_arr: datetime | None = None
    stops: list[Stop] = field(default_factory=list)
    warnings: dict[str, str] = field(default_factory=dict)  # 지나간 구간의 특보 {종류: 최고 단계 이름}

    @property
    def number(self) -> int | None:
        digits = "".join(ch for ch in self.trn_no if ch.isdigit())
        return int(digits) if digits else None

    @property
    def act_dep(self) -> datetime | None:
        return self.stops[0].dep if self.stops else None

    @property
    def act_arr(self) -> datetime | None:
        """종착역 실제 도착 시각. 종착 기록이 없으면(운행 중단·기록 누락) None."""
        if not self.stops or self.stops[-1].kind != STOP_TERMINUS:
            return None
        return self.stops[-1].arr

    @property
    def has_record(self) -> bool:
        return bool(self.stops)

    def _delay(self, actual: datetime | None, planned: datetime | None) -> int | None:
        if actual is None or planned is None:
            return None
        minutes = int(round((actual - planned).total_seconds() / 60.0))
        return minutes if abs(minutes) <= _MAX_SANE_DELAY_MIN else None

    @property
    def dep_delay(self) -> int | None:
        return self._delay(self.act_dep, self.plan_dep)

    @property
    def arr_delay(self) -> int | None:
        return self._delay(self.act_arr, self.plan_arr)


def build_runs(plan_rows: Iterable[Mapping[str, Any]], info_rows: Iterable[Mapping[str, Any]]) -> list[TrainRun]:
    """운행계획 행과 운행정보 행을 열차 단위로 묶는다 (같은 운행일자끼리)."""
    runs: dict[tuple[str, str], TrainRun] = {}

    def get(ymd: str, trn_no: str) -> TrainRun:
        key = (ymd, trn_no)
        if key not in runs:
            runs[key] = TrainRun(ymd=ymd, trn_no=trn_no)
        return runs[key]

    for row in plan_rows:
        run = get(str(row.get("run_ymd", "")), str(row.get("trn_no", "")))
        run.origin = str(row.get("dptre_stn_nm") or "")
        run.dest = str(row.get("arvl_stn_nm") or "")
        run.plan_dep = parse_dt(row.get("trn_plan_dptre_dt"))
        run.plan_arr = parse_dt(row.get("trn_plan_arvl_dt"))

    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in info_rows:
        grouped.setdefault((str(row.get("run_ymd", "")), str(row.get("trn_no", ""))), []).append(row)
    for (ymd, trn_no), rows in grouped.items():
        run = get(ymd, trn_no)

        def order(r: Mapping[str, Any]) -> int:
            try:
                return int(r.get("trn_run_sn") or 0)
            except (TypeError, ValueError):
                return 0

        rows.sort(key=order)
        run.stops = [
            Stop(str(r.get("stn_cd") or ""), str(r.get("stn_nm") or ""), str(r.get("stop_se_cd") or ""), parse_dt(r.get("trn_arvl_dt")), parse_dt(r.get("trn_dptre_dt")))
            for r in rows
        ]
        lines = Counter(str(r.get("mrnt_nm") or "") for r in rows if r.get("mrnt_nm"))
        run.line = lines.most_common(1)[0][0] if lines else ""
        dirs = Counter(str(r.get("uppln_dn_se_cd") or "") for r in rows if r.get("uppln_dn_se_cd"))
        run.direction = dirs.most_common(1)[0][0] if dirs else ""
        if not run.origin:
            run.origin = run.stops[0].name
        if not run.dest:
            run.dest = run.stops[-1].name
    return sorted(runs.values(), key=lambda r: (r.ymd, r.trn_no))


def train_class(number: int | None, classes: Sequence[Mapping[str, Any]]) -> str:
    """열차번호 대역으로 열차 종류를 구분한다(settings.json rail.train_classes — 관례에 따른 구분)."""
    if number is not None:
        for c in classes:
            if int(c["min"]) <= number <= int(c["max"]):
                return str(c["name"])
    return "기타"


# ---------------------------------------------------------------- 특보 발효 구간(시간)


@dataclass(frozen=True)
class Interval:
    code: str
    type_name: str
    level_name: str
    start: datetime
    end: datetime | None  # None = 아직 해제되지 않음


def normalize_event(row: Mapping[str, Any], warn_var: Mapping[int, str], warn_stress: Mapping[int, str]) -> dict[str, Any] | None:
    """getPwnCd 한 행 → {code, type, level, command, start, end, tm}. 쓸 수 없는 행은 None."""
    if str(row.get("cancel", "0")).strip() != "0":
        return None
    try:
        type_name = warn_var[int(row.get("warnVar"))]
        level_name = warn_stress[int(row.get("warnStress"))]
    except (KeyError, TypeError, ValueError):
        return None
    code = str(row.get("areaCode") or "").strip()
    tm = parse_compact(str(row.get("tmFc") or ""))
    if not code or tm is None:
        return None
    return {
        "code": code,
        "type": type_name,
        "level": level_name,
        "command": str(row.get("command") or "").strip(),
        "start": parse_compact(str(row.get("startTime") or "")),
        "end": parse_compact(str(row.get("endTime") or "")),
        "tm": tm,
    }


def build_intervals(events: Iterable[Mapping[str, Any]]) -> list[Interval]:
    """발표·해제 이력을 구역·종류별 발효 구간으로 바꾼다.

    발표/변경발표 → 발효 시각에 구간 시작(같은 종류의 다른 단계가 열려 있으면 그 시각에 닫는다).
    해제/변경해제 → 해제 시각에 구간 끝. 연장·정정은 구간을 바꾸지 않는다.
    집계 기간 이전에 발표되어 이력에 없는 특보는 알 수 없다(구간이 만들어지지 않는다).
    """
    out: list[Interval] = []
    open_by_key: dict[tuple[str, str], tuple[str, datetime]] = {}  # (구역, 종류) → (단계, 시작)
    for ev in sorted(events, key=lambda e: (e["tm"], 0 if e["command"] in RELEASE_COMMANDS else 1)):
        key = (ev["code"], ev["type"])
        if ev["command"] in ISSUE_COMMANDS:
            start = ev["start"] or ev["tm"]
            current = open_by_key.get(key)
            if current is not None:
                if current[0] == ev["level"]:
                    continue  # 같은 단계가 이미 발효 중
                out.append(Interval(ev["code"], ev["type"], current[0], current[1], max(start, current[1])))
            open_by_key[key] = (ev["level"], start)
        elif ev["command"] in RELEASE_COMMANDS:
            current = open_by_key.get(key)
            if current is None or current[0] != ev["level"]:
                continue  # 이미 다른 단계로 바뀌었거나, 기간 이전에 발표된 특보의 해제
            end = ev["end"] or ev["tm"]
            out.append(Interval(ev["code"], ev["type"], current[0], current[1], max(end, current[1])))
            del open_by_key[key]
    for (code, type_name), (level, start) in open_by_key.items():
        out.append(Interval(code, type_name, level, start, None))
    return sorted(out, key=lambda i: (i.start, i.code, i.type_name))


class WarningTimeline:
    """구역 코드·시각으로 그때 발효 중이던 특보를 찾는다."""

    def __init__(self, intervals: Iterable[Interval]):
        self._by_code: dict[str, list[Interval]] = {}
        for interval in intervals:
            self._by_code.setdefault(interval.code, []).append(interval)

    def active(self, code: str, when: datetime) -> list[Interval]:
        return [i for i in self._by_code.get(code, []) if i.start <= when and (i.end is None or when < i.end)]


def plain_name(name: str) -> str:
    """역 이름에서 괄호와 빈칸을 뺀다: '김천(구미)' → '김천구미'. 표기가 조금 다른 이름을 잇는 데 쓴다."""
    return re.sub(r"[()\s]", "", name or "")


def attach_warnings(
    runs: Iterable[TrainRun],
    timeline: WarningTimeline,
    station_zones: Mapping[str, Sequence[str]],
    level_rank: Mapping[str, int],
    zones_by_name: Mapping[str, Sequence[str]] | None = None,
) -> Counter:
    """열차마다, 정차한 역의 특보구역에 그 시각 발효 중이던 특보를 붙인다(종류별 최고 단계).

    역은 역코드로 찾고, 표에 없는 코드는 역 이름으로 찾는다. 반환: 특보구역을 찾지 못한 역 이름별 정차 횟수.
    """
    by_name = zones_by_name or {}
    unmatched: Counter = Counter()
    for run in runs:
        found: dict[str, str] = {}
        for stop in run.stops:
            when = stop.time
            zones = station_zones.get(stop.code) or by_name.get(stop.name) or by_name.get(plain_name(stop.name))
            if not zones:
                unmatched[stop.name] += 1
                continue
            if when is None:
                continue
            for code in zones:
                for interval in timeline.active(code, when):
                    best = found.get(interval.type_name)
                    if best is None or level_rank.get(interval.level_name, 0) > level_rank.get(best, 0):
                        found[interval.type_name] = interval.level_name
        run.warnings = found
    return unmatched


# ---------------------------------------------------------------- 집계


def delay_stats(delays: Sequence[int], late_minutes: Sequence[int] = (5, 10, 20)) -> dict[str, Any]:
    """지연 분포 요약. 조착(음수)은 0분 지연으로 본다."""
    values = [max(0, d) for d in delays]
    n = len(values)
    if n == 0:
        return {"n": 0}
    out: dict[str, Any] = {
        "n": n,
        "mean": round(sum(values) / n, 1),
        "median": round(float(median(values)), 1),
        "max": max(values),
    }
    for m in late_minutes:
        out[f"late{m}"] = sum(1 for v in values if v >= m)
    return out


def summarize_day(ymd: str, runs: Sequence[TrainRun], classes: Sequence[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    """하루치 요약 (data/rail/날짜.json). 열차별 한 줄 + 노선·특보별 집계."""
    day = [r for r in runs if r.ymd == ymd]
    measured = [r for r in day if r.arr_delay is not None]
    by_line: dict[str, list[int]] = {}
    by_class: dict[str, list[int]] = {}
    by_warning: dict[str, list[int]] = {}
    clear: list[int] = []
    for r in measured:
        by_line.setdefault(r.line or "미상", []).append(r.arr_delay)
        by_class.setdefault(train_class(r.number, classes), []).append(r.arr_delay)
        if r.warnings:
            for type_name in r.warnings:
                by_warning.setdefault(type_name, []).append(r.arr_delay)
        else:
            clear.append(r.arr_delay)
    trains = [
        [
            r.trn_no,
            r.line,
            train_class(r.number, classes),
            r.origin,
            r.dest,
            r.plan_dep.strftime("%H:%M") if r.plan_dep else None,
            r.plan_arr.strftime("%H:%M") if r.plan_arr else None,
            r.dep_delay,
            r.arr_delay,
            [f"{t}{lv}" for t, lv in sorted(r.warnings.items())],
        ]
        for r in day
        if r.plan_arr is not None or r.has_record
    ]
    return {
        "schema": 1,
        "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
        "built_at": iso(now),
        "counts": {
            "planned": sum(1 for r in day if r.plan_arr is not None),
            "recorded": sum(1 for r in day if r.has_record),
            "measured": len(measured),
            "no_record": sum(1 for r in day if r.plan_arr is not None and not r.has_record),
            "no_terminus": sum(1 for r in day if r.has_record and r.act_arr is None),
        },
        "all": delay_stats([r.arr_delay for r in measured]),
        "clear": delay_stats(clear),
        "by_warning": {k: delay_stats(v) for k, v in sorted(by_warning.items())},
        "by_line": {k: delay_stats(v) for k, v in sorted(by_line.items())},
        "by_class": {k: delay_stats(v) for k, v in sorted(by_class.items())},
        "fields": ["trn_no", "line", "class", "origin", "dest", "plan_dep", "plan_arr", "dep_delay", "arr_delay", "warnings"],
        "trains": trains,
    }


def aggregate(days: Sequence[Mapping[str, Any]], recent_days: int = 14) -> dict[str, Any]:
    """여러 날의 요약을 합쳐 '특보 구간 통과 열차 vs 그 외' 통계를 만든다 (data/rail/summary.json)."""
    days = sorted(days, key=lambda d: d["date"])
    clear: list[int] = []
    warned: list[int] = []  # 특보 구간을 지난 열차 (열차당 한 번)
    by_warning: dict[str, list[int]] = {}  # 호우주의보, 호우경보 …
    by_type: dict[str, list[int]] = {}  # 호우, 강풍 …
    by_line_warn: dict[str, dict[str, list[int]]] = {}  # 노선 → {"warn": […], "clear": […]}
    daily: list[dict[str, Any]] = []
    for day in days:
        idx = {name: i for i, name in enumerate(day.get("fields", []))}
        day_warned = 0
        for row in day.get("trains", []):
            delay = row[idx["arr_delay"]]
            if delay is None:
                continue
            names = row[idx["warnings"]] or []
            line = row[idx["line"]] or "미상"
            slot = by_line_warn.setdefault(line, {"warn": [], "clear": []})
            if names:
                day_warned += 1
                warned.append(delay)
                slot["warn"].append(delay)
                for name in names:
                    by_warning.setdefault(name, []).append(delay)
                for type_name in {n.replace("중대경보", "").replace("주의보", "").replace("경보", "") for n in names}:
                    by_type.setdefault(type_name, []).append(delay)
            else:
                slot["clear"].append(delay)
                clear.append(delay)
        daily.append({"date": day["date"], **{k: day.get("all", {}).get(k) for k in ("n", "mean", "late10", "max")}, "warned": day_warned})
    lines = {
        line: {"warn": delay_stats(v["warn"]), "clear": delay_stats(v["clear"])}
        for line, v in sorted(by_line_warn.items())
        if len(v["warn"]) + len(v["clear"]) >= 1
    }
    return {
        "schema": 1,
        "period": {"from": days[0]["date"] if days else None, "to": days[-1]["date"] if days else None, "days": len(days)},
        "clear": delay_stats(clear),
        "warned": delay_stats(warned),
        "by_type": {k: delay_stats(v) for k, v in sorted(by_type.items())},
        "by_warning": {k: delay_stats(v) for k, v in sorted(by_warning.items())},
        "by_line": lines,
        "daily": daily[-recent_days:],
        "latest": days[-1]["date"] if days else None,
    }


def window_days(now: datetime, back: int) -> list[str]:
    """어제부터 back 일 전까지의 운행일자(YYYYMMDD), 최근 날짜부터."""
    return [(now - timedelta(days=k)).strftime("%Y%m%d") for k in range(1, back + 1)]
