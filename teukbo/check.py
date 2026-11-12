"""출처 점검: 각 출처에 실제로 접속해 응답을 읽을 수 있는지 확인한다(아무것도 저장하지 않음).

    python3 -m teukbo.check

인증키를 새로 넣었거나, 모델 이름을 바꿨거나, 수집이 실패할 때 원인을 찾는 용도.
환경 변수 DATA_GO_KR_SERVICE_KEY, KMA_APIHUB_KEY 가 없으면 해당 출처는 건너뛴다.
"""

from __future__ import annotations

import logging
import os
import sys

from .config import load_config
from .http import FetchError, normalize_service_key
from datetime import timedelta

from .sources import kma_forecast, kma_warnings, korail, open_meteo
from .timeutil import iso, now_kst


def main() -> int:
    logging.disable(logging.CRITICAL)  # 결과는 아래 표시로 충분하다(재시도 로그는 생략)
    cfg = load_config()
    st = cfg.settings
    now = now_kst().replace(second=0, microsecond=0)
    regions = cfg.regions[:2]
    data_go_kr_key = normalize_service_key(os.environ.get("DATA_GO_KR_SERVICE_KEY"))
    apihub_key = (os.environ.get("KMA_APIHUB_KEY") or "").strip() or None
    opts = {**st.http, "retries": 1}
    print(f"기준 시각 {iso(now)} · 점검 지역: {', '.join(r.name for r in regions)}")
    ok_models = 0

    print("\n[Open-Meteo]")
    for model in (m for m in st.models if m.source == "open_meteo"):
        try:
            data = open_meteo.fetch_model(regions, model, st.past_hours, st.forecast_hours, opts)
        except FetchError as e:
            print(f"  실패  {model.id}: {e}")
            continue
        ms = data[regions[0].id]
        ok_models += 1
        have = ", ".join(f"{name}({sum(v is not None for v in ms.values[name])})" for name in sorted(ms.values))
        missing = sorted(set(open_meteo.HOURLY_VARS.values()) - set(ms.values))
        print(f"  정상  {model.id}: {len(data)}개 지점, {iso(ms.times[0])} ~ {iso(ms.times[-1])} ({len(ms.times)}시간)")
        print(f"        변수(값 있는 시간 수): {have}" + (f" · 없는 변수: {', '.join(missing)}" if missing else ""))

    print("\n[기상청 단기예보]")
    kma_model = next((m for m in st.models if m.source == "kma_forecast"), None)
    if kma_model is None:
        print("  설정에 없음")
    else:
        data, status = kma_forecast.fetch(regions, kma_model.id, now, data_go_kr_key=data_go_kr_key, apihub_key=apihub_key, http_opts=opts, conventions=st.raw.get("kma_forecast"))
        if status.get("configured") is False:
            print("  건너뜀 (인증키 미설정)")
        elif not status["ok"]:
            print(f"  실패  {status['error']}")
        else:
            ms = data[regions[0].id]
            ok_models += 1
            print(f"  정상  {status['provider']}, 발표 {status['base']}: {iso(ms.times[0])} ~ {iso(ms.times[-1])} ({len(ms.times)}시간), 변수 {', '.join(sorted(ms.values))}")

    print("\n[기상청 특보·예비특보]")
    official, status, national = kma_warnings.fetch(cfg.regions, cfg.thresholds, now, data_go_kr_key=data_go_kr_key, apihub_key=apihub_key, http_opts=opts)
    if status.get("configured") is False:
        print("  건너뜀 (인증키 미설정)")
    elif not status["ok"]:
        print(f"  실패  {status['error']}")
    else:
        active = sum(len(o.active) for o in official.values())
        prelim = sum(len(o.preliminary) for o in official.values())
        nat_active = len({(r.zone_code, r.type_name, r.level_name) for r in national if r.status == "active" and r.zone_code})
        nat_prelim = len({(r.zone_code, r.type_name) for r in national if r.status == "preliminary" and r.zone_code})
        print(f"  정상  {status['provider']}: 전국 발효 {nat_active}건·예비특보 {nat_prelim}건 (구역 단위), 대상 지역 발효 {active}건·예비특보 {prelim}건")
        if status.get("unresolved"):
            print(f"        ※ 구역 코드표에서 찾지 못한 구역 {status['unresolved']}곳 — config/warning_zones.json 갱신이 필요할 수 있습니다")
        for region in cfg.regions:
            for w in official[region.id].active + official[region.id].preliminary:
                kind = "예비" if w.status == "preliminary" else "발효"
                print(f"        {region.name}: {w.type_name}{w.level_name if w.status == 'active' else ' 예비특보'} [{kind}] 구역 {w.zone_name}")

    print("\n[코레일 열차운행정보] (전날까지의 실제 운행 기록 — 지연 통계용)")
    if not data_go_kr_key:
        print("  건너뜀 (인증키 미설정)")
    else:
        check_korail(data_go_kr_key, now, opts)

    print(f"\n예측 자료 {ok_models}개 사용 가능" + ("" if ok_models else " — 계산할 수 없습니다"))
    return 0 if ok_models else 1


def check_korail(key: str, now, opts: dict) -> None:
    """응답 형식과 하루 분량을 확인한다(처음 몇 행을 그대로 보여 준다)."""
    yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
    today = now.strftime("%Y%m%d")
    try:
        items, total = korail.fetch_page("travelerTrainRunInfo2", key, korail.day_conds(yesterday), 1, 6, opts)
        print(f"  정상  운행정보 {yesterday}: 전체 {total}행")
        for row in items:
            print("        " + ", ".join(f"{k}={row.get(k)!r}" for k in ("trn_no", "trn_run_sn", "stn_nm", "stn_cd", "mrnt_nm", "mrnt_cd", "uppln_dn_se_cd", "stop_se_nm", "stop_se_cd", "trn_arvl_dt", "trn_dptre_dt")))
        _, total_today = korail.fetch_page("travelerTrainRunInfo2", key, korail.day_conds(today), 1, 1, opts)
        print(f"        (오늘 {today} 운행정보: {total_today}행 — 0 이면 당일 자료는 제공되지 않는 것)")
    except FetchError as e:
        print(f"  실패  운행정보: {e}")
        if "REGISTERED" in str(e) or "ACCESS_DENIED" in str(e) or "403" in str(e):
            print("        → 공공데이터포털에서 '한국철도공사_열차운행정보' 활용신청이 필요합니다: https://www.data.go.kr/data/15125762/openapi.do")
        return
    try:
        items, total = korail.fetch_page("travelerTrainRunPlan2", key, korail.day_conds(today), 1, 4, opts)
        print(f"  정상  운행계획 {today}: 전체 {total}행")
        for row in items:
            print("        " + ", ".join(f"{k}={row.get(k)!r}" for k in ("trn_no", "dptre_stn_nm", "dptre_stn_cd", "arvl_stn_nm", "arvl_stn_cd", "trn_plan_dptre_dt", "trn_plan_arvl_dt")))
    except FetchError as e:
        print(f"  실패  운행계획: {e}")
    for code_type in ("mrnt_cd", "stop_se_cd", "uppln_dn_se_cd"):
        try:
            codes = korail.fetch_codes(key, code_type, opts)
            shown = ", ".join(f"{c}={v}" for c, v in list(codes.items())[:60])
            print(f"  정상  코드 {code_type}: {len(codes)}개 — {shown}")
        except FetchError as e:
            print(f"  실패  코드 {code_type}: {e}")
    try:
        _, total = korail.fetch_page("codes2", key, {"type::EQ": "stn_cd"}, 1, 1, opts)
        print(f"  정상  코드 stn_cd: {total}개 역")
    except FetchError as e:
        print(f"  실패  코드 stn_cd: {e}")


if __name__ == "__main__":
    sys.exit(main())
