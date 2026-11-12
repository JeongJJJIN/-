"""실시간 자료 수집: 특보·예비특보, 지금 날씨(초단기실황), 앞으로 6시간(초단기예보)을 live.json 하나로 묶는다.

    python3 -m teukbo.live --out _live/live.json --prev 이전_live.json

3시간마다 도는 본 수집(teukbo.run)과 달리 몇 분마다 가볍게 돈다(GitHub Actions 의 live.yml).
- 특보·예비특보: 실행할 때마다 조회
- 초단기실황·초단기예보: 기상청이 값을 바꾸는 간격(10분)에 맞춰, 설정한 간격이 지났거나 새 기준시각이 나왔을 때만 조회
- 조회에 실패하면 직전 값을 그대로 싣되 '확인 시각'(checked_at)은 바꾸지 않는다 → 화면이 얼마나 오래된 값인지 정확히 알 수 있다

live.json
    generated_at            이 파일을 만든 시각
    warnings                전국 특보·예비특보(특보구역 코드 단위) + checked_at
    obs / fcst              실황·초단기예보의 상태 {available, base(기준시각), checked_at, provider, error}
    regions[지역 id]
        official            그 지역의 발효 특보·예비특보 (latest.json 과 같은 형식)
        hz                  특보(종류·단계)별 공식 발표 상태 {특보 id: [상태, 단계, 발효 시각]}
        now                 지금 날씨 {temp, rain, wind, wdir, rh, pty}
        fcst                앞으로 6시간 [{t, temp, rain, rain_mm, sky, pty, wind, wdir, rh, lgt}]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from . import storage
from .assess import RegionOfficial, official_for
from .config import Config, load_config
from .http import normalize_service_key
from .sources import kma_ultra, kma_warnings
from .timeutil import iso, now_kst, parse_iso

log = logging.getLogger("teukbo.live")

SCHEMA = 1
_UNAVAILABLE = {"available": False, "checked_at": None, "error": None}


def _age_minutes(section: Mapping[str, Any] | None, now: datetime) -> float | None:
    """그 자료를 마지막으로 확인한 지 몇 분 됐는지. 확인한 적이 없으면 None."""
    stamp = (section or {}).get("checked_at")
    if not stamp:
        return None
    try:
        return (now - parse_iso(stamp)).total_seconds() / 60.0
    except ValueError:
        return None


def hazard_official(official: RegionOfficial, cfg: Config) -> dict[str, list[Any]]:
    """특보(종류·단계)별 공식 발표 상태. 본 수집과 같은 규칙(assess.official_for)을 쓴다."""
    out: dict[str, list[Any]] = {}
    for hazard in cfg.thresholds.hazards:
        status, level, start = official_for(hazard, official, cfg.thresholds)
        if status:
            out[hazard.id] = [status, level, iso(start) if start else None]
    return out


def _carry(prev: Mapping[str, Any], name: str, now: datetime, limit_minutes: float, error: str | None) -> dict[str, Any]:
    """조회에 실패했을 때: 직전 상태를 이어 쓴다(확인 시각은 그대로). 너무 오래됐으면 '없음'으로 둔다."""
    section = dict(prev.get(name) or {})
    age = _age_minutes(section, now)
    if not section.get("available") or age is None or age > limit_minutes:
        return {**_UNAVAILABLE, "error": error}
    section["error"] = error
    return section


def collect(cfg: Config, now: datetime, env: Mapping[str, str], prev: Mapping[str, Any] | None = None) -> dict[str, Any]:
    prev = prev if isinstance(prev, dict) and prev.get("schema") == SCHEMA else {}
    prev_regions: Mapping[str, Any] = prev.get("regions") or {}
    settings = cfg.settings.raw.get("live", {})
    carry_limit = float(settings.get("carry_over_minutes", 180))
    http_opts = cfg.settings.http
    data_key = normalize_service_key(env.get("DATA_GO_KR_SERVICE_KEY"))
    hub_key = (env.get("KMA_APIHUB_KEY") or "").strip() or None
    regions: dict[str, dict[str, Any]] = {r.id: {} for r in cfg.regions}

    # ---- 1) 특보·예비특보 (매번)
    official, status, national = kma_warnings.fetch(cfg.regions, cfg.thresholds, now, data_go_kr_key=data_key, apihub_key=hub_key, http_opts=http_opts)
    if status.get("ok"):
        warnings = storage.build_national(national, cfg, now, status)
        warnings.update({"checked_at": iso(now), "error": None})
        for r in cfg.regions:
            regions[r.id]["official"] = storage.region_official_dict(official[r.id])
            regions[r.id]["hz"] = hazard_official(official[r.id], cfg)
    else:
        warnings = _carry(prev, "warnings", now, carry_limit, status.get("error"))
        warnings["configured"] = status.get("configured", True)
        if warnings.get("available"):
            for r in cfg.regions:
                for key in ("official", "hz"):
                    if key in prev_regions.get(r.id, {}):
                        regions[r.id][key] = prev_regions[r.id][key]

    # ---- 2) 지금 날씨, 3) 앞으로 6시간 — 간격이 지났거나 새 기준시각이 나왔을 때만
    plans = (
        ("obs", "now", kma_ultra.fetch_ncst, kma_ultra.ncst_bases, float(settings.get("obs_refresh_minutes", 10))),
        ("fcst", "fcst", kma_ultra.fetch_fcst, kma_ultra.fcst_bases, float(settings.get("fcst_refresh_minutes", 20))),
    )
    sections: dict[str, dict[str, Any]] = {}
    for name, field, fetch, bases, every in plans:
        before = prev.get(name) or {}
        age = _age_minutes(before, now)
        newest = iso(kma_ultra.base_time(bases(now)[0]))
        due = not before.get("available") or age is None or age >= every - 1.0 or before.get("base") != newest
        if due and (data_key or hub_key):
            data, st = fetch(cfg.regions, now, data_go_kr_key=data_key, apihub_key=hub_key, http_opts=http_opts)
        elif due:
            data, st = {}, {"ok": False, "configured": False, "error": "인증키 미설정"}
        else:
            data, st = None, None
        if st is not None and st.get("ok"):
            sections[name] = {"available": True, "base": st["base"], "checked_at": iso(now), "provider": st.get("provider"), "error": None}
            same_base = before.get("base") == st["base"]
            for r in cfg.regions:
                value = data.get(r.id) or (prev_regions.get(r.id, {}).get(field) if same_base else None)  # 한 곳만 실패하면 같은 기준시각의 직전 값
                if value:
                    regions[r.id][field] = value
        else:
            # 아직 받을 때가 아니거나(직전 값 그대로) 실패(직전 값 + 오류 표시)
            sections[name] = dict(before) if st is None else _carry(prev, name, now, carry_limit, st.get("error"))
            if st is not None:
                sections[name]["configured"] = st.get("configured", True)
            if sections[name].get("available"):
                for r in cfg.regions:
                    if field in prev_regions.get(r.id, {}):
                        regions[r.id][field] = prev_regions[r.id][field]

    # 지난 시각의 예보는 싣지 않는다(직전 값을 이어 쓸 때 생길 수 있다)
    cutoff = iso(now - timedelta(minutes=30))
    for entry in regions.values():
        if "fcst" in entry:
            entry["fcst"] = [row for row in entry["fcst"] if row.get("t", "") >= cutoff]

    return {"schema": SCHEMA, "generated_at": iso(now), "warnings": warnings, "obs": sections["obs"], "fcst": sections["fcst"], "regions": regions}


def summary_line(doc: Mapping[str, Any]) -> str:
    def part(label: str, section: Mapping[str, Any], extra: str = "") -> str:
        if not section.get("available"):
            return f"{label} 없음({section.get('error') or '미확인'})"
        return f"{label} {section.get('checked_at', '')[11:16]} 확인{extra}" + (f" [직전 값: {section['error']}]" if section.get("error") else "")

    w = doc["warnings"]
    counts = f", 발효 {len(w.get('active', []))}·예비 {len(w.get('preliminary', []))}건" if w.get("available") else ""
    return " / ".join([part("특보", w, counts), part("실황", doc["obs"], f"(기준 {str(doc['obs'].get('base', ''))[11:16]})"), part("초단기예보", doc["fcst"], f"(발표 {str(doc['fcst'].get('base', ''))[11:16]})")])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="실시간 자료 수집 (특보·지금 날씨·앞으로 6시간)")
    parser.add_argument("--out", type=Path, required=True, help="결과 파일 (live.json)")
    parser.add_argument("--prev", type=Path, default=None, help="직전 결과 파일 (있으면 조회 간격 판단·실패 시 이어 쓰기에 사용)")
    parser.add_argument("--now", type=str, default=None, help="기준 시각 ISO 8601 (시험용)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("teukbo.http").setLevel(logging.WARNING)

    cfg = load_config()
    now = (parse_iso(args.now) if args.now else now_kst()).replace(second=0, microsecond=0)
    prev = None
    if args.prev is not None:
        try:
            prev = storage.read_json(args.prev)
        except ValueError:
            log.warning("직전 결과 파일을 읽지 못해 처음부터 받습니다: %s", args.prev)
    doc = collect(cfg, now, os.environ, prev)
    storage.write_json(args.out, doc)
    line = summary_line(doc)
    log.info("실시간 자료: %s", line)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(f"실시간 자료 {iso(now)} — {line}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
