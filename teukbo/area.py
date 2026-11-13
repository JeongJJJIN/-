"""주변 지역의 예보까지 보는 가능성 보정 (지금은 호우에만 쓴다).

왜
    집중호우는 예보 모델이 비의 양과 위치를 조금씩 어긋나게 잡는다. 그 지역 한 점의 예보가 특보 기준에 닿기를 기다리면
    실제 호우특보의 대부분을 놓친다(과거 검증: 하루 전 포착 71건 중 15건).
    과거 예보를 다시 살펴보니(tools/rain_study.py), 특보 전날 모델들은 기준의 20~65%쯤은 예보하고 있었고,
    '주변 30km 안 지역들의 상위 2개 모델 평균 도달률 가운데 가장 큰 값'이 0.5를 넘는지를 보면 하루 전 포착이 71건 중 46건으로 늘었다.

어떻게
    thresholds.json 의 types.<특보 종류>.area_score 로 켠다.
        radius_km   주변으로 볼 반경(지역의 조회 지점 사이 거리)
        top_models  도달률이 높은 모델 몇 개를 평균할지
        points      [[값, 가능성 %], …] — 값을 이 점들을 잇는 직선으로 가능성(%)으로 바꾼다
        levels      이 보정을 쓸 단계(없으면 전부). 지금은 주의보에만 쓴다 — 경보 단계는 과거 자료로 확인하지 못했다
    이 보정은 가능성을 올리기만 한다: 원래 계산식(teukbo.scoring)의 값이 더 높으면 그대로 둔다.

한계
    많이 잡는 만큼 헛짚는 것도 는다(과거 검증에서 이 기준으로 '주의' 이상이 실제 특보로 이어진 것은 5번에 1번쯤).
    기준값은 가을 두 달(특보가 시작된 날 7일)의 자료로 맞춘 것이라, 다른 계절의 자료가 쌓이면 다시 맞춰야 한다.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .assess import RegionAssessment
from .config import Config, Region
from .scoring import GRADE_LOW, GRADE_UNKNOWN, apply_official, grade_for

_NEIGHBORS: dict[tuple[tuple[str, ...], float], dict[str, list[str]]] = {}


def distance_km(a: Region, b: Region) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b.lon - a.lon) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def neighbors(regions: Sequence[Region], radius_km: float) -> dict[str, list[str]]:
    """지역 id → 반경 안의 지역 id 들(자기 자신 포함)."""
    key = (tuple(r.id for r in regions), float(radius_km))
    if key not in _NEIGHBORS:
        _NEIGHBORS[key] = {a.id: [b.id for b in regions if distance_km(a, b) <= radius_km] for a in regions}
    return _NEIGHBORS[key]


def interpolate(points: Sequence[Sequence[float]], value: float) -> int:
    """[[값, %], …](값이 커지는 순서)을 잇는 직선에서 value 에 해당하는 %. 범위 밖은 양 끝 값."""
    pts = sorted((float(x), float(y)) for x, y in points)
    if not pts:
        return 0
    if value <= pts[0][0]:
        return int(round(pts[0][1]))
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if value <= x1:
            return int(round(y0 + (y1 - y0) * (value - x0) / (x1 - x0))) if x1 > x0 else int(round(y1))
    return int(round(pts[-1][1]))


def model_score(reaches: Sequence[float], top_models: int) -> float | None:
    """모델별 도달률 가운데 높은 top_models 개의 평균. 모델이 하나도 없으면 None."""
    if not reaches:
        return None
    best = sorted(reaches)[-max(1, top_models):]
    return sum(best) / len(best)


def apply_area_scores(assessments: Sequence[RegionAssessment], cfg: Config) -> int:
    """설정된 특보 종류에 대해, 주변 지역의 예보까지 본 값으로 가능성을 올린다. 올린 건수를 돌려준다."""
    th, st = cfg.thresholds, cfg.settings
    changed = 0
    for type_id, info in th.types.items():
        conf: Mapping[str, Any] | None = info.get("area_score") if isinstance(info, Mapping) else None
        if not conf:
            continue
        near = neighbors([ra.region for ra in assessments], float(conf.get("radius_km", 0)))
        top_models = int(conf.get("top_models", 2))
        points = conf.get("points") or []
        names = {ra.region.id: ra.region.name for ra in assessments}
        levels = set(conf.get("levels") or [])
        for hazard in (h for h in th.hazards if h.type == type_id and h.method == "model" and (not levels or h.level in levels)):
            own: dict[str, float] = {}
            target = {}
            for ra in assessments:
                a = next((x for x in ra.hazards if x.hazard.id == hazard.id), None)
                if a is None or a.probability is None or a.off_season or not a.per_model:
                    continue
                score = model_score([r.reach for r in a.per_model.values()], top_models)
                if score is not None:
                    own[ra.region.id] = score
                    target[ra.region.id] = a
            for rid, a in target.items():
                source = max((n for n in near.get(rid, [rid]) if n in own), key=lambda n: own[n], default=None)
                if source is None:
                    continue
                percent = interpolate(points, own[source])
                if percent <= (a.probability or 0):
                    continue
                a.probability = percent
                a.area_value = round(own[source], 2)
                a.area_from = None if source == rid else names.get(source)
                a.model_grade = grade_for(percent, st.grade_caution_min, st.grade_high_min)
                base = GRADE_LOW if a.model_grade == GRADE_UNKNOWN and a.official_status else a.model_grade
                a.grade = apply_official(base, a.official_status)
                changed += 1
    return changed
