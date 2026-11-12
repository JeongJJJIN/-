"""계산 결과를 JSON 으로 저장.

- data/latest.json       : 웹이 처음 읽는 전체 지역 요약 (배포 전용, 저장소에 커밋하지 않음)
- data/detail/<지역>.json : 지역을 고르면 읽는 모델 비교 표·그래프 자료 (배포 전용)
- data/history/날짜.json  : 회차별 예측값·계산 결과·실제 특보 기록 (커밋, 삭제하지 않음)
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .assess import HazardAssessment, OfficialWarning, RegionAssessment
from .config import Config
from .indicators import HOUR, ModelSeries, indicator_points
from .timeutil import floor_hour, iso

SCHEMA = 1
NOTICE = "참고용입니다. 공식 특보·예비특보가 우선합니다."


def write_json(path: Path, data: Any, *, pretty: bool = False) -> None:
    """임시 파일에 쓴 뒤 바꿔치기해서, 쓰다 만 파일이 남지 않게 한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if pretty:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            else:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _t(dt: datetime | None) -> str | None:
    return iso(dt) if dt is not None else None


def _warning_dict(w: OfficialWarning) -> dict[str, Any]:
    name = w.type_name + ("" if w.status == "preliminary" else w.level_name)
    if w.status == "preliminary":
        name = f"{w.type_name} 예비특보" + (f"({w.level_name})" if w.level_name and w.level_name != "예비" else "")
    return {
        "type": w.type,
        "level": w.level,
        "name": name,
        "zone": w.zone_name,
        "from": _t(w.effective_from),
        "issued": _t(w.issued_at),
        "expected": w.expected_text or None,
    }


# ---------------------------------------------------------------- latest.json


def _hazard_summary(a: HazardAssessment) -> dict[str, Any]:
    d: dict[str, Any] = {"p": a.probability, "g": a.grade, "mg": a.model_grade}
    if a.stats is not None:
        d["n"] = a.stats.models_total
        d["x"] = a.stats.models_exceeding
        d["reach"] = round(a.stats.reach_max, 2)
        d["mean"] = round(a.stats.reach_mean, 2)
    if a.window_from is not None:
        d["from"], d["to"] = _t(a.window_from), _t(a.window_to)
    if a.peak_time is not None:
        d["peak"], d["peak_model"] = _t(a.peak_time), a.peak_model
    if a.off_season:
        d["off"] = True
    if a.official_status:
        d["official"] = a.official_status
        d["official_level"] = a.official_level
        if a.official_from is not None:
            d["official_from"] = _t(a.official_from)
    return d


def build_latest(
    assessments: list[RegionAssessment],
    cfg: Config,
    now: datetime,
    sources: dict[str, Any],
    model_status: dict[str, dict[str, Any]],
    demo: bool = False,
) -> dict[str, Any]:
    th, st = cfg.thresholds, cfg.settings
    regions = []
    for ra in assessments:
        top = ra.top()
        regions.append(
            {
                "id": ra.region.id,
                "name": ra.region.name,
                "province": ra.region.province,
                "models": ra.models_used,
                "top": ({"hazard": top.hazard.id, "grade": top.grade, "p": top.probability} if top else None),
                "official": {
                    "available": ra.official.available,
                    "active": [_warning_dict(w) for w in ra.official.active],
                    "preliminary": [_warning_dict(w) for w in ra.official.preliminary],
                },
                "hazards": {a.hazard.id: _hazard_summary(a) for a in ra.hazards},
            }
        )
    return {
        "schema": SCHEMA,
        "generated_at": iso(now),
        "demo": demo,
        "notice": NOTICE,
        "horizon_hours": st.horizon_hours,
        "stale_after_hours": st.stale_after_hours,
        "grades": {"caution_min": st.grade_caution_min, "high_min": st.grade_high_min},
        "scoring": {
            "weight_exceed_ratio": st.scoring.weight_exceed_ratio,
            "weight_max_reach": st.scoring.weight_max_reach,
            "weight_mean_reach": st.scoring.weight_mean_reach,
            "reach_floor": st.scoring.reach_floor,
        },
        "sources": sources,
        "models": [
            {"id": m.id, "name": m.name, "short": m.short or m.name, **model_status.get(m.id, {"ok": False})}
            for m in st.models
        ],
        "thresholds": {"version": th.version, "source": th.source, "checked_at": th.checked_at},
        "types": [{"id": k, "name": v["name"]} for k, v in th.types.items()],
        "levels": th.levels,
        "hazards": [
            {
                "id": h.id,
                "type": h.type,
                "level": h.level,
                "name": h.name,
                "method": h.method,
                "official_text": h.official_text,
                "note": h.note,
                "months": list(h.months),
            }
            for h in th.hazards
        ],
        "regions": regions,
    }


# ---------------------------------------------------------------- detail/<지역>.json


def _chart_key(cfg: Config, indicator_key: str) -> str | None:
    """지표를 그래프로 그릴 때 쓸 계열 키. 시간 단위 지표는 지표 자체(i:), 일 단위 지표는 원 변수(v:)."""
    ind = cfg.thresholds.indicators[indicator_key]
    if ind.kind in ("hourly", "rolling_sum"):
        return f"i:{ind.key}"
    if ind.kind == "period_extreme":
        return f"v:{ind.variable}"
    if ind.kind == "period_change":
        return _chart_key(cfg, ind.of or "")
    return None


def build_detail(ra: RegionAssessment, series: dict[str, ModelSeries], cfg: Config, now: datetime) -> dict[str, Any]:
    th, st = cfg.thresholds, cfg.settings
    start = floor_hour(now)
    times = [start + k * HOUR for k in range(st.horizon_hours + 1)]

    used = sorted({leaf.indicator for hz in th.hazards for leaf in hz.leaves()})
    chart_keys = sorted({k for k in (_chart_key(cfg, key) for key in used) if k})
    chart_series: dict[str, dict[str, list[float | None]]] = {}
    for ck in chart_keys:
        kind, name = ck.split(":", 1)
        per_model: dict[str, list[float | None]] = {}
        for mid in ra.models_used:
            ms = series[mid]
            if kind == "v":
                lookup = ms.lookup(name)
            else:
                lookup = {p.time: p.value for p in indicator_points(th.indicators[name], th.indicators, ms, now)}
            values = [_round(lookup.get(t)) for t in times]
            if any(v is not None for v in values):
                per_model[mid] = values
        if per_model:
            chart_series[ck] = per_model

    hazards: dict[str, Any] = {}
    for a in ra.hazards:
        if a.hazard.method != "model":
            continue
        rows = []
        for row in a.rows:
            ind = th.indicators[row.leaf.indicator]
            rows.append(
                {
                    "indicator": ind.key,
                    "op": row.leaf.op,
                    "value": row.threshold,
                    "days": row.leaf.consecutive_days,
                    "group": row.group,
                    "joint": row.joint,
                    "models": {
                        mid: ({"v": _round(r.value, ind.decimals), "t": _t(r.time), "x": r.exceeds, "r": round(r.reach, 2)} if r else None)
                        for mid, r in row.models.items()
                    },
                }
            )
        hazards[a.hazard.id] = {
            "criteria": rows,
            "models": {
                mid: {"reach": round(r.reach, 2), "x": r.exceeds, "first": _t(r.first_exceed), "peak": _t(r.peak_time)}
                for mid, r in a.per_model.items()
            },
        }

    return {
        "schema": SCHEMA,
        "id": ra.region.id,
        "name": ra.region.name,
        "generated_at": iso(now),
        "times": [iso(t) for t in times],
        "indicators": {
            key: {
                "label": th.indicators[key].label,
                "unit": th.indicators[key].unit,
                "kind": th.indicators[key].kind,
                "decimals": th.indicators[key].decimals,
                "chart": _chart_key(cfg, key),
            }
            for key in used
        },
        "series": chart_series,
        "hazards": hazards,
    }


# ---------------------------------------------------------------- warnings.json (전국 특보 현황)


def build_national(raws: list[Any], cfg: Config, now: datetime, status: dict[str, Any]) -> dict[str, Any]:
    """전국 특보·예비특보를 말단 특보구역 코드 단위로 정리한다(지도 표시용).

    raws: sources.kma_warnings.RawWarning 목록. 코드표에서 찾지 못한 구역은 unresolved 에 이름으로 남긴다.
    """
    from .sources.kma_warnings import classify  # 순환 참조를 피하려고 여기서 불러온다

    active: list[dict[str, Any]] = []
    preliminary: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for r in raws:
        type_id, level_id = classify(r.type_name, r.level_name, cfg.thresholds)
        if not r.zone_code:
            unresolved.append({"status": r.status, "name": r.type_name + r.level_name, "zone": r.zone_name, "parent": r.parent_name})
            continue
        key = (r.status, r.zone_code, r.type_name, r.level_name, r.expected_text)
        if key in seen:
            continue
        seen.add(key)
        if r.status == "active":
            active.append({"code": r.zone_code, "type": type_id, "level": level_id, "name": r.type_name + r.level_name, "from": _t(r.effective_from)})
        else:
            preliminary.append({"code": r.zone_code, "type": type_id, "level": level_id, "name": f"{r.type_name} 예비특보", "expected": r.expected_text or None})
    return {
        "schema": SCHEMA,
        "generated_at": iso(now),
        "available": bool(status.get("ok")),
        "provider": status.get("provider"),
        "active": active,
        "preliminary": preliminary,
        "unresolved": unresolved,
    }


def _national_for_history(raws: list[Any]) -> dict[str, Any]:
    """이력용 전국 특보: {a: {특보 이름: [구역 코드…]}, p: {종류: [구역 코드…]}}."""
    out: dict[str, dict[str, list[str]]] = {"a": {}, "p": {}}
    for r in raws:
        if not r.zone_code:
            continue
        bucket, name = (out["a"], r.type_name + r.level_name) if r.status == "active" else (out["p"], r.type_name)
        codes = bucket.setdefault(name, [])
        if r.zone_code not in codes:
            codes.append(r.zone_code)
    return out


# ---------------------------------------------------------------- history


def _history_indicator_order(cfg: Config) -> list[str]:
    return sorted({leaf.indicator for hz in cfg.thresholds.hazards for leaf in hz.leaves()})


def build_history_run(
    assessments: list[RegionAssessment],
    cfg: Config,
    now: datetime,
    sources_ok: dict[str, bool],
    national: list[Any] | None = None,
) -> dict[str, Any]:
    """이력 한 회차. 용량을 줄이려고 지표 값은 'ind' 순서의 배열로, 가능성 0% 인 특보는 생략한다.

    regions[지역].x[모델] = [[극값, 선행시간h] 또는 null, …]  ('ind' 순서)
    regions[지역].h[특보]  = {p: 가능성, mg: 모델 등급, g: 최종 등급, r: [모델별 도달률…('models' 순서)], w: [시작, 끝]}
    regions[지역].a       = 발효 중 특보 [{k: 종류, l: 단계, n: 이름, f: 발효 시각}]
    regions[지역].pre     = 예비특보 [{k, l, n, e: 예상 시점 문구}]
    nat                  = 전국 특보 {a: {특보 이름: [구역 코드…]}, p: {예비특보 종류: [구역 코드…]}} (특보 조회 성공 시에만)
    """
    order = _history_indicator_order(cfg)
    model_ids = [m.id for m in cfg.settings.models]
    regions: dict[str, Any] = {}
    for ra in assessments:
        x = {
            mid: [list(per[key]) if key in per else None for key in order]
            for mid, per in ra.extremes.items()
        }
        h: dict[str, Any] = {}
        for a in ra.hazards:
            if not a.probability and a.grade in ("low", "unknown") and a.model_grade in ("low", "unknown"):
                continue
            entry: dict[str, Any] = {"p": a.probability, "mg": a.model_grade, "g": a.grade}
            if a.per_model:
                entry["r"] = [round(a.per_model[m].reach, 2) if m in a.per_model else None for m in model_ids]
            if a.window_from is not None:
                entry["w"] = [_t(a.window_from), _t(a.window_to)]
            h[a.hazard.id] = entry
        rec: dict[str, Any] = {"x": x, "h": h}
        if ra.official.available:
            rec["a"] = [
                {"k": w.type, "l": w.level, "n": w.type_name + w.level_name, "f": _t(w.effective_from)}
                for w in ra.official.active
            ]
            rec["pre"] = [
                {"k": w.type, "l": w.level, "n": w.type_name, "e": w.expected_text or None}
                for w in ra.official.preliminary
            ]
        regions[ra.region.id] = rec
    run: dict[str, Any] = {
        "t": iso(now),
        "ok": sources_ok,
        "models": model_ids,
        "ind": order,
        "thresholds": cfg.thresholds.version,
        "regions": regions,
    }
    if national is not None:
        run["nat"] = _national_for_history(national)
    return run


def history_path(history_dir: Path, day: str) -> Path:
    return history_dir / f"{day}.json"


def append_history(history_dir: Path, run: dict[str, Any], now: datetime) -> Path:
    """그날 이력 파일에 회차를 덧붙인다(같은 시각의 회차가 있으면 교체). 기존 기록은 지우지 않는다."""
    day = now.strftime("%Y-%m-%d")
    path = history_path(history_dir, day)
    doc = read_json(path) or {"schema": SCHEMA, "date": day, "runs": []}
    runs = [r for r in doc.get("runs", []) if r.get("t") != run["t"]]
    runs.append(run)
    runs.sort(key=lambda r: r["t"])
    doc["runs"] = runs
    write_json(path, doc)
    return path


def iter_history_days(history_dir: Path, since: datetime | None = None) -> list[Path]:
    """이력 파일 경로를 날짜순으로. since 가 있으면 그 날짜 이후만."""
    if not history_dir.is_dir():
        return []
    paths = sorted(p for p in history_dir.glob("*.json") if len(p.stem) == 10)
    if since is not None:
        cutoff = (since - timedelta(days=1)).strftime("%Y-%m-%d")
        paths = [p for p in paths if p.stem >= cutoff]
    return paths
