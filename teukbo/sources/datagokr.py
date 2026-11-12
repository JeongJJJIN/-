"""공공데이터포털(기상청 API) 응답 공통 처리.

API허브의 typ02/openApi 경로도 같은 응답 구조를 쓴다.
- 정상:   {"response": {"header": {"resultCode": "00", …}, "body": {"items": {"item": [...]}, "totalCount": n}}}
- 자료없음: resultCode "03"  /  기타 오류: header 만 있고 body 없음 (예: "99" 조회 기간 초과)
- 게이트웨이 오류(키 미등록·한도 초과·IP 차단): {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {...}}} + HTTP 4xx
- API허브 오류: {"result": {"status": 401, "message": "…"}}
"""

from __future__ import annotations

from typing import Any, Mapping

from ..http import FetchError


class NoData(FetchError):
    """요청한 조건의 자료가 (아직) 없음 — resultCode 03 또는 빈 목록."""


def extract_items(payload: Any, label: str) -> tuple[list[Mapping[str, Any]], int]:
    """응답에서 item 목록과 totalCount 를 꺼낸다. 오류 응답이면 FetchError, 자료가 없으면 NoData."""
    if not isinstance(payload, dict):
        raise FetchError(f"{label}: 예상하지 못한 응답 형식")
    gateway = payload.get("OpenAPI_ServiceResponse")
    if gateway:
        head = gateway.get("cmmMsgHeader", {}) if isinstance(gateway, dict) else {}
        raise FetchError(
            f"{label}: {head.get('errMsg', '게이트웨이 오류')} "
            f"({head.get('returnAuthMsg', '')}, 코드 {head.get('returnReasonCode', '?')})"
        )
    if "result" in payload and "response" not in payload:
        res = payload["result"]
        message = res.get("message", res) if isinstance(res, dict) else res
        raise FetchError(f"{label}(API허브): {message}", status=res.get("status") if isinstance(res, dict) else None)
    response = payload.get("response") or {}
    header = response.get("header") or {}
    code = str(header.get("resultCode", "")).zfill(2)
    if code == "03":
        raise NoData(f"{label}: 자료 없음 ({header.get('resultMsg', 'NODATA_ERROR')})")
    if code != "00":
        raise FetchError(f"{label}: 오류 코드 {code} {header.get('resultMsg', '')}")
    body = response.get("body") or {}
    items_node = body.get("items") or {}
    items = items_node.get("item") if isinstance(items_node, dict) else None
    if isinstance(items, dict):
        items = [items]
    if not items:
        raise NoData(f"{label}: 자료 없음 (빈 응답)")
    return list(items), int(body.get("totalCount") or len(items))
