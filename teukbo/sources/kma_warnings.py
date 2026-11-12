"""기상청 기상특보·예비특보 수집.

1순위: 공공데이터포털 「기상청_기상특보 조회서비스」 (WthrWrnInfoService)
   - getPwnStatus (특보현황조회): 현재 전국 특보 발효현황(t6)과 예비특보 현황(t7)을 통보문 문장으로 제공
   - getPwnCd    (특보코드조회): 구역코드별 발표·해제 이력 → 발효 시각을 덧붙이는 데만 사용(실패해도 무방)
2순위: 기상청 API허브 특보현황 조회 (wrn_now_data.php, 공공데이터포털이 막히거나 실패할 때)
   - 구역코드별 현재 특보·예비특보를 JSON 으로 제공 (API허브에서 이 API 를 따로 활용신청해야 함)

특보현황 통보문 형식 (활용가이드와 실제 응답 예)
   t6: "o 강풍주의보 : 전라남도(거문도.초도, 완도여서도), 제주도(제주도산지, 추자도)\r\no 호우주의보 : …"
       특보가 없으면 "o 없 음"
   t7: "(1) 호우 예비특보\r\no 11월 13일 새벽(00시~06시) : 전라남도(순천, 광양)\r\n(2) 강풍 예비특보 …"
구역 이름 자체에 괄호·쉼표가 들어가는 경우("완도(여서도 제외)")가 있어 괄호 깊이를 따져 나눈다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from ..assess import OfficialWarning, RegionOfficial
from ..config import Region, Thresholds
from ..http import FetchError, get_bytes, get_json
from ..timeutil import KST, parse_compact
from .datagokr import NoData, extract_items

log = logging.getLogger("teukbo.kma_warnings")

DATA_GO_KR_BASE = "https://apis.data.go.kr/1360000/WthrWrnInfoService"
APIHUB_BASE = "https://apihub.kma.go.kr/api/typ01/url"

# getPwnCd 의 코드값 (활용가이드 2026-06-01 판)
WARN_VAR = {1: "강풍", 2: "호우", 3: "한파", 4: "건조", 5: "폭풍해일", 6: "풍랑", 7: "태풍", 8: "대설", 9: "황사", 12: "폭염", 13: "열대야"}
WARN_STRESS = {0: "주의보", 1: "경보", 2: "중대경보"}
_ISSUE_COMMANDS = {"1", "7"}  # 1 발표, 7 변경발표 (2 해제, 3 연장, 6 정정, 8 변경해제)
_START_TIME_DAYS = 3  # 발효 시각을 찾으려고 거슬러 올라가는 일수

_BULLET = re.compile(r"(?:^|\s)[oOㅇ○]\s+")
_PRELIM_HEADER = re.compile(r"\(\s*\d+\s*\)\s*([^\n\r(]*?)\s*예비\s*특보")
_COLON = re.compile(r"\s*[:：]\s*")


@dataclass(frozen=True)
class RawWarning:
    """출처에서 읽은 특보 한 건(구역 하나). 아직 지역·종류 id 와 연결되지 않은 상태."""

    status: str  # "active" | "preliminary"
    type_name: str  # 호우, 강풍 …
    level_name: str  # 주의보 / 경보 / 중대경보 / (예비특보에서 단계 미상이면 "")
    zone_name: str
    parent_name: str | None = None  # 통보문에서 괄호 앞에 쓰인 상위 구역(도) 이름
    zone_code: str | None = None
    effective_from: datetime | None = None
    issued_at: datetime | None = None
    expected_text: str = ""


# ---------------------------------------------------------------- 통보문 해석


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def is_none_text(text: str | None) -> bool:
    """'o 없음', 'o 없 음', 빈 문자열 등 '특보 없음' 표기인가."""
    cleaned = _norm(text or "").lstrip("oOㅇ○-·")
    return cleaned in ("", "없음")


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """괄호 밖의 구분자로만 나눈다."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in text:
        if ch in "(（":
            depth += 1
        elif ch in ")）":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf).strip())
    return [p for p in parts if p]


def parse_areas(text: str) -> list[tuple[str | None, str]]:
    """구역 나열 문장 → [(상위 구역 또는 None, 구역 이름), …].

    "전라남도(순천, 완도(여서도 제외)), 울릉도.독도"
      → [("전라남도","순천"), ("전라남도","완도(여서도 제외)"), (None,"전라남도(순천, 완도(여서도 제외))"), (None,"울릉도.독도")]
    괄호가 있는 항목은 '상위(하위…)' 로도, 이름 자체에 괄호가 있는 구역으로도 읽힐 수 있어 둘 다 돌려준다.
    """
    out: list[tuple[str | None, str]] = []
    for item in split_top_level(text.strip().rstrip(".")):
        open_at = item.find("(")
        if open_at > 0 and item.endswith(")"):
            parent = item[:open_at].strip()
            for sub in split_top_level(item[open_at + 1 : -1]):
                out.append((parent, sub))
        out.append((None, item))
    return out


def split_name(name: str, level_names: Sequence[str]) -> tuple[str, str]:
    """'호우주의보' → ('호우', '주의보'). 단계 이름이 없으면 (name, '')."""
    compact = _norm(name)
    for level in sorted(level_names, key=len, reverse=True):
        if compact.endswith(level) and len(compact) > len(level):
            return compact[: -len(level)], level
    return compact, ""


def _bullets(text: str) -> list[str]:
    return [chunk.strip() for chunk in _BULLET.split(text.replace("\r", "\n")) if chunk.strip()]


def parse_active_text(text: str | None, level_names: Sequence[str], issued_at: datetime | None = None) -> list[RawWarning]:
    """특보 발효현황 문장(t6) → RawWarning 목록."""
    if is_none_text(text):
        return []
    out: list[RawWarning] = []
    for chunk in _bullets(text or ""):
        parts = _COLON.split(" ".join(chunk.split()), maxsplit=1)
        if len(parts) != 2:
            continue
        type_name, level_name = split_name(parts[0], level_names)
        if not level_name:
            continue
        for parent, zone in parse_areas(parts[1]):
            out.append(RawWarning("active", type_name, level_name, zone, parent, issued_at=issued_at))
    return out


def parse_preliminary_text(text: str | None, level_names: Sequence[str], issued_at: datetime | None = None) -> list[RawWarning]:
    """예비특보 현황 문장(t7 / getWthrPwn 의 pwn) → RawWarning 목록."""
    if is_none_text(text):
        return []
    body = (text or "").replace("\r", "\n")
    headers = list(_PRELIM_HEADER.finditer(body))
    out: list[RawWarning] = []
    for i, m in enumerate(headers):
        type_name, level_name = split_name(m.group(1), level_names)
        section = body[m.end() : headers[i + 1].start() if i + 1 < len(headers) else len(body)]
        for chunk in _bullets(section):
            parts = _COLON.split(" ".join(chunk.split()), maxsplit=1)
            if len(parts) != 2:
                continue
            for parent, zone in parse_areas(parts[1]):
                out.append(RawWarning("preliminary", type_name, level_name, zone, parent, issued_at=issued_at, expected_text=parts[0]))
    return out


# ---------------------------------------------------------------- 지역 연결


def _variants(names: Sequence[str]) -> set[str]:
    out: set[str] = set()
    for n in names:
        base = _norm(n)
        out.update({base, base + "시", base + "군"})
    return out


def matches_region(raw: RawWarning, region: Region) -> bool:
    """이 특보가 이 지역에 해당하는가. 코드가 있으면 코드로, 없으면 구역 이름으로 판단."""
    if raw.zone_code:
        return raw.zone_code in region.zone_codes or raw.zone_code in region.group_codes
    zone = _norm(raw.zone_name)
    zones, groups = _variants(region.zone_names), {_norm(n) for n in region.group_names}
    if raw.parent_name is None:
        return zone in zones or zone in groups
    return _norm(raw.parent_name) in groups and zone in zones


def classify(type_name: str, level_name: str, thresholds: Thresholds) -> tuple[str | None, str | None]:
    """기상청 표기(호우·주의보) → thresholds.json 의 (type id, level id). 계산 대상이 아니면 None."""
    type_id = next((k for k, v in thresholds.types.items() if v.get("name") == type_name), None)
    level_id = next((k for k, v in thresholds.levels.items() if v.get("name") == level_name), None)
    return type_id, level_id


def assign_to_regions(raws: Sequence[RawWarning], regions: Sequence[Region], thresholds: Thresholds) -> dict[str, RegionOfficial]:
    """특보 목록을 지역별로 나눈다. 같은 종류·단계가 여러 세분 구역에 걸쳐 있으면 하나로 합친다."""
    result: dict[str, RegionOfficial] = {}
    for region in regions:
        merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for raw in raws:
            if not matches_region(raw, region):
                continue
            key = (raw.status, raw.type_name, raw.level_name, raw.expected_text)
            slot = merged.setdefault(key, {"zones": [], "from": None, "issued": None, "code": None})
            if raw.zone_name not in slot["zones"]:
                slot["zones"].append(raw.zone_name)
            if raw.effective_from and (slot["from"] is None or raw.effective_from < slot["from"]):
                slot["from"] = raw.effective_from
            slot["issued"] = slot["issued"] or raw.issued_at
            slot["code"] = slot["code"] or raw.zone_code
        official = RegionOfficial(available=True)
        for (status, type_name, level_name, expected), slot in merged.items():
            type_id, level_id = classify(type_name, level_name, thresholds)
            warning = OfficialWarning(
                status=status,
                type=type_id,
                type_name=type_name,
                level=level_id,
                level_name=level_name or ("예비" if status == "preliminary" else ""),
                zone_code=slot["code"],
                zone_name="·".join(slot["zones"]),
                effective_from=slot["from"],
                issued_at=slot["issued"],
                expected_text=expected,
            )
            (official.active if status == "active" else official.preliminary).append(warning)
        result[region.id] = official
    return result


# ---------------------------------------------------------------- 공공데이터포털


def _datagokr_status(key: str, level_names: Sequence[str], http_opts: Mapping[str, Any]) -> list[RawWarning]:
    payload = get_json(
        f"{DATA_GO_KR_BASE}/getPwnStatus",
        {"serviceKey": key, "pageNo": 1, "numOfRows": 10, "dataType": "JSON"},
        timeout=float(http_opts.get("timeout_seconds", 30)),
        retries=min(int(http_opts.get("retries", 3)), 2),
        backoff=float(http_opts.get("backoff_seconds", 3)),
        label="특보현황",
    )
    items, _ = extract_items(payload, "특보현황")
    item = items[0]
    issued = parse_compact(str(item.get("tmFc", "")))
    t6, t7 = str(item.get("t6") or ""), str(item.get("t7") or "")
    active = parse_active_text(t6, level_names, issued)
    prelim = parse_preliminary_text(t7, level_names, issued)
    # 문장이 있는데 한 건도 읽지 못했다면 형식이 바뀐 것이다. '특보 없음'으로 잘못 표시하지 않도록 실패 처리한다.
    if not is_none_text(t6) and not active:
        raise FetchError("특보현황: 발효현황 문장을 해석하지 못했습니다 — " + " ".join(t6.split())[:120])
    if not is_none_text(t7) and not prelim:
        raise FetchError("특보현황: 예비특보 문장을 해석하지 못했습니다 — " + " ".join(t7.split())[:120])
    return active + prelim


def parse_pwn_cd(items: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str, str], datetime]:
    """getPwnCd 이력 → (구역코드, 종류, 단계) 별 가장 최근 발표의 발효 시각."""
    latest: dict[tuple[str, str, str], tuple[str, datetime]] = {}
    for it in items:
        if str(it.get("cancel", "0")).strip() != "0" or str(it.get("command", "")).strip() not in _ISSUE_COMMANDS:
            continue
        try:
            type_name = WARN_VAR[int(it.get("warnVar"))]
            level_name = WARN_STRESS[int(it.get("warnStress"))]
        except (KeyError, TypeError, ValueError):
            continue
        start = parse_compact(str(it.get("startTime", "")))
        code = str(it.get("areaCode", "")).strip()
        announced = str(it.get("tmFc", ""))
        if not start or not code:
            continue
        key = (code, type_name, level_name)
        if key not in latest or announced > latest[key][0]:
            latest[key] = (announced, start)
    return {k: v[1] for k, v in latest.items()}


def _datagokr_start_times(key: str, now: datetime, http_opts: Mapping[str, Any]) -> dict[tuple[str, str, str], datetime]:
    """최근 며칠의 발표 이력에서 발효 시각을 모은다(하루 단위 조회). 실패한 날은 건너뛴다."""
    found: dict[tuple[str, str, str], datetime] = {}
    for back in range(_START_TIME_DAYS - 1, -1, -1):  # 오래된 날부터 → 최근 날이 덮어쓴다
        day = (now.astimezone(KST) - timedelta(days=back)).strftime("%Y%m%d")
        try:
            payload = get_json(
                f"{DATA_GO_KR_BASE}/getPwnCd",
                {"serviceKey": key, "pageNo": 1, "numOfRows": 1000, "dataType": "JSON", "fromTmFc": day, "toTmFc": day},
                timeout=float(http_opts.get("timeout_seconds", 30)),
                retries=1,
                backoff=float(http_opts.get("backoff_seconds", 3)),
                label=f"특보코드 {day}",
            )
            items, _ = extract_items(payload, "특보코드")
        except NoData:
            continue
        except FetchError as e:
            log.warning("특보코드조회(%s) 실패 — 발효 시각 없이 진행: %s", day, e)
            continue
        found.update(parse_pwn_cd(items))
    return found


def _with_start_times(raws: list[RawWarning], starts: Mapping[tuple[str, str, str], datetime], regions: Sequence[Region], now: datetime) -> list[RawWarning]:
    """통보문에서 읽은 발효 중 특보에, 이력에서 찾은 발효 시각을 붙인다(구역 이름 → 코드로 연결)."""
    if not starts:
        return raws
    name_to_codes: dict[str, set[str]] = {}
    for region in regions:
        zone_pairs = list(zip(region.zone_names, region.zone_codes))
        for name, code in zone_pairs:
            for variant in _variants([name]):
                name_to_codes.setdefault(variant, set()).add(code)
    out: list[RawWarning] = []
    for raw in raws:
        start = None
        if raw.status == "active":
            for code in name_to_codes.get(_norm(raw.zone_name), ()):
                candidate = starts.get((code, raw.type_name, raw.level_name))
                if candidate and candidate <= now + timedelta(hours=48) and (start is None or candidate > start):
                    start = candidate
        out.append(
            RawWarning(raw.status, raw.type_name, raw.level_name, raw.zone_name, raw.parent_name, raw.zone_code, start, raw.issued_at, raw.expected_text)
            if start
            else raw
        )
    return out


def fetch_datagokr(key: str, regions: Sequence[Region], thresholds: Thresholds, now: datetime, http_opts: Mapping[str, Any]) -> list[RawWarning]:
    level_names = [str(v["name"]) for v in thresholds.levels.values()]
    raws = _datagokr_status(key, level_names, http_opts)
    if any(r.status == "active" for r in raws):
        raws = _with_start_times(raws, _datagokr_start_times(key, now, http_opts), regions, now)
    return raws


# ---------------------------------------------------------------- 기상청 API허브


def parse_apihub_text(text: str) -> list[dict[str, str]]:
    """API허브 typ01 의 텍스트 응답(disp=0)을 행 목록으로 — JSON(disp=1)을 못 받았을 때의 예비 경로.

    '#' 주석 줄 가운데 열 이름 줄(# REG_UP, REG_UP_KO, REG_ID, …)이 있고, 이어서 쉼표로 나뉜 자료 줄이 온다.
    """
    columns: list[str] = []
    rows: list[dict[str, str]] = []
    for line in text.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            names = [c for c in re.split(r"[,\s]+", line.lstrip("#").strip()) if c]
            names = [re.sub(r"-+$", "", n) for n in names]
            if len(names) >= 4 and all(re.fullmatch(r"[A-Z_0-9]+", n) for n in names):
                columns = names
            continue
        if not columns:
            continue
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < len(columns):
            cells += [""] * (len(columns) - len(cells))
        rows.append(dict(zip(columns, cells)))
    return rows


def _apihub_level(level: str) -> str | None:
    """LVL 값 → 단계 이름. 예비특보는 ''. 알 수 없는 값은 None."""
    level = _norm(level)
    if level == "예비":
        return ""
    if "중대" in level:
        return "중대경보"
    if level in ("주의", "주의보"):
        return "주의보"
    if level == "경보":
        return "경보"
    return None


_WINDOW_LABELS = {6: "새벽", 9: "아침", 12: "오전", 15: "낮", 18: "오후", 21: "저녁", 24: "밤"}


def _expected_until(compact: str) -> str:
    """예비특보 행의 TM_EF(예상 시간대의 끝, 예: …2358) → '11월 13일 밤(~24시)'."""
    when = parse_compact(compact)
    if when is None:
        return ""
    hour = when.hour + (1 if when.minute >= 30 else 0)
    label = _WINDOW_LABELS.get(hour)
    if label:
        return f"{when.month}월 {when.day}일 {label}(~{hour}시)"
    return f"{when.month}월 {when.day}일 {when.hour}시경"


def parse_apihub_now(rows: Sequence[Mapping[str, Any]]) -> list[RawWarning]:
    """API허브 특보현황(wrn_now_data.php) 행 → RawWarning 목록.

    열: REG_UP, REG_UP_KO, REG_ID, REG_KO, TM_FC(발표), TM_EF(발효), WRN(종류: 호우·강풍…), LVL(예비/주의/경보), CMD(발표/변경), ED_TM(해제 예고)
    값은 공백으로 채워진 고정폭이라 앞뒤 공백을 지운다. 해제된 특보는 목록에서 빠진다.
    예비특보 행의 TM_EF 는 예상 시간대의 끝이다.
    """
    out: list[RawWarning] = []
    for row in rows:
        get = lambda k: str(row.get(k) or "").strip()  # noqa: E731
        kind, code = _norm(get("WRN")), get("REG_ID")
        level = _apihub_level(get("LVL"))
        if not kind or not code or level is None or "해제" in get("CMD"):
            continue
        preliminary = level == ""
        out.append(
            RawWarning(
                status="preliminary" if preliminary else "active",
                type_name=kind,
                level_name=level,
                zone_name=get("REG_KO"),
                parent_name=get("REG_UP_KO") or None,
                zone_code=code,
                effective_from=None if preliminary else parse_compact(get("TM_EF")),
                issued_at=parse_compact(get("TM_FC")),
                expected_text=_expected_until(get("TM_EF")) if preliminary else "",
            )
        )
    return out


def _decode(body: bytes) -> str:
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", "replace")


def parse_apihub_body(body: bytes) -> list[RawWarning]:
    """wrn_now_data.php 응답 본문 → RawWarning 목록. disp=1 은 JSON 배열(특보가 없으면 [])."""
    text = _decode(body).strip()
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise FetchError("특보현황(API허브): JSON 해석 실패") from e
        if isinstance(data, dict):  # {"result": {"status": 403, "message": "활용신청이 필요한 API 입니다."}}
            result = data.get("result", data)
            message = result.get("message", result) if isinstance(result, dict) else result
            raise FetchError(f"특보현황(API허브): {message}")
        return parse_apihub_now(data)
    if "#START7777" in text or "REG_ID" in text:
        return parse_apihub_now(parse_apihub_text(text))
    raise FetchError("특보현황(API허브): 예상하지 못한 응답 — " + " ".join(text.split())[:120])


def fetch_apihub(key: str, http_opts: Mapping[str, Any]) -> list[RawWarning]:
    body = get_bytes(
        f"{APIHUB_BASE}/wrn_now_data.php",
        {"fe": "f", "tm": "", "disp": 1, "help": 0, "authKey": key},
        timeout=float(http_opts.get("timeout_seconds", 30)),
        retries=min(int(http_opts.get("retries", 3)), 2),
        backoff=float(http_opts.get("backoff_seconds", 3)),
        label="특보현황(API허브)",
    )
    return parse_apihub_body(body)


# ---------------------------------------------------------------- 진입점


def fetch(
    regions: Sequence[Region],
    thresholds: Thresholds,
    now: datetime,
    *,
    data_go_kr_key: str | None,
    apihub_key: str | None,
    http_opts: Mapping[str, Any] | None = None,
) -> tuple[dict[str, RegionOfficial], dict[str, Any]]:
    """현재 발효 특보·예비특보를 지역별로 돌려준다. 반환: (지역 id → RegionOfficial, 상태 정보).

    조회에 실패하면 모든 지역이 available=False — 화면에서는 '없음'이 아니라 '확인 불가'로 표시해야 한다.
    """
    opts = http_opts or {}
    unavailable = {r.id: RegionOfficial(available=False) for r in regions}
    providers: list[tuple[str, Any]] = []
    if data_go_kr_key:
        providers.append(("공공데이터포털", lambda: fetch_datagokr(data_go_kr_key, regions, thresholds, now, opts)))
    if apihub_key:
        providers.append(("기상청 API허브", lambda: fetch_apihub(apihub_key, opts)))
    if not providers:
        return unavailable, {"ok": False, "configured": False, "provider": None, "error": "인증키 미설정"}

    errors: list[str] = []
    for name, call in providers:
        try:
            raws = call()
        except FetchError as e:
            errors.append(f"{name}: {e}")
            log.error("특보 조회(%s) 실패: %s", name, e)
            continue
        official = assign_to_regions(raws, regions, thresholds)
        log.info("특보 조회(%s): 전국 %d건 중 대상 지역 발효 %d건·예비 %d건", name, len(raws),
                 sum(len(o.active) for o in official.values()), sum(len(o.preliminary) for o in official.values()))
        return official, {"ok": True, "configured": True, "provider": name, "error": None}
    return unavailable, {"ok": False, "configured": True, "provider": None, "error": " / ".join(errors)}
