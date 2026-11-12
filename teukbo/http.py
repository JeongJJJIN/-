"""HTTP GET 도우미: 재시도·대기·로그. 인증키는 로그와 오류 메시지에 남기지 않는다."""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

log = logging.getLogger("teukbo.http")

USER_AGENT = "teukbo-forecast/1.0 (weather warning outlook; GitHub Actions)"
SECRET_PARAMS = ("serviceKey", "ServiceKey", "authKey", "apikey", "key")
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
_MAX_RETRY_AFTER = 90.0


class FetchError(RuntimeError):
    """재시도 후에도 받아오지 못했을 때. 메시지에는 인증키가 포함되지 않는다."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def build_url(base: str, params: Mapping[str, Any]) -> str:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, safe=",")
    return f"{base}?{query}" if query else base


def redact(url: str) -> str:
    """URL 의 인증키 값을 가린다."""
    parts = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    safe = [(k, "***" if k in SECRET_PARAMS else v) for k, v in pairs]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(safe, safe=",*")))


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def get_bytes(
    base: str,
    params: Mapping[str, Any] | None = None,
    *,
    timeout: float = 30.0,
    retries: int = 3,
    backoff: float = 3.0,
    label: str = "",
) -> bytes:
    """GET 요청. 네트워크 오류·5xx·429 는 간격을 늘려 가며 재시도한다.

    retries 는 '추가 시도' 횟수(총 시도 = retries + 1). 실패하면 FetchError.
    """
    url = build_url(base, params or {})
    shown = redact(url)
    name = label or urllib.parse.urlsplit(url).netloc
    last = "알 수 없는 오류"
    status: int | None = None
    for attempt in range(retries + 1):
        wait = backoff * (2**attempt)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            log.info("[%s] 수신 %d바이트 (시도 %d) %s", name, len(body), attempt + 1, shown)
            return body
        except urllib.error.HTTPError as e:
            status = e.code
            detail = ""
            try:
                detail = e.read(300).decode("utf-8", "replace").strip().replace("\n", " ")
            except Exception:  # noqa: BLE001 - 본문을 못 읽어도 상태 코드는 남긴다
                pass
            last = f"HTTP {e.code} {detail}".strip()
            if e.code not in _RETRY_STATUS:
                break
            retry_after = e.headers.get("Retry-After") if e.headers else None
            if retry_after and retry_after.strip().isdigit():
                if float(retry_after) > _MAX_RETRY_AFTER:
                    # 일일 한도 초과처럼 오래 기다려야 하는 경우: 이번 실행에서는 재시도해도 소용없다
                    last += f" (Retry-After {retry_after.strip()}초 — 재시도하지 않음)"
                    break
                wait = float(retry_after) + random.uniform(1.0, 3.0)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            status = None
            reason = getattr(e, "reason", e)
            last = f"{type(e).__name__}: {reason}"
        if attempt < retries:
            log.warning("[%s] 실패(%s) — %.0f초 뒤 재시도 %d/%d", name, last, wait, attempt + 1, retries)
            _sleep(wait)
    log.error("[%s] 최종 실패: %s — %s", name, last, shown)
    raise FetchError(f"{name}: {last}", status=status)


def get_json(base: str, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
    body = get_bytes(base, params, **kwargs)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        head = body[:200].decode("utf-8", "replace").replace("\n", " ")
        raise FetchError(f"{kwargs.get('label') or base}: JSON 이 아닌 응답 — {head}") from e


def normalize_service_key(key: str | None) -> str | None:
    """공공데이터포털 인증키 정리. Encoding 키(%2B 등 포함)를 넣어도 Decoding 키로 되돌려 쓴다."""
    if not key:
        return None
    key = key.strip()
    if not key:
        return None
    if "%" in key:
        decoded = urllib.parse.unquote(key)
        if decoded != key:
            return decoded
    return key
