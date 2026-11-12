"""기상청 단기예보(getVilageFcst) 수집.

1순위: 공공데이터포털  https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst  (serviceKey)
2순위: 기상청 API허브  https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst  (authKey)
두 곳의 요청 변수와 응답(JSON) 형식은 같다. 공공데이터포털이 막히거나 실패하면 API허브로 넘어간다.

근거: 「기상청41_단기예보 조회서비스_오픈API활용가이드」(2026-10 판)
- 발표시각(base_time) 02·05·08·11·14·17·20·23시, 각 +10분 이후 제공
- 1시간 간격 자료는 모레(또는 글피)까지, 그 뒤는 3시간 간격의 정성 코드 → 1시간 간격 구간만 사용
- PCP(1시간 강수량)·SNO(1시간 신적설)는 범주 문자열 ("강수없음", "1mm 미만", "6.2mm", "30.0~50.0mm", "50.0mm 이상" …)
- +900 이상, -900 이하는 결측
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
import re
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from ..config import Region
from ..http import FetchError, get_json
from .datagokr import NoData, extract_items
from ..indicators import HOUR, ModelSeries
from ..timeutil import KST

log = logging.getLogger("teukbo.kma_forecast")

WORKERS = 6  # 동시에 받는 격자 수

DATA_GO_KR_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
APIHUB_URL = "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst"

BASE_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)
PAGE_SIZE = 1000
MAX_PAGES = 4

# 단기예보 범주 문자열을 수치로 바꿀 때의 대푯값 (settings.json 의 kma_forecast 로 덮어쓸 수 있다)
DEFAULT_CONVENTIONS = {
    "pcp_under_1mm": 0.5,  # "1mm 미만" (0.1~1.0mm)
    "pcp_30_to_50mm": 40.0,  # "30.0~50.0mm"
    "pcp_over_50mm": 50.0,  # "50.0mm 이상" — 상한을 알 수 없어 하한값을 쓴다(과소 추정 가능)
    "sno_under_0_5cm": 0.3,  # "0.5cm 미만" (0.1~0.5cm)
    "sno_over_5cm": 5.0,  # "5.0cm 이상" — 하한값
    "available_after_minutes": 15,  # 발표시각 뒤 이만큼 지나야 조회 (공식 제공 시각은 +10분)
}

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def candidate_bases(now: datetime, wait_minutes: float, count: int = 3) -> list[tuple[str, str]]:
    """지금 조회할 수 있는 발표시각을 최신순으로. [(base_date, base_time), …]"""
    ready = now.astimezone(KST) - timedelta(minutes=wait_minutes)
    out: list[tuple[str, str]] = []
    day = ready.replace(minute=0, second=0, microsecond=0)
    probe = day
    while len(out) < count:
        if probe.hour in BASE_HOURS:
            out.append((probe.strftime("%Y%m%d"), probe.strftime("%H00")))
        probe -= HOUR
    return out


def _to_float(text: Any) -> float | None:
    """수치 문자열 → float. 결측(±900 이상)·빈 값은 None."""
    if text is None:
        return None
    m = _NUMBER.search(str(text))
    if not m:
        return None
    value = float(m.group())
    if value >= 900.0 or value <= -900.0:
        return None
    return value


def parse_pcp(text: Any, conv: Mapping[str, float] = DEFAULT_CONVENTIONS) -> float | None:
    """PCP(1시간 강수량) 범주 문자열 → mm."""
    if text is None:
        return None
    s = str(text).replace(" ", "")
    if s in ("", "-", "0", "null") or "강수없음" in s:
        return 0.0
    if "미만" in s:
        return float(conv["pcp_under_1mm"])
    if "~" in s:
        return float(conv["pcp_30_to_50mm"])
    if "이상" in s:
        return float(conv["pcp_over_50mm"])
    return _to_float(s)


def parse_sno(text: Any, conv: Mapping[str, float] = DEFAULT_CONVENTIONS) -> float | None:
    """SNO(1시간 신적설) 범주 문자열 → cm."""
    if text is None:
        return None
    s = str(text).replace(" ", "")
    if s in ("", "-", "0", "null") or "적설없음" in s:
        return 0.0
    if "미만" in s:
        return float(conv["sno_under_0_5cm"])
    if "이상" in s:
        return float(conv["sno_over_5cm"])
    return _to_float(s)


def parse_items(items: Sequence[Mapping[str, Any]], model_id: str, conv: Mapping[str, float] = DEFAULT_CONVENTIONS) -> ModelSeries | None:
    """getVilageFcst 의 item 목록 → ModelSeries (1시간 간격 구간만)."""
    slots: dict[datetime, dict[str, Any]] = {}
    for it in items:
        try:
            t = datetime.strptime(f"{it['fcstDate']}{str(it['fcstTime']).zfill(4)}", "%Y%m%d%H%M").replace(tzinfo=KST)
        except (KeyError, ValueError):
            continue
        slots.setdefault(t, {})[str(it.get("category"))] = it.get("fcstValue")
    if not slots:
        return None

    ordered = sorted(slots)
    times = [ordered[0]]
    for t in ordered[1:]:
        if t - times[-1] != HOUR:
            break  # 여기서부터는 3시간 간격(정성 코드) 구간
        times.append(t)

    values: dict[str, list[float | None]] = {
        "temp": [_to_float(slots[t].get("TMP")) for t in times],
        "rh": [_to_float(slots[t].get("REH")) for t in times],
        "wind": [_to_float(slots[t].get("WSD")) for t in times],
        "precip": [parse_pcp(slots[t].get("PCP"), conv) for t in times],
        "snow": [parse_sno(slots[t].get("SNO"), conv) for t in times],
    }
    values = {k: v for k, v in values.items() if any(x is not None for x in v)}
    if not values:
        return None
    return ModelSeries(model_id=model_id, times=times, values=values)


def _fetch_grid(url: str, key_param: str, key: str, base: tuple[str, str], nx: int, ny: int, http_opts: Mapping[str, Any], retries: int) -> list[Mapping[str, Any]]:
    collected: list[Mapping[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        payload = get_json(
            url,
            {
                key_param: key,
                "pageNo": page,
                "numOfRows": PAGE_SIZE,
                "dataType": "JSON",
                "base_date": base[0],
                "base_time": base[1],
                "nx": nx,
                "ny": ny,
            },
            timeout=float(http_opts.get("timeout_seconds", 30)),
            retries=retries,
            backoff=float(http_opts.get("backoff_seconds", 3)),
            label=f"단기예보 {nx},{ny}",
        )
        items, total = extract_items(payload, "단기예보")
        collected.extend(items)
        if len(collected) >= total or len(items) < PAGE_SIZE:
            break
    return collected


def _fetch_provider(
    url: str,
    key_param: str,
    key: str,
    regions: Sequence[Region],
    model_id: str,
    now: datetime,
    http_opts: Mapping[str, Any],
    conv: Mapping[str, float],
) -> tuple[dict[str, ModelSeries], str]:
    """한 제공처에서 전 지역(격자 중복 제거)을 받는다. 반환: (지역 id → ModelSeries, 사용한 발표시각)"""
    grids: dict[tuple[int, int], list[Region]] = {}
    for r in regions:
        grids.setdefault((r.nx, r.ny), []).append(r)
    grid_list = list(grids)
    retries = int(http_opts.get("retries", 3))

    # 첫 격자로 쓸 수 있는 발표시각을 정한다(최신 자료가 아직 없으면 직전 발표로). 재시도는 짧게.
    bases = candidate_bases(now, float(conv["available_after_minutes"]))
    base: tuple[str, str] | None = None
    first_items: list[Mapping[str, Any]] = []
    last_error: FetchError | None = None
    for cand in bases:
        try:
            first_items = _fetch_grid(url, key_param, key, cand, *grid_list[0], http_opts, retries=min(retries, 1))
            base = cand
            break
        except NoData as e:
            last_error = e
            log.info("단기예보 %s %s 발표분 없음 — 직전 발표시각으로 재시도", *cand)
    if base is None:
        raise last_error or FetchError("단기예보: 조회 가능한 발표시각이 없습니다")

    # 나머지 격자는 몇 개씩 동시에 받는다(전국은 격자가 200곳이 넘는다).
    # 계속 실패하면 오래 붙잡지 않고 다음 제공처로 넘어간다 — 한도를 넘는 순간 남은 요청은 보내지 않는다.
    limit = max(2, len(grid_list) // 4)
    stop = threading.Event()

    def one(grid: tuple[int, int]) -> tuple[tuple[int, int], ModelSeries | None, FetchError | None]:
        if stop.is_set():
            return grid, None, FetchError("중단")
        try:
            return grid, parse_items(_fetch_grid(url, key_param, key, base, *grid, http_opts, retries=min(retries, 2)), model_id, conv), None
        except FetchError as e:
            return grid, None, e

    out: dict[str, ModelSeries] = {}
    failed = 0
    last: FetchError | None = None
    try:
        first: tuple[tuple[int, int], ModelSeries | None, FetchError | None] = (grid_list[0], parse_items(first_items, model_id, conv), None)
    except FetchError as e:
        first = (grid_list[0], None, e)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for grid, series, error in [first, *pool.map(one, grid_list[1:])]:
            if error is not None:
                if stop.is_set():
                    continue
                failed += 1
                last = error
                log.warning("단기예보 격자 %s 실패: %s", grid, error)
                if failed > limit:
                    stop.set()
                continue
            if series is None:
                failed += 1
                continue
            for r in grids[grid]:
                out[r.id] = ModelSeries(model_id=model_id, times=series.times, values=series.values)
    if stop.is_set():
        raise FetchError(f"단기예보: 격자 {failed}곳 실패로 중단 (마지막 오류: {last})") from last
    if not out:
        raise FetchError("단기예보: 사용할 수 있는 자료가 없습니다")
    return out, f"{base[0]} {base[1]}"


def fetch(
    regions: Sequence[Region],
    model_id: str,
    now: datetime,
    *,
    data_go_kr_key: str | None,
    apihub_key: str | None,
    http_opts: Mapping[str, Any] | None = None,
    conventions: Mapping[str, float] | None = None,
) -> tuple[dict[str, ModelSeries], dict[str, Any]]:
    """단기예보를 받아 온다. 반환: (지역 id → ModelSeries, 상태 정보).

    인증키가 하나도 없으면 configured=False 로 조용히 건너뛴다(다른 출처만으로 계산).
    """
    opts = http_opts or {}
    conv = {**DEFAULT_CONVENTIONS, **(conventions or {})}
    providers: list[tuple[str, str, str, str]] = []
    if data_go_kr_key:
        providers.append(("공공데이터포털", DATA_GO_KR_URL, "serviceKey", data_go_kr_key))
    if apihub_key:
        providers.append(("기상청 API허브", APIHUB_URL, "authKey", apihub_key))
    if not providers:
        return {}, {"ok": False, "configured": False, "provider": None, "error": "인증키 미설정"}

    errors: list[str] = []
    for name, url, key_param, key in providers:
        try:
            data, base = _fetch_provider(url, key_param, key, regions, model_id, now, opts, conv)
            log.info("단기예보(%s): %d개 지역, 발표 %s", name, len(data), base)
            return data, {"ok": True, "configured": True, "provider": name, "base": base, "regions": len(data), "error": None}
        except FetchError as e:
            errors.append(f"{name}: {e}")
            log.error("단기예보(%s) 실패: %s", name, e)
    return {}, {"ok": False, "configured": True, "provider": None, "error": " / ".join(errors)}
