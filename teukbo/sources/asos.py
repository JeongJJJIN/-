"""기상청 종관기상관측(ASOS) 시간자료 — 실제로 관측된 기온·강수량·바람·습도·적설.

공공데이터포털 「기상청_지상(종관, ASOS) 시간자료 조회서비스」(getWthrDataList). 전국 약 100개 지점, 전날까지의 자료를 준다.
이 서비스의 쓰임: 과거 검증(백테스트)의 '실제 결과'. 특보 발표 이력은 60일 전까지만 조회되지만 관측 자료는 오래전까지 있으므로,
"그때의 예보로 계산한 가능성" 대 "실제 관측이 특보 기준에 닿았는지"를 여러 해·여러 계절에 걸쳐 맞춰 볼 수 있다.

응답 한 행(시각 하나)에서 쓰는 값
    tm 시각(YYYY-MM-DD HH:MM, 한국 시각) · stnId 지점번호 · stnNm 지점 이름
    ta 기온(℃) · rn 강수량(mm, 직전 1시간) · ws 풍속(m/s) · hm 습도(%) · hr3Fhsc 3시간 신적설(cm) · dsnw 적설(cm)
    값이 없으면 빈 문자열이다(강수량의 빈 값은 '비 없음'으로 본다 — 관측 자체가 빠진 시각과는 구분되지 않는다).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from ..http import get_json
from ..timeutil import KST
from .datagokr import NoData, extract_items

ENDPOINT = "https://apis.data.go.kr/1360000/AsosHourlyInfoService/getWthrDataList"
PAGE_SIZE = 999  # 한 번에 받을 수 있는 최대 행 수
FIELDS = {"ta": "temp", "rn": "precip", "ws": "wind", "hm": "rh", "hr3Fhsc": "snow3h", "dsnw": "snow_depth"}


def _number(value: Any) -> float | None:
    try:
        return float(value) if str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def parse_rows(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """응답 행들 → [{t: 시각, stn: 지점번호, name: 지점 이름, temp, precip, wind, rh, snow3h, snow_depth}] (값이 없으면 None)."""
    out = []
    for row in items:
        try:
            when = datetime.strptime(str(row.get("tm", "")), "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        except ValueError:
            continue
        rec: dict[str, Any] = {"t": when, "stn": str(row.get("stnId", "")), "name": str(row.get("stnNm", ""))}
        for raw, name in FIELDS.items():
            rec[name] = _number(row.get(raw))
        out.append(rec)
    return out


def fetch_hourly(key: str, station: str, start: datetime, end: datetime, http_opts: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """한 지점의 start ~ end(시각 포함, 한국 시각) 시간자료. 자료가 없으면 빈 목록. end 는 어제 이전이어야 한다."""
    opts = http_opts or {}
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = get_json(
            ENDPOINT,
            {"serviceKey": key, "pageNo": page, "numOfRows": PAGE_SIZE, "dataType": "JSON", "dataCd": "ASOS", "dateCd": "HR",
             "startDt": start.strftime("%Y%m%d"), "startHh": start.strftime("%H"), "endDt": end.strftime("%Y%m%d"), "endHh": end.strftime("%H"), "stnIds": station},
            timeout=float(opts.get("timeout_seconds", 30)),
            retries=int(opts.get("retries", 2)),
            backoff=float(opts.get("backoff_seconds", 3)),
            label=f"ASOS {station}",
        )
        try:
            items, total = extract_items(payload, f"ASOS {station}")
        except NoData:
            break
        out.extend(parse_rows(items))
        if page * PAGE_SIZE >= total:
            break
        page += 1
    return out
