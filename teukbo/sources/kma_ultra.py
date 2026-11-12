"""기상청 초단기실황(getUltraSrtNcst)·초단기예보(getUltraSrtFcst) — '지금 날씨'와 '앞으로 6시간'.

1순위: 공공데이터포털  https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/…  (serviceKey, 단기예보 조회서비스와 같은 활용신청)
2순위: 기상청 API허브  https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/…  (authKey)

근거: 「기상청41_단기예보 조회서비스_오픈API활용가이드」
- 초단기실황: 매시 정시 기준(base_time HH00), 매시 10분 이후 제공, 10분마다 최신 값으로 갱신
  T1H 기온(℃) · RN1 1시간 강수량(mm) · REH 습도(%) · PTY 강수형태 · VEC 풍향(°) · WSD 풍속(m/s)
- 초단기예보: 매시 30분 기준(base_time HH30), 매시 45분 이후 제공, 앞으로 6시간의 시간별 예보
  위 항목 + SKY 하늘상태 · LGT 낙뢰(kA). RN1 은 범주 문자열("강수없음", "1mm 미만", "3.2mm", "30.0~50.0mm", "50.0mm 이상")
- PTY: 0 없음, 1 비, 2 비/눈, 3 눈, 5 빗방울, 6 빗방울눈날림, 7 눈날림 / SKY: 1 맑음, 3 구름많음, 4 흐림
- +900 이상, -900 이하는 결측
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from ..config import Region
from ..http import FetchError, get_json
from ..timeutil import KST, iso
from .datagokr import NoData, extract_items
from .kma_forecast import _to_float, parse_pcp

log = logging.getLogger("teukbo.kma_ultra")

DATA_GO_KR_BASE = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
APIHUB_BASE = "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0"

NCST_DELAY_MINUTES = 11  # 기준시각(정시) 뒤 제공까지 — 공식 제공 시각은 정시 + 10분
FCST_DELAY_MINUTES = 16  # 발표시각(매시 30분) 뒤 제공까지 — 공식 제공 시각은 매시 45분
WORKERS = 6

Grid = tuple[int, int]


def ncst_bases(now: datetime, delay_minutes: float = NCST_DELAY_MINUTES, count: int = 2) -> list[tuple[str, str]]:
    """지금 조회할 수 있는 초단기실황 기준시각(정시)을 최신순으로."""
    t = (now.astimezone(KST) - timedelta(minutes=delay_minutes)).replace(minute=0, second=0, microsecond=0)
    return [((t - timedelta(hours=k)).strftime("%Y%m%d"), (t - timedelta(hours=k)).strftime("%H00")) for k in range(count)]


def fcst_bases(now: datetime, delay_minutes: float = FCST_DELAY_MINUTES, count: int = 2) -> list[tuple[str, str]]:
    """지금 조회할 수 있는 초단기예보 발표시각(매시 30분)을 최신순으로."""
    t = (now.astimezone(KST) - timedelta(minutes=delay_minutes)).replace(second=0, microsecond=0)
    t = t.replace(minute=30) if t.minute >= 30 else (t - timedelta(hours=1)).replace(minute=30)
    return [((t - timedelta(hours=k)).strftime("%Y%m%d"), (t - timedelta(hours=k)).strftime("%H30")) for k in range(count)]


def base_time(base: tuple[str, str]) -> datetime:
    return datetime.strptime(base[0] + base[1], "%Y%m%d%H%M").replace(tzinfo=KST)


def _int(value: Any) -> int | None:
    number = _to_float(value)
    return None if number is None else int(round(number))


def rain_text(value: Any) -> str | None:
    """초단기예보 RN1 범주 문자열(화면 표시용). 비가 없으면 None. 기상청 표기를 그대로 둔다."""
    if value is None:
        return None
    s = str(value).strip()
    if s in ("", "-", "0", "null") or "강수없음" in s:
        return None
    return s


def parse_ncst(items: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """getUltraSrtNcst 의 item 목록 → {temp, rain, wind, wdir, rh, pty}. 쓸 값이 없으면 None."""
    raw = {str(it.get("category")): it.get("obsrValue") for it in items}
    out = {
        "temp": _to_float(raw.get("T1H")),
        "rain": _to_float(raw.get("RN1")),
        "wind": _to_float(raw.get("WSD")),
        "wdir": _int(raw.get("VEC")),
        "rh": _int(raw.get("REH")),
        "pty": _int(raw.get("PTY")),
    }
    return out if any(v is not None for v in out.values()) else None


def parse_fcst(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """getUltraSrtFcst 의 item 목록 → 시간순 [{t, temp, rain(문자열), rain_mm, sky, pty, wind, wdir, rh, lgt}]."""
    slots: dict[datetime, dict[str, Any]] = {}
    for it in items:
        try:
            t = datetime.strptime(f"{it['fcstDate']}{str(it['fcstTime']).zfill(4)}", "%Y%m%d%H%M").replace(tzinfo=KST)
        except (KeyError, ValueError):
            continue
        slots.setdefault(t, {})[str(it.get("category"))] = it.get("fcstValue")
    out = []
    for t in sorted(slots):
        raw = slots[t]
        row = {
            "t": iso(t),
            "temp": _to_float(raw.get("T1H")),
            "rain": rain_text(raw.get("RN1")),
            "rain_mm": parse_pcp(raw.get("RN1")) if raw.get("RN1") is not None else None,
            "sky": _int(raw.get("SKY")),
            "pty": _int(raw.get("PTY")),
            "wind": _to_float(raw.get("WSD")),
            "wdir": _int(raw.get("VEC")),
            "rh": _int(raw.get("REH")),
            "lgt": _to_float(raw.get("LGT")),
        }
        if any(v is not None for k, v in row.items() if k != "t"):
            out.append(row)
    return out


def _fetch_grid(url: str, key_param: str, key: str, base: tuple[str, str], grid: Grid, http_opts: Mapping[str, Any], retries: int, label: str) -> list[Mapping[str, Any]]:
    payload = get_json(
        url,
        {key_param: key, "pageNo": 1, "numOfRows": 100, "dataType": "JSON", "base_date": base[0], "base_time": base[1], "nx": grid[0], "ny": grid[1]},
        timeout=float(http_opts.get("timeout_seconds", 20)),
        retries=retries,
        backoff=float(http_opts.get("backoff_seconds", 3)),
        label=f"{label} {grid[0]},{grid[1]}",
    )
    items, _ = extract_items(payload, label)
    return items


def _fetch_all(
    url: str, key_param: str, key: str, bases: Sequence[tuple[str, str]], grids: Sequence[Grid], parse: Callable[[Sequence[Mapping[str, Any]]], Any],
    http_opts: Mapping[str, Any], label: str,
) -> tuple[dict[Grid, Any], tuple[str, str], int]:
    """모든 격자를 받는다. 첫 격자로 쓸 수 있는 기준시각을 정한 뒤 나머지를 동시에 받는다. 반환: (격자 → 값, 기준시각, 실패 수)"""
    base: tuple[str, str] | None = None
    first: Any = None
    last_error: FetchError | None = None
    for cand in bases:
        try:
            first = parse(_fetch_grid(url, key_param, key, cand, grids[0], http_opts, 1, label))
            base = cand
            break
        except NoData as e:
            last_error = e
    if base is None:
        raise last_error or FetchError(f"{label}: 조회 가능한 기준시각이 없습니다")

    out: dict[Grid, Any] = {}
    if first:
        out[grids[0]] = first

    def one(grid: Grid) -> tuple[Grid, Any, FetchError | None]:
        try:
            return grid, parse(_fetch_grid(url, key_param, key, base, grid, http_opts, 1, label)), None
        except FetchError as e:
            return grid, None, e

    failed = 0 if first else 1
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for grid, value, error in pool.map(one, grids[1:]):
            if error is not None or not value:
                failed += 1
                if error is not None and not isinstance(error, NoData):
                    log.warning("%s 격자 %s 실패: %s", label, grid, error)
                continue
            out[grid] = value
    if not out:
        raise FetchError(f"{label}: 사용할 수 있는 자료가 없습니다")
    return out, base, failed


def _fetch(
    operation: str, label: str, bases: Sequence[tuple[str, str]], parse: Callable[[Sequence[Mapping[str, Any]]], Any], regions: Sequence[Region],
    data_go_kr_key: str | None, apihub_key: str | None, http_opts: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    opts = http_opts or {}
    grids: dict[Grid, list[Region]] = {}
    for r in regions:
        grids.setdefault((r.nx, r.ny), []).append(r)
    providers: list[tuple[str, str, str, str]] = []
    if data_go_kr_key:
        providers.append(("공공데이터포털", f"{DATA_GO_KR_BASE}/{operation}", "serviceKey", data_go_kr_key))
    if apihub_key:
        providers.append(("기상청 API허브", f"{APIHUB_BASE}/{operation}", "authKey", apihub_key))
    if not providers:
        return {}, {"ok": False, "configured": False, "provider": None, "error": "인증키 미설정"}
    errors: list[str] = []
    for name, url, key_param, key in providers:
        try:
            by_grid, base, failed = _fetch_all(url, key_param, key, bases, list(grids), parse, opts, label)
        except FetchError as e:
            errors.append(f"{name}: {e}")
            log.error("%s(%s) 실패: %s", label, name, e)
            continue
        out = {r.id: value for grid, value in by_grid.items() for r in grids[grid]}
        log.info("%s(%s): %d개 지역, 기준 %s %s%s", label, name, len(out), base[0], base[1], f", 격자 {failed}곳 실패" if failed else "")
        return out, {"ok": True, "configured": True, "provider": name, "base": iso(base_time(base)), "regions": len(out), "failed": failed, "error": None}
    return {}, {"ok": False, "configured": True, "provider": None, "error": " / ".join(errors)}


def fetch_ncst(
    regions: Sequence[Region], now: datetime, *, data_go_kr_key: str | None, apihub_key: str | None = None, http_opts: Mapping[str, Any] | None = None
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """지역별 초단기실황(지금 날씨). 반환: (지역 id → {temp, rain, wind, wdir, rh, pty}, 상태 정보)"""
    return _fetch("getUltraSrtNcst", "초단기실황", ncst_bases(now), parse_ncst, regions, data_go_kr_key, apihub_key, http_opts)


def fetch_fcst(
    regions: Sequence[Region], now: datetime, *, data_go_kr_key: str | None, apihub_key: str | None = None, http_opts: Mapping[str, Any] | None = None
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """지역별 초단기예보(앞으로 6시간, 시간별). 반환: (지역 id → [시간별 값…], 상태 정보)"""
    return _fetch("getUltraSrtFcst", "초단기예보", fcst_bases(now), parse_fcst, regions, data_go_kr_key, apihub_key, http_opts)
