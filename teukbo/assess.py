"""지역별·특보별 가능성 판정.

모델별 지표 값 → 기준 대비 도달률·초과 여부 → 가능성(%)·등급·예상 시간대.
기상청 공식 특보(발효 중)·예비특보가 있으면 계산 결과보다 우선해 등급을 올린다.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from .config import AllOf, Config, Hazard, Leaf, Region, Thresholds
from .indicators import ModelSeries, Point, add_apparent_temp, in_horizon, indicator_points
from .scoring import (
    GRADE_LOW,
    GRADE_RANK,
    GRADE_UNKNOWN,
    Probability,
    apply_official,
    compute_probability,
    grade_for,
    reach_ratio,
)

_OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
}


# ---------------------------------------------------------------- 공식 특보


@dataclass(frozen=True)
class OfficialWarning:
    """기상청이 발표한 특보 또는 예비특보 한 건 (한 특보구역 기준)."""

    status: str  # "active"(발효 중) | "preliminary"(예비특보)
    type: str | None  # thresholds.json 의 type id. 계산 대상이 아닌 종류(건조 등)는 None
    type_name: str  # 기상청 표기 (호우, 폭염 …)
    level: str | None  # level id (advisory/warning/severe). 예비특보에서 단계 미상이면 None
    level_name: str  # 주의보 / 경보 / 예비 …
    zone_code: str | None = None
    zone_name: str = ""
    effective_from: datetime | None = None  # 발효 시각
    issued_at: datetime | None = None  # 발표 시각
    expected_text: str = ""  # 예비특보의 예상 시점 문구 등


@dataclass
class RegionOfficial:
    """한 지역에 해당하는 공식 특보 현황. available=False 이면 조회 실패(없음과 구별)."""

    available: bool = False
    active: list[OfficialWarning] = field(default_factory=list)
    preliminary: list[OfficialWarning] = field(default_factory=list)


# ---------------------------------------------------------------- 평가 결과


@dataclass(frozen=True)
class SlotEval:
    time: datetime
    value: float | None
    reach: float
    exceeds: bool


@dataclass
class LeafModelResult:
    """기준 한 줄에 대한 한 모델의 결과 (표시용)."""

    value: float
    time: datetime
    reach: float
    exceeds: bool


@dataclass
class ModelHazardResult:
    reach: float
    exceeds: bool
    first_exceed: datetime | None
    peak_time: datetime | None


@dataclass
class CriterionRow:
    """모델 비교 표의 한 열: 기준 한 줄과 모델별 최대(최저) 예측값."""

    leaf: Leaf
    threshold: float  # 이 지역에 적용되는 기준값
    group: int  # 같은 번호끼리는 '동시에 만족'(AND) 묶음
    joint: bool
    models: dict[str, Optional[LeafModelResult]]


@dataclass
class HazardAssessment:
    hazard: Hazard
    probability: int | None
    model_grade: str
    grade: str
    stats: Probability | None
    per_model: dict[str, ModelHazardResult]
    window_from: datetime | None
    window_to: datetime | None
    peak_time: datetime | None
    peak_model: str | None
    official_status: str | None  # None | "preliminary" | "active"
    official_level: str | None  # 공식 발표의 단계 (자기 단계보다 높을 수 있음)
    official_from: datetime | None  # 공식 특보의 발효 시각 (미래면 '발효 예정')
    rows: list[CriterionRow]
    off_season: bool = False  # 예측 범위 전체가 특보 적용 기간(월) 밖


@dataclass
class RegionAssessment:
    region: Region
    hazards: list[HazardAssessment]
    official: RegionOfficial
    models_used: list[str]
    extremes: dict[str, dict[str, tuple[float, int]]]  # 모델 → 지표 → (극값, 선행시간 h)

    def top(self) -> HazardAssessment | None:
        ranked = [h for h in self.hazards if h.grade != GRADE_UNKNOWN]
        if not ranked:
            return None
        return max(ranked, key=lambda h: (GRADE_RANK[h.grade], h.probability or 0))


# ---------------------------------------------------------------- 기준 평가


def _sustained(points: dict[str, Point], slot: str, days: int, op: str) -> float | None:
    """slot(날짜)부터 days 일 연속으로 유지되는 수준. 하루라도 값이 없으면 None."""
    start = date.fromisoformat(slot)
    values: list[float] = []
    for k in range(days):
        p = points.get((start + timedelta(days=k)).isoformat())
        if p is None:
            return None
        values.append(p.value)
    return min(values) if op in (">=", ">") else max(values)


def evaluate_leaf(
    leaf: Leaf,
    points: list[Point],
    now: datetime,
    horizon_end: datetime,
    threshold: float | None = None,
    months: tuple[int, ...] = (),
) -> dict[str, SlotEval]:
    """기준 한 줄을 한 모델의 지표 값에 적용한다. 예측 범위 안의 slot 별 결과를 돌려준다.

    threshold: 지역별 기준값(없으면 leaf.value). months: 특보 적용 기간(월) — 벗어난 시각은 제외.
    """
    limit = leaf.value if threshold is None else threshold
    by_slot = {p.slot: p for p in points}
    out: dict[str, SlotEval] = {}
    for p in points:
        if not in_horizon(p, now, horizon_end):
            continue
        if months and max(p.time, now).month not in months:
            continue
        value: float | None = p.value
        if leaf.consecutive_days > 1:
            value = _sustained(by_slot, p.slot, leaf.consecutive_days, leaf.op)
            if value is None:
                continue
        out[p.slot] = SlotEval(
            time=max(p.time, now),  # 이미 지난 극값은 '지금'으로 표시
            value=value,
            reach=reach_ratio(value, limit, leaf.baseline),
            exceeds=_OPS[leaf.op](value, limit),
        )
    return out


def _combine_all(parts: list[dict[str, SlotEval]]) -> dict[str, SlotEval]:
    """AND 묶음: 같은 slot 에서 모두 만족해야 초과, 도달률은 가장 낮은 쪽."""
    common = set(parts[0])
    for part in parts[1:]:
        common &= set(part)
    out: dict[str, SlotEval] = {}
    for slot in common:
        evals = [part[slot] for part in parts]
        out[slot] = SlotEval(
            time=max(e.time for e in evals),
            value=None,
            reach=min(e.reach for e in evals),
            exceeds=all(e.exceeds for e in evals),
        )
    return out


def _best(slots: dict[str, SlotEval]) -> SlotEval | None:
    """도달률이 가장 높은 slot (같으면 이른 시각)."""
    if not slots:
        return None
    return min(slots.values(), key=lambda e: (-e.reach, e.time))


class _IndicatorCache:
    def __init__(self, thresholds: Thresholds, series: dict[str, ModelSeries], now: datetime):
        self._thresholds = thresholds
        self._series = series
        self._now = now
        self._cache: dict[tuple[str, str], list[Point]] = {}

    def points(self, model_id: str, indicator_key: str) -> list[Point]:
        key = (model_id, indicator_key)
        if key not in self._cache:
            ind = self._thresholds.indicators[indicator_key]
            self._cache[key] = indicator_points(ind, self._thresholds.indicators, self._series[model_id], self._now)
        return self._cache[key]


def _official_for(
    hazard: Hazard, official: RegionOfficial, thresholds: Thresholds
) -> tuple[str | None, str | None, datetime | None]:
    """이 특보(종류·단계)에 해당하는 공식 발표를 찾는다. 반환: (상태, 발표된 단계, 발효 시각)

    - 같은 종류의 같은 단계 이상이 발표되어 있으면 → "active"
    - 같은 종류의 예비특보: 단계가 명시되면 그 단계 이하에, 미상이면 가장 낮은 단계에만 적용 → "preliminary"
    """
    my_rank = thresholds.level_rank(hazard.level)
    best: OfficialWarning | None = None
    for w in official.active:
        if w.type == hazard.type and w.level and thresholds.level_rank(w.level) >= my_rank:
            if best is None or thresholds.level_rank(w.level) > thresholds.level_rank(best.level or w.level):
                best = w
    if best is not None:
        return "active", best.level, best.effective_from

    lowest_rank = min(thresholds.level_rank(h.level) for h in thresholds.hazards if h.type == hazard.type)
    for w in official.preliminary:
        if w.type != hazard.type:
            continue
        if w.level is None:
            if my_rank == lowest_rank:
                return "preliminary", None, None
        elif thresholds.level_rank(w.level) >= my_rank:
            return "preliminary", w.level, None
    return None, None, None


official_for = _official_for  # 실시간 수집(teukbo.live)도 같은 규칙으로 특보 ↔ 공식 발표를 잇는다


def assess_hazard(
    hazard: Hazard,
    cache: _IndicatorCache,
    model_ids: list[str],
    official: RegionOfficial,
    cfg: Config,
    now: datetime,
    region: Region | None = None,
) -> HazardAssessment:
    th, st = cfg.thresholds, cfg.settings
    horizon_end = now + timedelta(hours=st.horizon_hours)
    official_status, official_level, official_from = _official_for(hazard, official, th)

    per_model: dict[str, ModelHazardResult] = {}
    rows: list[CriterionRow] = []
    row_index: dict[tuple[int, int], CriterionRow] = {}
    for gi, crit in enumerate(hazard.criteria):
        leaves = list(crit.items) if isinstance(crit, AllOf) else [crit]
        for li, leaf in enumerate(leaves):
            row = CriterionRow(leaf=leaf, threshold=leaf.threshold_for(region), group=gi, joint=isinstance(crit, AllOf), models={})
            rows.append(row)
            row_index[(gi, li)] = row

    for mid in model_ids:
        merged: list[SlotEval] = []
        for gi, crit in enumerate(hazard.criteria):
            leaves = list(crit.items) if isinstance(crit, AllOf) else [crit]
            parts: list[dict[str, SlotEval]] = []
            for li, leaf in enumerate(leaves):
                slots = evaluate_leaf(
                    leaf, cache.points(mid, leaf.indicator), now, horizon_end, leaf.threshold_for(region), hazard.months
                )
                parts.append(slots)
                best = _best(slots)
                row_index[(gi, li)].models[mid] = (
                    LeafModelResult(value=best.value, time=best.time, reach=best.reach, exceeds=best.exceeds)
                    if best is not None and best.value is not None
                    else None
                )
            combined = _combine_all(parts) if isinstance(crit, AllOf) else parts[0]
            merged.extend(combined.values())
        if not merged:
            continue  # 이 모델에는 이 특보를 판단할 자료가 없다
        peak = min(merged, key=lambda e: (-e.reach, e.time))
        exceed_times = [e.time for e in merged if e.exceeds]
        per_model[mid] = ModelHazardResult(
            reach=peak.reach,
            exceeds=bool(exceed_times),
            first_exceed=min(exceed_times) if exceed_times else None,
            peak_time=peak.time,
        )

    stats: Probability | None = None
    probability: int | None = None
    window_from = window_to = peak_time = None
    peak_model: str | None = None
    if hazard.method == "model" and per_model:
        ordered = [per_model[m] for m in model_ids if m in per_model]
        stats = compute_probability([r.reach for r in ordered], [r.exceeds for r in ordered], st.scoring)
        probability = stats.percent
        firsts = [r.first_exceed for r in ordered if r.first_exceed is not None]
        if firsts:
            window_from, window_to = min(firsts), max(firsts)
        peak_model = max(per_model, key=lambda m: per_model[m].reach)
        peak_time = per_model[peak_model].peak_time

    off_season = False
    if hazard.method == "model" and hazard.months and model_ids:
        off_season = now.month not in hazard.months and horizon_end.month not in hazard.months
        if off_season:
            probability = 0  # 적용 기간이 아니면 '자료 없음'이 아니라 가능성 0%

    if hazard.method == "official_only":
        # 모델로 계산하지 않는 특보(태풍 등): 공식 발표가 없으면 '낮음'으로 둔다.
        model_grade = GRADE_LOW if official.available else GRADE_UNKNOWN
    else:
        model_grade = grade_for(probability, st.grade_caution_min, st.grade_high_min)
    base = GRADE_LOW if model_grade == GRADE_UNKNOWN and official_status else model_grade
    grade = apply_official(base, official_status)

    return HazardAssessment(
        hazard=hazard,
        probability=probability,
        model_grade=model_grade,
        grade=grade,
        stats=stats,
        per_model=per_model,
        window_from=window_from,
        window_to=window_to,
        peak_time=peak_time,
        peak_model=peak_model,
        official_status=official_status,
        official_level=official_level,
        official_from=official_from,
        rows=rows,
        off_season=off_season,
    )


def _extremes(cache: _IndicatorCache, cfg: Config, model_ids: list[str], now: datetime) -> dict[str, dict[str, tuple[float, int]]]:
    """이력 저장용: 모델·지표별 예측 범위 내 극값과 선행시간(h).

    극값의 방향은 그 지표를 쓰는 기준의 방향을 따른다('이하' 기준이면 최저값).
    """
    th = cfg.thresholds
    horizon_end = now + timedelta(hours=cfg.settings.horizon_hours)
    direction: dict[str, str] = {}
    for hz in th.hazards:
        for leaf in hz.leaves():
            direction.setdefault(leaf.indicator, "max" if leaf.op in (">=", ">") else "min")
    out: dict[str, dict[str, tuple[float, int]]] = {}
    for mid in model_ids:
        per: dict[str, tuple[float, int]] = {}
        for key, how in direction.items():
            pts = [p for p in cache.points(mid, key) if in_horizon(p, now, horizon_end)]
            if not pts:
                continue
            p = (max if how == "max" else min)(pts, key=lambda q: q.value)
            lead = int(round((max(p.time, now) - now).total_seconds() / 3600.0))
            per[key] = (round(p.value, 2), lead)
        if per:
            out[mid] = per
    return out


def assess_region(
    region: Region,
    series: dict[str, ModelSeries],
    official: RegionOfficial,
    cfg: Config,
    now: datetime,
) -> RegionAssessment:
    """한 지역의 모든 특보 가능성을 계산한다. series: 모델 id → 예측값."""
    for ms in series.values():
        add_apparent_temp(ms)
    model_ids = [m.id for m in cfg.settings.models if m.id in series]
    cache = _IndicatorCache(cfg.thresholds, series, now)
    hazards = [assess_hazard(h, cache, model_ids, official, cfg, now, region) for h in cfg.thresholds.hazards]
    return RegionAssessment(
        region=region,
        hazards=hazards,
        official=official,
        models_used=model_ids,
        extremes=_extremes(cache, cfg, model_ids, now),
    )
