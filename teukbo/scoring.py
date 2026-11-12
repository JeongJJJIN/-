"""가능성(%) 계산식과 등급 판정.

가능성 계산식은 compute_probability() 한 함수에만 있다. 식을 바꾸려면 이 함수만 고치면 되고,
가중치·구간은 config/settings.json 의 scoring, grades 에서 읽는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .config import ScoringParams

GRADE_LOW = "low"  # 낮음
GRADE_CAUTION = "caution"  # 주의
GRADE_HIGH = "high"  # 높음
GRADE_ACTIVE = "active"  # 발효 중
GRADE_UNKNOWN = "unknown"  # 자료 없음

GRADE_RANK = {GRADE_UNKNOWN: -1, GRADE_LOW: 0, GRADE_CAUTION: 1, GRADE_HIGH: 2, GRADE_ACTIVE: 3}


@dataclass(frozen=True)
class Probability:
    percent: int  # 가능성(%) 0~100
    reach_max: float  # 모델 중 최대 도달률 (1.0 = 기준값 도달)
    reach_mean: float  # 모델 평균 도달률
    exceed_ratio: float  # 기준 초과 모델 수 / 전체 모델 수
    models_total: int
    models_exceeding: int


def reach_ratio(value: float, threshold: float, baseline: float = 0.0) -> float:
    """도달률 = (예측값 − 바닥값) / (기준값 − 바닥값). 바닥값 0이면 '예측값 / 기준값'.

    기온처럼 0이 의미 없는 지표나 '이하' 기준(한파)에서도 0~1 범위가 같은 뜻을 갖도록
    기준별 바닥값(baseline)을 둔다. 음수는 0으로 자른다(상한은 두지 않는다).
    """
    ratio = (value - baseline) / (threshold - baseline)
    return ratio if ratio > 0.0 else 0.0


def _reach_score(reach: float, floor: float) -> float:
    """도달률을 0~1 점수로 환산: floor 이하 0점, 1.0(기준 도달) 이상 1점, 그 사이는 직선."""
    capped = min(reach, 1.0)
    if capped <= floor:
        return 0.0
    return (capped - floor) / (1.0 - floor)


def compute_probability(reaches: Sequence[float], exceeds: Sequence[bool], params: ScoringParams) -> Probability:
    """모델별 도달률과 기준 초과 여부를 조합해 가능성(%)을 계산한다. (1단계: 규칙 기반)

        가능성 = 100 × ( w_초과 × 초과 모델 비율
                       + w_최대 × 점수(최대 도달률)
                       + w_평균 × 점수(평균 도달률) )

    - 초과 모델 비율: 기준을 넘긴 모델 수 / 자료가 있는 전체 모델 수
    - 점수(x): 도달률 reach_floor 이하는 0, 1.0 이상은 1, 그 사이는 직선
    - 평균 도달률은 모델별 도달률을 1.0에서 자른 뒤 평균 (한 모델의 극단값이 평균을 끌어올리지 않게)

    모든 모델이 기준을 넘기면 100%, 어느 모델도 reach_floor 에 못 미치면 0%.
    """
    if len(reaches) != len(exceeds):
        raise ValueError("reaches 와 exceeds 의 길이가 다릅니다")
    n = len(reaches)
    if n == 0:
        raise ValueError("모델이 하나 이상 필요합니다")

    n_exceed = sum(1 for e in exceeds if e)
    exceed_ratio = n_exceed / n
    reach_max = max(reaches)
    reach_mean = sum(reaches) / n
    capped_mean = sum(min(r, 1.0) for r in reaches) / n

    score = (
        params.weight_exceed_ratio * exceed_ratio
        + params.weight_max_reach * _reach_score(reach_max, params.reach_floor)
        + params.weight_mean_reach * _reach_score(capped_mean, params.reach_floor)
    )
    percent = int(round(100.0 * max(0.0, min(1.0, score))))
    return Probability(
        percent=percent,
        reach_max=reach_max,
        reach_mean=reach_mean,
        exceed_ratio=exceed_ratio,
        models_total=n,
        models_exceeding=n_exceed,
    )


def grade_for(percent: int | None, caution_min: float, high_min: float) -> str:
    """가능성(%) → 모델 계산 등급 (낮음/주의/높음)."""
    if percent is None:
        return GRADE_UNKNOWN
    if percent >= high_min:
        return GRADE_HIGH
    if percent >= caution_min:
        return GRADE_CAUTION
    return GRADE_LOW


def apply_official(model_grade: str, official_status: str | None) -> str:
    """기상청 공식 발표를 반영한 최종 등급.

    발효 중이면 '발효 중', 예비특보가 있으면 최소 '높음'. 공식 발표가 계산보다 항상 우선한다.
    """
    if official_status == "active":
        return GRADE_ACTIVE
    if official_status == "preliminary":
        return GRADE_HIGH if GRADE_RANK[model_grade] < GRADE_RANK[GRADE_HIGH] else model_grade
    return model_grade


def max_grade(a: str, b: str) -> str:
    return a if GRADE_RANK[a] >= GRADE_RANK[b] else b
