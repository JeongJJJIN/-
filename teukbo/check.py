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

from .sources import kma_forecast, kma_ultra, kma_warnings, korail, open_meteo
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

    print("\n[실시간 자료] (초단기실황·초단기예보 — 몇 분마다 도는 live.yml 이 쓰는 것)")
    check_live(cfg, now, data_go_kr_key, apihub_key, opts)

    print("\n[선로 강우] (기상청 레이더 합성영상을 직접 읽음 — 인증키 없이)")
    check_radar(now, opts)

    print("\n[코레일 열차운행정보] (전날까지의 실제 운행 기록 — 지연 통계용)")
    if not data_go_kr_key:
        print("  건너뜀 (인증키 미설정)")
    else:
        check_korail(data_go_kr_key, now, opts)
        check_rail_day(data_go_kr_key, now, cfg)

    print(f"\n예측 자료 {ok_models}개 사용 가능" + ("" if ok_models else " — 계산할 수 없습니다"))
    return 0 if ok_models else 1


def check_live(cfg, now, data_go_kr_key, apihub_key, opts: dict) -> None:
    """초단기실황·초단기예보의 실제 응답을 그대로 보여 주고, 실시간 자료 한 벌을 끝까지 만들어 본다(저장하지 않음)."""
    from . import live

    if not (data_go_kr_key or apihub_key):
        print("  건너뜀 (인증키 미설정)")
        return
    region = cfg.regions[0]
    if data_go_kr_key:
        for operation, bases, field in (("getUltraSrtNcst", kma_ultra.ncst_bases(now), "obsrValue"), ("getUltraSrtFcst", kma_ultra.fcst_bases(now), "fcstValue")):
            for base in bases:
                try:
                    items = kma_ultra._fetch_grid(f"{kma_ultra.DATA_GO_KR_BASE}/{operation}", "serviceKey", data_go_kr_key, base, (region.nx, region.ny), opts, 1, operation)
                except FetchError as e:
                    print(f"  실패  {operation} 기준 {base[0]} {base[1]}: {e}")
                    continue
                first_time = items[0].get("fcstTime")
                shown = [it for it in items if it.get("fcstTime") == first_time]
                print(f"  정상  {operation} 기준 {base[0]} {base[1]} ({region.name} 격자 {region.nx},{region.ny}): {len(items)}행 — " + ", ".join(f"{it.get('category')}={it.get(field)!r}" for it in shown))
                if operation == "getUltraSrtFcst":
                    print("        시각별 RN1(강수)·SKY·PTY: " + " / ".join(
                        f"{t[:2]}시 {' '.join(str(it.get(field)) for it in items if it.get('fcstTime') == t and it.get('category') in ('RN1', 'SKY', 'PTY'))}"
                        for t in sorted({str(it.get('fcstTime')) for it in items}, key=lambda t: [str(i.get('fcstDate')) + t for i in items if str(i.get('fcstTime')) == t][0])))
                break
    doc = live.collect(cfg, now, {"DATA_GO_KR_SERVICE_KEY": data_go_kr_key or "", "KMA_APIHUB_KEY": apihub_key or ""}, None)
    print(f"  결과  {live.summary_line(doc)}")
    got_now = [r.name for r in cfg.regions if "now" in doc["regions"][r.id]]
    got_fcst = [r.name for r in cfg.regions if doc["regions"][r.id].get("fcst")]
    print(f"        지금 날씨 {len(got_now)}/{len(cfg.regions)}곳, 6시간 예보 {len(got_fcst)}/{len(cfg.regions)}곳")
    for r in cfg.regions[:3]:
        entry = doc["regions"][r.id]
        print(f"        {r.name}: 지금 {entry.get('now')}")
        print("          6시간 " + " | ".join(f"{row['t'][11:13]}시 {row['temp']}℃ 비 {row['rain'] or '없음'} 바람 {row['wind']}" for row in entry.get("fcst", [])))


def check_radar(now, opts: dict) -> None:
    """가장 최근 레이더 영상을 받아 범례·크기를 확인하고, 선로 위의 강우를 한 번 계산해 본다."""
    from . import radar

    try:
        found = radar.latest_stamp(now, [], opts)
    except Exception as e:  # 점검용 — 어떤 오류든 보여 준다
        print(f"  실패  {type(e).__name__}: {e}")
        return
    if found is None or found[1] is None:
        print("  실패  최근 35분 안의 레이더 영상을 받지 못했습니다 (서버가 이 위치의 접속을 막거나 영상 주소가 바뀌었을 수 있습니다)")
        return
    stamp, frame = found
    counts: dict[float, int] = {}
    for y in range(radar.FRAME[1], radar.FRAME[3] + 1):
        for x in range(radar.FRAME[0], radar.FRAME[2] + 1):
            rate = frame.rate_at(x, y)
            if rate is not None:
                counts[rate] = counts.get(rate, 0) + 1
    print(f"  정상  영상 {stamp} ({frame.width}x{frame.height}), 범례 색 일치: {frame.legend_ok()}, 비구름 화소 {sum(v for k, v in counts.items() if k >= radar.RAIN_MIN)}개, 가장 센 구간 {max(counts) if counts else 0:g}mm/h 이상")
    samples = radar.RailSamples.load()
    rainy = samples.read(frame)
    lines: dict[str, float] = {}
    for i, rate in rainy.items():
        name = samples.names[samples.points[i][0]]
        lines[name] = max(lines.get(name, 0.0), rate)
    print(f"        선로 표본점 {len(samples.points)}개 중 비가 있는 점 {len(rainy)}개" + ("" if not lines else " — " + ", ".join(f"{name} {rate:g}mm/h 이상" for name, rate in sorted(lines.items(), key=lambda kv: -kv[1]))))


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
    plan_fields = ("run_ymd", "trn_no", "dptre_stn_nm", "dptre_stn_cd", "arvl_stn_nm", "arvl_stn_cd", "trn_plan_dptre_dt", "trn_plan_arvl_dt")
    tomorrow = (now + timedelta(days=1)).strftime("%Y%m%d")
    probes = [
        (f"어제 {yesterday}", korail.day_conds(yesterday)),
        (f"오늘 {today}", korail.day_conds(today)),
        (f"내일 {tomorrow}", korail.day_conds(tomorrow)),
        ("조건 없음", None),
        (f"어제 이후(GTE {yesterday})", {"run_ymd::GTE": yesterday}),
        ("어제(하이픈 형식)", korail.day_conds(f"{yesterday[:4]}-{yesterday[4:6]}-{yesterday[6:]}")),
    ]
    for label, conds in probes:
        try:
            items, total = korail.fetch_page("travelerTrainRunPlan2", key, conds, 1, 3, opts)
            print(f"  정상  운행계획 [{label}]: 전체 {total}행")
            for row in items:
                print("        " + ", ".join(f"{k}={row.get(k)!r}" for k in plan_fields))
        except FetchError as e:
            print(f"  실패  운행계획 [{label}]: {e}")
    # 운행정보: 종착 행의 모양과 한 열차의 전체 정차 기록
    try:
        items, total = korail.fetch_page("travelerTrainRunInfo2", key, {**korail.day_conds(yesterday), "stn_nm::EQ": "순천"}, 1, 5, opts)
        print(f"  정상  운행정보 [어제 순천역]: 전체 {total}행")
        for row in items:
            print("        " + ", ".join(f"{k}={row.get(k)!r}" for k in ("trn_no", "trn_run_sn", "stn_nm", "mrnt_nm", "uppln_dn_se_cd", "stop_se_nm", "trn_arvl_dt", "trn_dptre_dt")))
        items, _ = korail.fetch_page("travelerTrainRunInfo2", key, korail.day_conds(yesterday), 10, 1000, opts)
        kinds: dict[str, int] = {}
        lines: dict[str, int] = {}
        for row in items:
            kinds[str(row.get("stop_se_nm"))] = kinds.get(str(row.get("stop_se_nm")), 0) + 1
            lines[str(row.get("mrnt_nm"))] = lines.get(str(row.get("mrnt_nm")), 0) + 1
        print(f"        마지막 쪽({len(items)}행) 정차구분: {kinds}")
        print(f"        마지막 쪽 주운행선: {lines}")
        if items:
            last_train = items[-1].get("trn_no")
            tail = [r for r in items if r.get("trn_no") == last_train]
            print(f"        열차 {last_train} 기록 {len(tail)}행: " + " → ".join(f"{r.get('stn_nm')}({r.get('stop_se_nm')},{str(r.get('trn_arvl_dt'))[11:16]}/{str(r.get('trn_dptre_dt'))[11:16]})" for r in tail[:30]))
    except FetchError as e:
        print(f"  실패  운행정보 추가 확인: {e}")
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




def check_rail_day(key: str, now, cfg) -> None:
    """어제 하루치 열차 지연 집계를 실제 자료로 끝까지 돌려 본다(임시 폴더에만 쓰고 버린다)."""
    import tempfile
    from collections import Counter
    from pathlib import Path

    from . import rail, rail_run

    print("열차 지연 집계 시험 (가장 최근 하루, 특보 이력 최근 7일)")
    tmp = Path(tempfile.mkdtemp())
    days = rail.window_days(now, 7)
    try:
        events = rail_run.refresh_events(tmp, key, days, now, cfg.settings.http)
        intervals = rail.build_intervals(events)
        print(f"  정상  특보 발표 이력 {len(events)}건 → 발효 구간 {len(intervals)}개: " + ", ".join(f"{k} {v}" for k, v in Counter(i.type_name + i.level_name for i in intervals).most_common(12)))
        day = None
        for ymd in days[:3]:  # 전날 기록은 다음 날 새벽~오전에 올라온다. 아직 없으면 그 전날로 시험한다
            day = rail_run.process_day(cfg, key, ymd, rail.WarningTimeline(intervals), rail_run.load_station_zones(), now)
            if day is not None:
                break
            print(f"        {ymd} 운행 기록은 아직 제공되지 않습니다")
    except FetchError as e:
        print(f"  실패  {e}")
        return
    if day is None:
        print("  실패  최근 3일의 운행 기록이 없습니다")
        return
    print(f"  정상  {day['date']} 열차 수 {day['counts']}")
    print(f"        전체 {day['all']}")
    print(f"        특보 구간을 지나지 않음 {day['clear']}")
    print(f"        특보별 {day['by_warning'] or '(특보 구간을 지난 열차 없음)'}")
    print("        종류별 " + ", ".join(f"{k} {v.get('n')}편 평균 {v.get('mean')}분" for k, v in day["by_class"].items()))
    print("        노선별 " + ", ".join(f"{k} {v.get('n')}편" for k, v in sorted(day["by_line"].items(), key=lambda kv: -kv[1].get("n", 0))))
    idx = {name: i for i, name in enumerate(day["fields"])}
    late = sorted((t for t in day["trains"] if t[idx["arr_delay"]] is not None), key=lambda t: -t[idx["arr_delay"]])[:5]
    for t in late:
        print("        지연 큰 열차: " + ", ".join(f"{name}={t[idx[name]]!r}" for name in ("trn_no", "class", "line", "origin", "dest", "plan_arr", "arr_delay", "warnings")))
    unmatched = day.get("unmatched_stations") or {}
    print(f"        특보구역을 찾지 못한 역 {len(unmatched)}곳: {unmatched or '없음'}")


if __name__ == "__main__":
    sys.exit(main())
