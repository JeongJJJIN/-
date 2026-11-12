"""Open-Meteo Forecast API 수집 (https://open-meteo.com/en/docs).

- 무료 API(비상업적 이용, CC BY 4.0, 출처 표기 필요). 인증키 없음.
- 모델 하나당 요청 한 번(전 지역을 쉼표로 묶어 한꺼번에). 한 모델이 실패해도 나머지는 계속 쓴다.
- 요청 한도는 접속 IP 기준(하루 10,000회 등)이라, 공용 IP 를 쓰는 GitHub Actions 에서는
  내 사용량과 무관하게 429 가 날 수 있다. 그 경우 그 모델은 이번 회차에서 빠진다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Sequence

from ..config import ModelDef, Region
from ..http import FetchError, get_json
from ..indicators import ModelSeries
from ..timeutil import KST

log = logging.getLogger("teukbo.open_meteo")

ENDPOINT = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo 시간별 변수명 → 내부 표준 변수명
# precipitation·snowfall 은 '직전 1시간 합', wind_gusts_10m 은 '직전 1시간 최대'.
HOURLY_VARS = {
    "precipitation": "precip",  # mm
    "snowfall": "snow",  # cm
    "temperature_2m": "temp",  # ℃
    "relative_humidity_2m": "rh",  # %
    "wind_speed_10m": "wind",  # m/s (wind_speed_unit=ms)
    "wind_gusts_10m": "gust",  # m/s
}


def build_params(regions: Sequence[Region], model_id: str, past_hours: int, forecast_hours: int) -> dict[str, Any]:
    return {
        "latitude": ",".join(f"{r.lat:.4f}" for r in regions),
        "longitude": ",".join(f"{r.lon:.4f}" for r in regions),
        "hourly": ",".join(HOURLY_VARS),
        "models": model_id,
        "wind_speed_unit": "ms",
        "timezone": "Asia/Seoul",
        "past_hours": past_hours,
        "forecast_hours": forecast_hours,
    }


def parse_response(payload: Any, regions: Sequence[Region], model_id: str) -> dict[str, ModelSeries]:
    """응답 JSON → 지역 id 별 ModelSeries.

    지점이 여러 개면 입력 순서대로의 배열, 하나면 객체 하나가 온다.
    변수 키는 모델을 하나만 요청하면 접미사 없이, 여러 개면 '<변수>_<모델 id>' 로 온다(둘 다 처리).
    모델에 없는 변수는 전부 null 인 배열로 온다.
    """
    if isinstance(payload, dict):
        if payload.get("error"):
            raise FetchError(f"open-meteo[{model_id}]: {payload.get('reason', '알 수 없는 오류')}")
        payload = [payload]
    if not isinstance(payload, list) or len(payload) != len(regions):
        raise FetchError(f"open-meteo[{model_id}]: 응답 지점 수가 요청과 다릅니다")

    out: dict[str, ModelSeries] = {}
    for region, loc in zip(regions, payload):
        hourly = (loc or {}).get("hourly") or {}
        raw_times = hourly.get("time") or []
        if not raw_times:
            continue
        times = [datetime.fromisoformat(t).replace(tzinfo=KST) for t in raw_times]
        values: dict[str, list[float | None]] = {}
        for om_name, std_name in HOURLY_VARS.items():
            series = hourly.get(f"{om_name}_{model_id}", hourly.get(om_name))
            if not isinstance(series, list) or len(series) != len(times):
                continue
            if all(v is None for v in series):
                continue  # 이 모델이 제공하지 않는 변수
            values[std_name] = [float(v) if v is not None else None for v in series]
        if values:
            out[region.id] = ModelSeries(model_id=model_id, times=times, values=values)
    return out


def fetch_model(
    regions: Sequence[Region],
    model: ModelDef,
    past_hours: int,
    forecast_hours: int,
    http_opts: dict[str, Any] | None = None,
) -> dict[str, ModelSeries]:
    """한 모델의 전 지역 예측을 받아 온다. 실패하면 FetchError."""
    opts = http_opts or {}
    payload = get_json(
        ENDPOINT,
        build_params(regions, model.id, past_hours, forecast_hours),
        timeout=float(opts.get("timeout_seconds", 30)),
        retries=int(opts.get("retries", 3)),
        backoff=float(opts.get("backoff_seconds", 3)),
        label=f"open-meteo:{model.id}",
    )
    result = parse_response(payload, regions, model.id)
    if not result:
        raise FetchError(f"open-meteo[{model.id}]: 사용할 수 있는 자료가 없습니다")
    return result


def fetch_all(
    regions: Sequence[Region],
    models: Sequence[ModelDef],
    past_hours: int,
    forecast_hours: int,
    http_opts: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, ModelSeries]], dict[str, str]]:
    """여러 모델을 차례로 받는다(무료 API 는 동시 요청을 하나씩만 처리).

    반환: (모델 id → 지역 id → ModelSeries, 모델 id → 오류 메시지)
    """
    data: dict[str, dict[str, ModelSeries]] = {}
    errors: dict[str, str] = {}
    for model in models:
        try:
            data[model.id] = fetch_model(regions, model, past_hours, forecast_hours, http_opts)
            log.info("open-meteo %s: %d개 지역 수신", model.id, len(data[model.id]))
        except FetchError as e:
            errors[model.id] = str(e)
            log.error("open-meteo %s 수집 실패: %s", model.id, e)
    return data, errors
