"""설정 파일(config/*.json) 로더와 검증.

특보 기준값·지역·계산 파라미터는 모두 JSON 설정에서 읽는다. 코드에는 수치를 두지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

OPS = (">=", ">", "<=", "<")
INDICATOR_KINDS = ("hourly", "rolling_sum", "period_extreme", "period_change")
VARIABLES = ("precip", "snow", "temp", "apparent", "rh", "wind", "gust")


class ConfigError(ValueError):
    """설정 파일 내용이 잘못되었을 때."""


# ---------------------------------------------------------------- 지역


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    lat: float
    lon: float
    nx: int
    ny: int
    zone_codes: tuple[str, ...]  # 이 지역에 해당하는 특보구역 코드(세분 구역 포함)
    zone_names: tuple[str, ...]  # 특보구역 이름(통보문 표기)
    group_codes: tuple[str, ...] = ()  # 상위 구역(도 단위) 코드
    group_names: tuple[str, ...] = ()  # 상위 구역 이름 (예: 전라남도)
    province: str = ""
    params: dict[str, float] = field(default_factory=dict, compare=False, hash=False)  # 지역별 기준값


# ---------------------------------------------------------------- 특보 기준


@dataclass(frozen=True)
class Indicator:
    """예측값에서 뽑아내는 지표 정의 (예: 3시간 누적 강수량)."""

    key: str
    kind: str
    label: str
    unit: str
    variable: str | None = None
    window_hours: int | None = None  # rolling_sum
    stat: str | None = None  # period_extreme: "max" | "min"
    start_hour: int | None = None  # period_extreme: 이 시각 초과 ~
    end_hour: int | None = None  # ~ 이 시각 이하 (start 이하이면 다음 날)
    of: str | None = None  # period_change: 대상 period_extreme 지표 key
    decimals: int = 1


@dataclass(frozen=True)
class Leaf:
    """단일 기준: 지표 값이 op 기준값을 만족하는가."""

    indicator: str
    op: str
    value: float
    baseline: float = 0.0  # 도달률 계산의 0% 지점
    consecutive_days: int = 1
    region_param: str | None = None  # 지역별로 기준값이 다를 때 regions.json params 의 키

    def threshold_for(self, region: "Region | None") -> float:
        if self.region_param and region is not None and self.region_param in region.params:
            return float(region.params[self.region_param])
        return self.value


@dataclass(frozen=True)
class AllOf:
    """여러 기준을 동시에 만족해야 하는 묶음(AND)."""

    items: tuple[Leaf, ...]


Criterion = Union[Leaf, AllOf]


@dataclass(frozen=True)
class Hazard:
    """특보 한 종류·한 단계 (예: 호우주의보)."""

    id: str
    type: str
    level: str
    name: str
    method: str  # "model" | "official_only"
    criteria: tuple[Criterion, ...]  # 하나라도 만족하면 기준 초과(OR)
    official_text: str = ""
    note: str = ""
    months: tuple[int, ...] = ()  # 적용 기간(월). 비어 있으면 연중

    def leaves(self) -> list[Leaf]:
        out: list[Leaf] = []
        for c in self.criteria:
            out.extend(c.items if isinstance(c, AllOf) else [c])
        return out


@dataclass(frozen=True)
class Thresholds:
    version: str
    source: str
    checked_at: str
    types: dict[str, dict[str, Any]]  # type id → {"name": "호우", ...}
    levels: dict[str, dict[str, Any]]  # level id → {"name": "주의보", "rank": 1}
    indicators: dict[str, Indicator]
    hazards: tuple[Hazard, ...]

    def hazard(self, hazard_id: str) -> Hazard:
        for h in self.hazards:
            if h.id == hazard_id:
                return h
        raise KeyError(hazard_id)

    def level_rank(self, level: str) -> int:
        return int(self.levels[level]["rank"])

    def type_name(self, type_id: str) -> str:
        return str(self.types[type_id]["name"])

    def level_name(self, level: str) -> str:
        return str(self.levels[level]["name"])


# ---------------------------------------------------------------- 계산 설정


@dataclass(frozen=True)
class ModelDef:
    id: str
    name: str
    source: str  # "open_meteo" | "kma_forecast"
    short: str = ""


@dataclass(frozen=True)
class ScoringParams:
    weight_exceed_ratio: float
    weight_max_reach: float
    weight_mean_reach: float
    reach_floor: float


@dataclass(frozen=True)
class Settings:
    horizon_hours: int
    past_hours: int
    forecast_hours: int
    models: tuple[ModelDef, ...]
    scoring: ScoringParams
    grade_caution_min: float
    grade_high_min: float
    accuracy: dict[str, Any]
    http: dict[str, Any]
    stale_after_hours: float
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def model(self, model_id: str) -> ModelDef:
        for m in self.models:
            if m.id == model_id:
                return m
        raise KeyError(model_id)


@dataclass(frozen=True)
class Config:
    regions: tuple[Region, ...]
    thresholds: Thresholds
    settings: Settings


# ---------------------------------------------------------------- 로더


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"설정 파일이 없습니다: {path}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"JSON 형식 오류: {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"최상위가 객체가 아닙니다: {path}")
    return data


def _req(obj: dict[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise ConfigError(f"{where}: '{key}' 항목이 필요합니다")
    return obj[key]


def load_regions(path: Path | None = None) -> tuple[Region, ...]:
    data = _read(path or CONFIG_DIR / "regions.json")
    groups = data.get("zone_groups", {})
    regions: list[Region] = []
    seen: set[str] = set()
    for i, r in enumerate(_req(data, "regions", "regions.json")):
        where = f"regions.json regions[{i}]"
        rid = str(_req(r, "id", where))
        if rid in seen:
            raise ConfigError(f"{where}: id '{rid}' 중복")
        seen.add(rid)
        zones = r.get("warning_zones") or []
        if not zones:
            raise ConfigError(f"{where}: warning_zones(특보구역)가 필요합니다")
        group_id = r.get("group")
        if group_id is not None and group_id not in groups:
            raise ConfigError(f"{where}: zone_groups 에 없는 group '{group_id}'")
        group = groups.get(group_id, {})
        lat, lon = float(_req(r, "lat", where)), float(_req(r, "lon", where))
        if not (32.0 <= lat <= 39.5 and 124.0 <= lon <= 132.0):
            raise ConfigError(f"{where}: 위경도가 한반도 범위를 벗어났습니다 ({lat}, {lon})")
        regions.append(
            Region(
                id=rid,
                name=str(_req(r, "name", where)),
                lat=lat,
                lon=lon,
                nx=int(_req(r, "nx", where)),
                ny=int(_req(r, "ny", where)),
                zone_codes=tuple(str(z["code"]) for z in zones if z.get("code")),
                zone_names=tuple(str(_req(z, "name", where)) for z in zones),
                group_codes=tuple(str(c) for c in ([group["code"]] if group.get("code") else [])),
                group_names=tuple(str(n) for n in group.get("names", [])),
                province=str(group.get("name", r.get("province", ""))),
                params={str(k): float(v) for k, v in (r.get("params") or {}).items()},
            )
        )
    if not regions:
        raise ConfigError("regions.json: 지역이 하나도 없습니다")
    return tuple(regions)


def _parse_indicator(key: str, d: dict[str, Any]) -> Indicator:
    where = f"thresholds.json indicators.{key}"
    kind = _req(d, "kind", where)
    if kind not in INDICATOR_KINDS:
        raise ConfigError(f"{where}: kind 는 {INDICATOR_KINDS} 중 하나여야 합니다")
    ind = Indicator(
        key=key,
        kind=kind,
        label=str(_req(d, "label", where)),
        unit=str(d.get("unit", "")),
        variable=d.get("variable"),
        window_hours=d.get("window_hours"),
        stat=d.get("stat"),
        start_hour=d.get("start_hour"),
        end_hour=d.get("end_hour"),
        of=d.get("of"),
        decimals=int(d.get("decimals", 1)),
    )
    if kind in ("hourly", "rolling_sum", "period_extreme") and ind.variable not in VARIABLES:
        raise ConfigError(f"{where}: variable 은 {VARIABLES} 중 하나여야 합니다")
    if kind == "rolling_sum" and not (isinstance(ind.window_hours, int) and ind.window_hours >= 1):
        raise ConfigError(f"{where}: window_hours(1 이상 정수)가 필요합니다")
    if kind == "period_extreme":
        if ind.stat not in ("max", "min"):
            raise ConfigError(f"{where}: stat 은 'max' 또는 'min'")
        for hk in ("start_hour", "end_hour"):
            hv = getattr(ind, hk)
            if not (isinstance(hv, int) and 0 <= hv <= 24):
                raise ConfigError(f"{where}: {hk} 는 0~24 정수")
    if kind == "period_change" and not ind.of:
        raise ConfigError(f"{where}: of(대상 지표 key)가 필요합니다")
    return ind


def _parse_leaf(d: dict[str, Any], where: str, indicators: dict[str, Indicator]) -> Leaf:
    key = _req(d, "indicator", where)
    if key not in indicators:
        raise ConfigError(f"{where}: 정의되지 않은 지표 '{key}'")
    op = _req(d, "op", where)
    if op not in OPS:
        raise ConfigError(f"{where}: op 는 {OPS} 중 하나")
    value = float(_req(d, "value", where))
    baseline = float(d.get("baseline", 0.0))
    if value == baseline:
        raise ConfigError(f"{where}: value 와 baseline 이 같으면 도달률을 계산할 수 없습니다")
    if op in (">=", ">") and value < baseline or op in ("<=", "<") and value > baseline:
        raise ConfigError(f"{where}: baseline 이 기준값의 반대편에 있어야 합니다 (op {op}, value {value}, baseline {baseline})")
    days = int(d.get("consecutive_days", 1))
    if days < 1:
        raise ConfigError(f"{where}: consecutive_days 는 1 이상")
    if days > 1 and indicators[key].kind not in ("period_extreme", "period_change"):
        raise ConfigError(f"{where}: consecutive_days 는 일 단위 지표에만 쓸 수 있습니다")
    region_param = d.get("region_param")
    return Leaf(indicator=key, op=op, value=value, baseline=baseline, consecutive_days=days, region_param=str(region_param) if region_param else None)


def load_thresholds(path: Path | None = None) -> Thresholds:
    data = _read(path or CONFIG_DIR / "thresholds.json")
    indicators = {k: _parse_indicator(k, v) for k, v in _req(data, "indicators", "thresholds.json").items()}
    for ind in indicators.values():
        if ind.kind == "period_change":
            target = indicators.get(ind.of or "")
            if target is None or target.kind != "period_extreme":
                raise ConfigError(f"thresholds.json indicators.{ind.key}: of 는 period_extreme 지표여야 합니다")

    types = _req(data, "types", "thresholds.json")
    levels = _req(data, "levels", "thresholds.json")
    hazards: list[Hazard] = []
    seen: set[str] = set()
    for i, h in enumerate(_req(data, "hazards", "thresholds.json")):
        where = f"thresholds.json hazards[{i}]"
        hid = str(_req(h, "id", where))
        if hid in seen:
            raise ConfigError(f"{where}: id '{hid}' 중복")
        seen.add(hid)
        htype, level = _req(h, "type", where), _req(h, "level", where)
        if htype not in types:
            raise ConfigError(f"{where}: 정의되지 않은 type '{htype}'")
        if level not in levels:
            raise ConfigError(f"{where}: 정의되지 않은 level '{level}'")
        method = h.get("method", "model")
        if method not in ("model", "official_only"):
            raise ConfigError(f"{where}: method 는 'model' 또는 'official_only'")
        criteria: list[Criterion] = []
        for j, c in enumerate(h.get("criteria", [])):
            cw = f"{where}.criteria[{j}]"
            if "all" in c:
                items = tuple(_parse_leaf(x, f"{cw}.all[{k}]", indicators) for k, x in enumerate(c["all"]))
                if len(items) < 2:
                    raise ConfigError(f"{cw}: all 은 기준 2개 이상")
                kinds = {indicators[x.indicator].kind in ("period_extreme", "period_change") for x in items}
                if len(kinds) != 1:
                    raise ConfigError(f"{cw}: all 안에는 일 단위 지표끼리 또는 시간 단위 지표끼리만 묶을 수 있습니다")
                criteria.append(AllOf(items))
            else:
                criteria.append(_parse_leaf(c, cw, indicators))
        if method == "model" and not criteria:
            raise ConfigError(f"{where}: method 'model' 에는 criteria 가 필요합니다")
        months = tuple(int(m) for m in h.get("months", []))
        if any(not 1 <= m <= 12 for m in months):
            raise ConfigError(f"{where}: months 는 1~12")
        hazards.append(
            Hazard(
                id=hid,
                type=htype,
                level=level,
                name=str(_req(h, "name", where)),
                method=method,
                criteria=tuple(criteria),
                official_text=str(h.get("official_text", "")),
                note=str(h.get("note", "")),
                months=months,
            )
        )
    if not hazards:
        raise ConfigError("thresholds.json: hazards 가 비어 있습니다")
    return Thresholds(
        version=str(data.get("version", "")),
        source=str(data.get("source", "")),
        checked_at=str(data.get("checked_at", "")),
        types=types,
        levels=levels,
        indicators=indicators,
        hazards=tuple(hazards),
    )


def load_settings(path: Path | None = None) -> Settings:
    data = _read(path or CONFIG_DIR / "settings.json")
    where = "settings.json"
    models = tuple(
        ModelDef(id=str(_req(m, "id", where)), name=str(_req(m, "name", where)), source=str(_req(m, "source", where)), short=str(m.get("short", "")))
        for m in _req(data, "models", where)
    )
    if not models:
        raise ConfigError("settings.json: models 가 비어 있습니다")
    for m in models:
        if m.source not in ("open_meteo", "kma_forecast"):
            raise ConfigError(f"settings.json: 모델 '{m.id}' 의 source 가 올바르지 않습니다")
    sc = _req(data, "scoring", where)
    scoring = ScoringParams(
        weight_exceed_ratio=float(_req(sc, "weight_exceed_ratio", where)),
        weight_max_reach=float(_req(sc, "weight_max_reach", where)),
        weight_mean_reach=float(_req(sc, "weight_mean_reach", where)),
        reach_floor=float(_req(sc, "reach_floor", where)),
    )
    total = scoring.weight_exceed_ratio + scoring.weight_max_reach + scoring.weight_mean_reach
    if abs(total - 1.0) > 1e-9:
        raise ConfigError(f"settings.json scoring: 가중치 합이 1이어야 합니다 (현재 {total})")
    if not 0.0 <= scoring.reach_floor < 1.0:
        raise ConfigError("settings.json scoring: reach_floor 는 0 이상 1 미만")
    grades = _req(data, "grades", where)
    caution, high = float(_req(grades, "caution_min", where)), float(_req(grades, "high_min", where))
    if not 0 < caution < high <= 100:
        raise ConfigError("settings.json grades: 0 < caution_min < high_min <= 100 이어야 합니다")
    fetch = _req(data, "fetch", where)
    horizon = int(_req(data, "horizon_hours", where))
    forecast_hours = int(_req(fetch, "forecast_hours", where))
    if forecast_hours < horizon:
        raise ConfigError("settings.json: fetch.forecast_hours 는 horizon_hours 이상이어야 합니다")
    return Settings(
        horizon_hours=horizon,
        past_hours=int(_req(fetch, "past_hours", where)),
        forecast_hours=forecast_hours,
        models=models,
        scoring=scoring,
        grade_caution_min=caution,
        grade_high_min=high,
        accuracy=dict(data.get("accuracy", {})),
        http=dict(data.get("http", {})),
        stale_after_hours=float(data.get("stale_after_hours", 7)),
        raw=data,
    )


def load_config(config_dir: Path | None = None) -> Config:
    d = config_dir or CONFIG_DIR
    return Config(
        regions=load_regions(d / "regions.json"),
        thresholds=load_thresholds(d / "thresholds.json"),
        settings=load_settings(d / "settings.json"),
    )
