"""한국철도공사 열차운행정보 (공공데이터포털 15125762).

    https://apis.data.go.kr/B551457/run/v2
      /travelerTrainRunPlan2   여객열차 운행계획 — 열차별 출발역·도착역과 계획 출발·도착 일시 (3개월 전 ~ 1개월 뒤)
      /travelerTrainRunInfo2   여객열차 운행정보 — 실제 운행 기록: 열차·역별 실제 도착·출발 일시, 주운행선(노선) (3개월 전 ~ 1일 전)
      /codes2                  코드정보 (mrnt_cd 주운행선, stn_cd 역, stop_se_cd 정차구분, uppln_dn_se_cd 상·하행)

실시간 자료가 아니다. 운행정보는 전날까지만 제공되므로 '어제까지의 지연'을 집계하는 데 쓴다.
열차의 현재 위치·현재 지연은 공식 공개 자료가 없다(코레일 열차위치 화면은 외부 프로그램 접근을 허용하지 않음).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ..http import FetchError, get_json
from .datagokr import NoData, extract_items

log = logging.getLogger("teukbo.korail")

BASE = "https://apis.data.go.kr/B551457/run/v2"
PAGE_SIZE = 1000
MAX_PAGES = 200  # 하루치 운행정보가 수만 행일 수 있다
CODE_TYPES = ("mrnt_cd", "stn_cd", "stop_se_cd", "uppln_dn_se_cd")


def build_params(key: str, page: int, rows: int, conds: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """요청 변수. conds 는 {"run_ymd::GTE": "20261111"} 처럼 '항목::연산자' → 값."""
    params: dict[str, Any] = {"serviceKey": key, "pageNo": page, "numOfRows": rows, "returnType": "JSON"}
    for name, value in (conds or {}).items():
        if value is not None:
            params[f"cond[{name}]"] = value
    return params


def fetch_page(
    operation: str, key: str, conds: Mapping[str, Any] | None, page: int, rows: int, http_opts: Mapping[str, Any] | None = None
) -> tuple[list[Mapping[str, Any]], int]:
    """한 쪽을 받아 (행 목록, 전체 건수)를 돌려준다. 자료가 없으면 ([], 0)."""
    opts = http_opts or {}
    payload = get_json(
        f"{BASE}/{operation}",
        build_params(key, page, rows, conds),
        timeout=float(opts.get("timeout_seconds", 30)),
        retries=min(int(opts.get("retries", 3)), 2),
        backoff=float(opts.get("backoff_seconds", 3)),
        label=f"코레일 {operation}",
    )
    try:
        return extract_items(payload, f"코레일 {operation}")
    except NoData:
        return [], 0


def fetch_all(
    operation: str, key: str, conds: Mapping[str, Any] | None = None, http_opts: Mapping[str, Any] | None = None, page_size: int = PAGE_SIZE
) -> list[Mapping[str, Any]]:
    """조건에 맞는 모든 행을 쪽을 넘겨 가며 받는다."""
    rows: list[Mapping[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        items, total = fetch_page(operation, key, conds, page, page_size, http_opts)
        rows.extend(items)
        if not items or len(rows) >= total:
            return rows
    raise FetchError(f"코레일 {operation}: {MAX_PAGES}쪽을 넘도록 끝나지 않습니다 (조건을 좁혀야 합니다)")


def day_conds(ymd: str) -> dict[str, str]:
    """운행일자 하루만 고르는 조건."""
    return {"run_ymd::GTE": ymd, "run_ymd::LTE": ymd}


def fetch_run_plan(key: str, ymd: str, http_opts: Mapping[str, Any] | None = None) -> list[Mapping[str, Any]]:
    """그날의 여객열차 운행계획 (열차별 출발·도착역과 계획 시각)."""
    return fetch_all("travelerTrainRunPlan2", key, day_conds(ymd), http_opts)


def fetch_run_info(key: str, ymd: str, http_opts: Mapping[str, Any] | None = None) -> list[Mapping[str, Any]]:
    """그날의 여객열차 실제 운행 기록 (열차·역별 실제 도착·출발 시각). 전날 이전 날짜만 제공된다."""
    return fetch_all("travelerTrainRunInfo2", key, day_conds(ymd), http_opts)


def fetch_codes(key: str, code_type: str, http_opts: Mapping[str, Any] | None = None) -> dict[str, str]:
    """코드 → 이름."""
    if code_type not in CODE_TYPES:
        raise ValueError(f"code_type 은 {CODE_TYPES} 중 하나")
    rows = fetch_all("codes2", key, {"type::EQ": code_type}, http_opts)
    return {str(r.get("code")): str(r.get("value")) for r in rows}
