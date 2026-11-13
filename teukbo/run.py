"""한 회차 실행: 수집 → 계산 → 저장.

    python -m teukbo.run            실제 수집 (GitHub Actions 가 3시간마다 실행)
    python -m teukbo.run --demo     가상 자료로 화면·계산 점검 (네트워크 불필요, data-demo/ 에 저장)

환경 변수(인증키) — 코드나 저장소에 직접 쓰지 않는다. GitHub Secrets 로 넣는다.
    DATA_GO_KR_SERVICE_KEY   공공데이터포털 인증키 (단기예보 조회서비스 + 기상특보 조회서비스 활용신청)
    KMA_APIHUB_KEY           기상청 API허브 인증키 (공공데이터포털이 막힐 때의 대체 경로, 선택)
    TEUKBO_LIVE_URL          실시간 자료(live.json)를 읽어 올 주소 (인증키 아님. 없으면 화면은 3시간 자료만 쓴다)
인증키가 없어도 Open-Meteo 만으로 계산은 되지만, 발효 특보·예비특보는 '확인 불가'로 표시된다.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from . import accuracy as accuracy_mod
from . import demo as demo_mod
from . import storage
from .area import apply_area_scores
from .assess import RegionAssessment, RegionOfficial, assess_region
from .config import ROOT, Config, load_config
from .http import normalize_service_key
from .indicators import ModelSeries
from .sources import kma_forecast, kma_warnings, open_meteo
from .timeutil import iso, now_kst, parse_iso

log = logging.getLogger("teukbo.run")

EXIT_OK = 0
EXIT_NO_FORECAST = 2  # 예측 자료를 하나도 받지 못함 → 아무것도 쓰지 않는다(이전 배포 유지)

Forecasts = dict[str, dict[str, ModelSeries]]  # 모델 id → 지역 id → 예측값


def collect(
    cfg: Config, now: datetime, env: Mapping[str, str]
) -> tuple[Forecasts, dict[str, RegionOfficial], dict[str, Any], dict[str, dict[str, Any]], list[Any]]:
    """모든 출처에서 자료를 받는다. 한 출처가 실패해도 나머지로 계속한다.

    반환: (예측값, 지역별 공식 특보, 출처 상태, 모델 상태, 전국 특보 목록)
    """
    st = cfg.settings
    data_go_kr_key = normalize_service_key(env.get("DATA_GO_KR_SERVICE_KEY"))
    apihub_key = (env.get("KMA_APIHUB_KEY") or "").strip() or None
    forecasts: Forecasts = {}
    model_status: dict[str, dict[str, Any]] = {}

    om_models = [m for m in st.models if m.source == "open_meteo"]
    fetch = st.raw.get("fetch", {})
    om_data, om_errors = open_meteo.fetch_all(cfg.regions, om_models, st.past_hours, st.forecast_hours, st.http,
                                             chunk=int(fetch.get("open_meteo_chunk", 0)), pause=float(fetch.get("open_meteo_pause_seconds", 0)))
    forecasts.update(om_data)
    for m in om_models:
        model_status[m.id] = {"ok": m.id in om_data, "error": om_errors.get(m.id)}
    sources: dict[str, Any] = {
        "open_meteo": {
            "ok": bool(om_data),
            "models_ok": len(om_data),
            "models_total": len(om_models),
            "error": "; ".join(om_errors.values()) or None,
        }
    }

    kma_model = next((m for m in st.models if m.source == "kma_forecast"), None)
    if kma_model is not None:
        kma_data, kma_status = kma_forecast.fetch(
            cfg.regions,
            kma_model.id,
            now,
            data_go_kr_key=data_go_kr_key,
            apihub_key=apihub_key,
            http_opts=st.http,
            conventions=st.raw.get("kma_forecast"),
        )
        if kma_data:
            forecasts[kma_model.id] = kma_data
        model_status[kma_model.id] = {"ok": bool(kma_data), "error": kma_status.get("error"), "configured": kma_status.get("configured")}
        sources["kma_forecast"] = kma_status

    official, warn_status, national = kma_warnings.fetch(
        cfg.regions, cfg.thresholds, now, data_go_kr_key=data_go_kr_key, apihub_key=apihub_key, http_opts=st.http
    )
    sources["kma_warnings"] = warn_status
    return forecasts, official, sources, model_status, national


def assess_all(cfg: Config, now: datetime, forecasts: Forecasts, official: Mapping[str, RegionOfficial]) -> tuple[list[RegionAssessment], dict[str, dict[str, ModelSeries]]]:
    """지역별 판정. 반환: (판정 목록, 지역 id → 모델 id → 예측값)"""
    assessments: list[RegionAssessment] = []
    by_region: dict[str, dict[str, ModelSeries]] = {}
    for region in cfg.regions:
        series = {mid: per_region[region.id] for mid, per_region in forecasts.items() if region.id in per_region}
        by_region[region.id] = series
        assessments.append(assess_region(region, series, official.get(region.id, RegionOfficial()), cfg, now))
    apply_area_scores(assessments, cfg)  # 호우: 주변 지역의 예보까지 본다
    return assessments, by_region


def write_web_files(
    cfg: Config,
    now: datetime,
    assessments: list[RegionAssessment],
    by_region: dict[str, dict[str, ModelSeries]],
    sources: dict[str, Any],
    model_status: dict[str, dict[str, Any]],
    data_dir: Path,
    demo: bool,
    live_url: str | None = None,
) -> None:
    latest = storage.build_latest(assessments, cfg, now, sources, model_status, demo=demo, live_url=live_url)
    detail_dir = data_dir / "detail"
    detail_dir.mkdir(parents=True, exist_ok=True)
    keep = set()
    for ra in assessments:
        path = detail_dir / f"{ra.region.id}.json"
        storage.write_json(path, storage.build_detail(ra, by_region[ra.region.id], cfg, now))
        keep.add(path.name)
    for stale in detail_dir.glob("*.json"):
        if stale.name not in keep:
            stale.unlink()
    storage.write_json(data_dir / "latest.json", latest)


def _summary(assessments: list[RegionAssessment], sources: dict[str, Any], now: datetime) -> str:
    grade_ko = {"low": "낮음", "caution": "주의", "high": "높음", "active": "발효 중", "unknown": "자료 없음"}
    lines = [f"### 기상특보 가능성 — {now.strftime('%Y-%m-%d %H:%M')} KST", "", "| 출처 | 상태 |", "|---|---|"]
    for name, status in sources.items():
        state = "정상" if status.get("ok") else ("인증키 미설정" if status.get("configured") is False else f"실패: {status.get('error')}")
        lines.append(f"| {name} | {state} |")
    notable = []
    for ra in assessments:
        top = ra.top()
        if top and top.grade in ("caution", "high", "active"):
            pct = f"{top.probability}%" if top.probability is not None else "-"
            notable.append(f"| {ra.region.name} | {top.hazard.name} | {grade_ko[top.grade]} | {pct} |")
    lines += ["", "| 지역 | 특보 | 등급 | 가능성 |", "|---|---|---|---|"]
    lines += notable or ["| (전 지역 낮음) | | | |"]
    return "\n".join(lines) + "\n"


def run_real(cfg: Config, now: datetime, data_dir: Path, env: Mapping[str, str]) -> int:
    forecasts, official, sources, model_status, national = collect(cfg, now, env)
    if not forecasts:
        log.error("예측 자료를 하나도 받지 못했습니다. 결과를 쓰지 않고 종료합니다(이전 배포가 유지됩니다).")
        return EXIT_NO_FORECAST

    assessments, by_region = assess_all(cfg, now, forecasts, official)
    # 실시간 자료(live.json)의 주소 — 배포 워크플로가 저장소 이름으로 만들어 넣는다
    live_url = (env.get("TEUKBO_LIVE_URL") or "").strip() or None
    write_web_files(cfg, now, assessments, by_region, sources, model_status, data_dir, demo=False, live_url=live_url)
    warnings_ok = bool(sources["kma_warnings"].get("ok"))
    storage.write_json(data_dir / "warnings.json", storage.build_national(national, cfg, now, sources["kma_warnings"]))

    history_dir = data_dir / "history"
    run = storage.build_history_run(
        assessments, cfg, now, {k: bool(v.get("ok")) for k, v in sources.items()}, national=national if warnings_ok else None
    )
    path = storage.append_history(history_dir, run, now)
    log.info("이력 기록: %s", path.relative_to(data_dir.parent) if data_dir.parent in path.parents else path)

    storage.write_json(data_dir / "accuracy.json", accuracy_mod.compute_accuracy(history_dir, cfg, now), pretty=True)

    summary = _summary(assessments, sources, now)
    log.info("완료: 모델 %d개, 지역 %d곳", len(forecasts), len(assessments))
    step_summary = env.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(summary)
    return EXIT_OK


def run_demo(cfg: Config, now: datetime, data_dir: Path, days: int) -> int:
    """가상 시나리오로 과거 회차부터 차례로 돌려 이력·적중률까지 만든다."""
    history_dir = data_dir / "history"
    if history_dir.is_dir():
        for old in history_dir.glob("*.json"):
            old.unlink()
    events = [now - timedelta(hours=30), now + timedelta(hours=15)]  # 지난 비 한 번, 다가오는 비 한 번
    sources = {
        "open_meteo": {"ok": True, "models_ok": 4, "models_total": 4, "error": None},
        "kma_forecast": {"ok": True, "configured": True, "provider": "데모", "error": None},
        "kma_warnings": {"ok": True, "configured": True, "provider": "데모", "error": None},
    }
    model_status = {m.id: {"ok": True, "error": None} for m in cfg.settings.models}
    steps = days * 8
    for k in range(steps, -1, -1):
        t = now - timedelta(hours=3 * k)
        forecasts, official = demo_mod.generate(cfg, t, events)
        assessments, by_region = assess_all(cfg, t, forecasts, official)
        national = demo_mod.national_raws(cfg, official)
        storage.append_history(
            history_dir, storage.build_history_run(assessments, cfg, t, {k2: True for k2 in sources}, national=national), t
        )
        if k == 0:
            write_web_files(cfg, t, assessments, by_region, sources, model_status, data_dir, demo=True, live_url="data/live.json")
            storage.write_json(data_dir / "warnings.json", storage.build_national(national, cfg, t, sources["kma_warnings"]))
            storage.write_json(data_dir / "live.json", demo_mod.live_doc(cfg, t, forecasts, official, national))
    storage.write_json(data_dir / "accuracy.json", accuracy_mod.compute_accuracy(history_dir, cfg, now), pretty=True)
    from . import rail as rail_mod

    rail_days = demo_mod.rail_days(cfg, now)
    for old_file in (data_dir / "rail" / "days").glob("*.json") if (data_dir / "rail" / "days").is_dir() else []:
        old_file.unlink()
    for day in rail_days:
        storage.write_json(data_dir / "rail" / "days" / f"{day['date']}.json", day)
    storage.write_json(data_dir / "rail" / "summary.json", demo_mod.rail_summary(cfg, now, rail_days))
    storage.write_json(data_dir / "rail" / "cube.json", {**rail_mod.build_cube(rail_days), "demo": True})
    log.info("데모 자료 생성 완료: %s (실제 예보가 아닙니다)", data_dir)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="기상특보 가능성: 수집 → 계산 → 저장")
    parser.add_argument("--demo", action="store_true", help="가상 자료로 실행 (네트워크 불필요, data-demo/ 에 저장)")
    parser.add_argument("--demo-days", type=int, default=4, help="데모 이력을 만들 일수")
    parser.add_argument("--data-dir", type=Path, default=None, help="결과 폴더 (기본: data, 데모는 data-demo)")
    parser.add_argument("--now", type=str, default=None, help="기준 시각 ISO 8601 (시험용)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    cfg = load_config()
    now = parse_iso(args.now) if args.now else now_kst()
    now = now.replace(second=0, microsecond=0)
    log.info("기준 시각 %s, 지역 %d곳, 특보 %d종", iso(now), len(cfg.regions), len(cfg.thresholds.hazards))

    if args.demo:
        return run_demo(cfg, now, args.data_dir or ROOT / "data-demo", args.demo_days)
    return run_real(cfg, now, args.data_dir or ROOT / "data", os.environ)


if __name__ == "__main__":
    sys.exit(main())
