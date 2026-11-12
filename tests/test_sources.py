import io
import json
import unittest
import urllib.error
from unittest import mock

from teukbo import http
from teukbo.http import FetchError
from teukbo.sources import kma_forecast, kma_warnings, open_meteo
from teukbo.sources.datagokr import NoData, extract_items

from .helpers import config, kst

CFG = config()
LEVELS = [v["name"] for v in CFG.thresholds.levels.values()]


def region(rid):
    return next(r for r in CFG.regions if r.id == rid)


# ---------------------------------------------------------------- Open-Meteo


class OpenMeteoTest(unittest.TestCase):
    def setUp(self):
        self.regions = [region("suncheon"), region("beolgyo")]
        self.times = ["2026-11-12T21:00", "2026-11-12T22:00", "2026-11-12T23:00"]

    def _loc(self, suffix="", gust=(8.0, 9.0, None)):
        return {
            "latitude": 34.95,
            "utc_offset_seconds": 32400,
            "hourly": {
                "time": self.times,
                f"precipitation{suffix}": [0.0, 1.2, 3.4],
                f"snowfall{suffix}": [0.0, 0.0, 0.0],
                f"temperature_2m{suffix}": [10.1, 9.8, 9.5],
                f"relative_humidity_2m{suffix}": [70, 72, 75],
                f"wind_speed_10m{suffix}": [3.1, 3.3, 4.0],
                f"wind_gusts_10m{suffix}": list(gust),
            },
        }

    def test_request_parameters(self):
        params = open_meteo.build_params(self.regions, "ecmwf_ifs025", 36, 72)
        self.assertEqual(params["latitude"], "34.9507,34.8430")
        self.assertEqual(params["longitude"], "127.4872,127.3410")
        self.assertEqual(params["models"], "ecmwf_ifs025")
        self.assertEqual(params["wind_speed_unit"], "ms")
        self.assertEqual(params["timezone"], "Asia/Seoul")
        self.assertEqual((params["past_hours"], params["forecast_hours"]), (36, 72))
        self.assertEqual(params["hourly"].split(","), ["precipitation", "snowfall", "temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_gusts_10m"])
        url = http.build_url(open_meteo.ENDPOINT, params)
        self.assertIn("latitude=34.9507,34.8430", url)  # 쉼표는 그대로 보낸다

    def test_parse_multiple_locations_in_order(self):
        result = open_meteo.parse_response([self._loc(), self._loc()], self.regions, "ecmwf_ifs025")
        self.assertEqual(set(result), {"suncheon", "beolgyo"})
        ms = result["suncheon"]
        self.assertEqual(ms.times[0], kst(2026, 11, 12, 21))
        self.assertEqual(ms.values["precip"], [0.0, 1.2, 3.4])
        self.assertEqual(ms.values["gust"], [8.0, 9.0, None])
        self.assertEqual(ms.values["rh"], [70.0, 72.0, 75.0])

    def test_parse_suffixed_keys(self):
        loc = self._loc("_ncep_gfs_seamless")
        result = open_meteo.parse_response([loc, loc], self.regions, "ncep_gfs_seamless")
        self.assertEqual(result["suncheon"].values["wind"], [3.1, 3.3, 4.0])

    def test_all_null_variable_is_dropped(self):
        loc = self._loc(gust=(None, None, None))  # JMA 처럼 돌풍 자료가 없는 모델
        result = open_meteo.parse_response([loc, loc], self.regions, "jma_seamless")
        self.assertNotIn("gust", result["suncheon"].values)
        self.assertIn("wind", result["suncheon"].values)

    def test_single_location_object(self):
        result = open_meteo.parse_response(self._loc(), self.regions[:1], "ecmwf_ifs025")
        self.assertEqual(list(result), ["suncheon"])

    def test_error_payload(self):
        with self.assertRaises(FetchError):
            open_meteo.parse_response({"error": True, "reason": "Daily API request limit exceeded."}, self.regions, "x")

    def test_location_count_mismatch(self):
        with self.assertRaises(FetchError):
            open_meteo.parse_response([self._loc()], self.regions, "x")

    def test_one_failing_model_does_not_stop_others(self):
        models = [m for m in CFG.settings.models if m.source == "open_meteo"][:2]

        def fake(_url, params, **_kw):
            if params["models"] == models[0].id:
                raise FetchError("open-meteo: HTTP 400")
            return [self._loc(), self._loc()]

        with mock.patch.object(open_meteo, "get_json", side_effect=fake):
            data, errors = open_meteo.fetch_all(self.regions, models, 36, 72)
        self.assertEqual(list(data), [models[1].id])
        self.assertIn(models[0].id, errors)


# ---------------------------------------------------------------- 단기예보


def fcst_item(date, time, category, value):
    return {"baseDate": "20261112", "baseTime": "2000", "category": category, "fcstDate": date, "fcstTime": time, "fcstValue": value, "nx": 70, "ny": 70}


class KmaForecastTest(unittest.TestCase):
    def test_base_time_selection(self):
        self.assertEqual(kma_forecast.candidate_bases(kst(2026, 11, 12, 21, 12), 15)[0], ("20261112", "2000"))
        self.assertEqual(kma_forecast.candidate_bases(kst(2026, 11, 12, 20, 12), 15)[0], ("20261112", "1700"))  # 20시 발표분은 아직
        self.assertEqual(kma_forecast.candidate_bases(kst(2026, 11, 12, 20, 16), 15)[0], ("20261112", "2000"))
        self.assertEqual(kma_forecast.candidate_bases(kst(2026, 11, 12, 1, 0), 15)[:2], [("20261111", "2300"), ("20261111", "2000")])

    def test_precipitation_categories(self):
        self.assertEqual(kma_forecast.parse_pcp("강수없음"), 0.0)
        self.assertEqual(kma_forecast.parse_pcp("1mm 미만"), 0.5)
        self.assertEqual(kma_forecast.parse_pcp("6.2mm"), 6.2)
        self.assertEqual(kma_forecast.parse_pcp("30.0~50.0mm"), 40.0)
        self.assertEqual(kma_forecast.parse_pcp("50.0mm 이상"), 50.0)
        self.assertEqual(kma_forecast.parse_pcp("50.0mm이상"), 50.0)
        self.assertEqual(kma_forecast.parse_pcp("-"), 0.0)
        self.assertIsNone(kma_forecast.parse_pcp(None))

    def test_snow_categories(self):
        self.assertEqual(kma_forecast.parse_sno("적설없음"), 0.0)
        self.assertEqual(kma_forecast.parse_sno("0.5cm 미만"), 0.3)
        self.assertEqual(kma_forecast.parse_sno("1.3cm"), 1.3)
        self.assertEqual(kma_forecast.parse_sno("5.0cm 이상"), 5.0)

    def test_parse_items_keeps_only_hourly_part(self):
        items = []
        for hour, pcp in (("2100", "강수없음"), ("2200", "6.2mm"), ("2300", "30.0~50.0mm")):
            items += [
                fcst_item("20261112", hour, "TMP", "9"),
                fcst_item("20261112", hour, "WSD", "3.4"),
                fcst_item("20261112", hour, "REH", "80"),
                fcst_item("20261112", hour, "PCP", pcp),
                fcst_item("20261112", hour, "SNO", "적설없음"),
                fcst_item("20261112", hour, "SKY", "4"),
            ]
        # 3시간 간격(정성 코드) 구간: PCP "1" 은 1mm 가 아니라 '약한 비' 코드 → 읽으면 안 된다
        items += [fcst_item("20261113", "0200", "PCP", "1"), fcst_item("20261113", "0200", "WSD", "3"), fcst_item("20261113", "0200", "TMP", "7")]
        ms = kma_forecast.parse_items(items, "kma_dfs")
        self.assertEqual(ms.times, [kst(2026, 11, 12, 21), kst(2026, 11, 12, 22), kst(2026, 11, 12, 23)])
        self.assertEqual(ms.values["precip"], [0.0, 6.2, 40.0])
        self.assertEqual(ms.values["wind"], [3.4, 3.4, 3.4])
        self.assertEqual(ms.values["temp"], [9.0, 9.0, 9.0])
        self.assertNotIn("gust", ms.values)

    def test_missing_values_are_none(self):
        items = [fcst_item("20261112", "2100", "TMP", "-999"), fcst_item("20261112", "2100", "WSD", "2.0")]
        ms = kma_forecast.parse_items(items, "kma_dfs")
        self.assertNotIn("temp", ms.values)
        self.assertEqual(ms.values["wind"], [2.0])

    def test_response_envelopes(self):
        ok = {"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"}, "body": {"items": {"item": [{"a": 1}]}, "totalCount": 907}}}
        self.assertEqual(extract_items(ok, "x"), ([{"a": 1}], 907))
        single = {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": {"a": 1}}, "totalCount": 1}}}
        self.assertEqual(extract_items(single, "x")[0], [{"a": 1}])
        with self.assertRaises(NoData):
            extract_items({"response": {"header": {"resultCode": "03", "resultMsg": "NO_DATA"}}}, "x")
        with self.assertRaises(NoData):
            extract_items({"response": {"header": {"resultCode": "00"}, "body": {"items": "", "totalCount": 0}}}, "x")
        with self.assertRaisesRegex(FetchError, "99"):
            extract_items({"response": {"header": {"resultCode": "99", "resultMsg": "최대 조회 기간은 오늘 기준으로 6일 전까지입니다."}}}, "x")
        gateway = {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"errMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "returnAuthMsg": "등록되지 않은 서비스키", "returnReasonCode": "30"}}}
        with self.assertRaisesRegex(FetchError, "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"):
            extract_items(gateway, "x")
        with self.assertRaisesRegex(FetchError, "유효한 인증키"):
            extract_items({"result": {"status": 401, "message": "유효한 인증키가 아닙니다."}}, "x")

    def test_no_keys_means_not_configured(self):
        data, status = kma_forecast.fetch(CFG.regions, "kma_dfs", kst(2026, 11, 12, 21), data_go_kr_key=None, apihub_key=None)
        self.assertEqual(data, {})
        self.assertEqual((status["ok"], status["configured"]), (False, False))

    def test_falls_back_to_apihub_and_dedupes_grids(self):
        calls = []
        items = [fcst_item("20261112", "2100", "TMP", "9"), fcst_item("20261112", "2100", "PCP", "강수없음")]

        def fake(url, params, **_kw):
            calls.append((url, params))
            if "apis.data.go.kr" in url:
                raise FetchError("단기예보: TimeoutError")
            return {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": items}, "totalCount": len(items)}}}

        regions = [region("boseong"), region("suncheon"), region("beolgyo")]
        with mock.patch.object(kma_forecast, "get_json", side_effect=fake):
            data, status = kma_forecast.fetch(regions, "kma_dfs", kst(2026, 11, 12, 21, 30), data_go_kr_key="k1", apihub_key="k2")
        self.assertTrue(status["ok"])
        self.assertEqual(status["provider"], "기상청 API허브")
        self.assertEqual(set(data), {"boseong", "suncheon", "beolgyo"})
        hub_calls = [p for u, p in calls if "apihub" in u]
        self.assertEqual(len(hub_calls), 3)  # 격자 3개
        self.assertTrue(all("authKey" in p and "serviceKey" not in p for p in hub_calls))
        self.assertEqual(hub_calls[0]["base_time"], "2000")

    def test_previous_base_when_latest_not_ready(self):
        seen = []

        def fake(_url, params, **_kw):
            seen.append(params["base_time"])
            if params["base_time"] == "2000":
                return {"response": {"header": {"resultCode": "03", "resultMsg": "NO_DATA"}}}
            item = fcst_item("20261112", "2100", "TMP", "9")
            return {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": [item]}, "totalCount": 1}}}

        with mock.patch.object(kma_forecast, "get_json", side_effect=fake):
            data, status = kma_forecast.fetch([region("suncheon")], "kma_dfs", kst(2026, 11, 12, 20, 20), data_go_kr_key="k", apihub_key=None)
        self.assertEqual(seen, ["2000", "1700"])
        self.assertEqual(status["base"], "20261112 1700")
        self.assertIn("suncheon", data)


# ---------------------------------------------------------------- 특보


T6 = (
    "o 강풍주의보 : 전라남도(거문도.초도, 완도여서도), 제주도(제주도산지, 추자도)\r\n"
    "o 풍랑주의보 : 서해남부북쪽바깥먼바다, 남해서부서쪽먼바다\r\n"
    "o 호우주의보 : 전라남도(거문도.초도, 완도(여서도 제외), 완도여서도, 순천, 고흥북부), 제주도(제주도산지)\r\n"
    "o 폭염중대경보 : 경상북도(김천시북부)\r\n"
    "o 건조주의보 : 전라남도"
)
T7 = (
    "(1) 풍랑 예비특보\r\no 09월 26일 새벽(00시~06시) : 제주도남쪽바깥먼바다, 제주도남서쪽안쪽먼바다\r\n"
    "(2) 호우 예비특보\r\no 11월 13일 오전(06시~12시) : 전라남도(순천, 광양, 보성), 경상남도(하동)\r\n"
    "o 11월 13일 오후(12시~18시) : 전라남도(구례평지)"
)


class WarningTextTest(unittest.TestCase):
    def test_none_text(self):
        for text in ("o 없 음", "o 없음\r\n\r\n", "", None):
            self.assertTrue(kma_warnings.is_none_text(text), text)
            self.assertEqual(kma_warnings.parse_active_text(text, LEVELS), [])
            self.assertEqual(kma_warnings.parse_preliminary_text(text, LEVELS), [])
        self.assertFalse(kma_warnings.is_none_text("o 호우주의보 : 전라남도"))

    def test_split_respects_parentheses(self):
        self.assertEqual(
            kma_warnings.split_top_level("전라남도(거문도.초도, 완도(여서도 제외)), 울릉도.독도"),
            ["전라남도(거문도.초도, 완도(여서도 제외))", "울릉도.독도"],
        )

    def test_split_name(self):
        self.assertEqual(kma_warnings.split_name("호우주의보", LEVELS), ("호우", "주의보"))
        self.assertEqual(kma_warnings.split_name("폭염중대경보", LEVELS), ("폭염", "중대경보"))
        self.assertEqual(kma_warnings.split_name("폭염경보", LEVELS), ("폭염", "경보"))
        self.assertEqual(kma_warnings.split_name("열대야주의보", LEVELS), ("열대야", "주의보"))
        self.assertEqual(kma_warnings.split_name("호우", LEVELS), ("호우", ""))

    def test_active_assigned_to_regions(self):
        official = kma_warnings.assign_to_regions(kma_warnings.parse_active_text(T6, LEVELS), CFG.regions, CFG.thresholds)

        def names(rid):
            return {(w.type_name + w.level_name, w.zone_name) for w in official[rid].active}

        self.assertEqual(names("suncheon"), {("호우주의보", "순천"), ("건조주의보", "전라남도")})
        self.assertEqual(names("yeosu"), {("강풍주의보", "거문도.초도"), ("호우주의보", "거문도.초도"), ("건조주의보", "전라남도")})
        self.assertEqual(names("wando"), {("강풍주의보", "완도여서도"), ("호우주의보", "완도(여서도 제외)·완도여서도"), ("건조주의보", "전라남도")})
        self.assertEqual(names("goheung"), {("호우주의보", "고흥북부"), ("건조주의보", "전라남도")})
        self.assertEqual(names("beolgyo"), {("건조주의보", "전라남도")})  # 도 전체 특보는 모든 지역에
        self.assertTrue(all(o.available for o in official.values()))
        rain = next(w for w in official["suncheon"].active if w.type_name == "호우")
        self.assertEqual((rain.type, rain.level), ("heavy_rain", "advisory"))
        dry = next(w for w in official["suncheon"].active if w.type_name == "건조")
        self.assertIsNone(dry.type)  # 계산 대상이 아닌 종류도 목록에는 남긴다

    def test_other_province_with_same_style_is_not_matched(self):
        raws = kma_warnings.parse_active_text("o 호우경보 : 경상남도(순천, 보성)", LEVELS)  # 가상의 동명 구역
        official = kma_warnings.assign_to_regions(raws, CFG.regions, CFG.thresholds)
        self.assertEqual(official["suncheon"].active, [])

    def test_preliminary_assigned_to_regions(self):
        official = kma_warnings.assign_to_regions(kma_warnings.parse_preliminary_text(T7, LEVELS), CFG.regions, CFG.thresholds)
        pre = official["suncheon"].preliminary
        self.assertEqual(len(pre), 1)
        self.assertEqual((pre[0].type, pre[0].level, pre[0].expected_text), ("heavy_rain", None, "11월 13일 오전(06시~12시)"))
        self.assertEqual(official["beolgyo"].preliminary[0].zone_name, "보성")  # 벌교는 보성 구역
        self.assertEqual(official["gurye"].preliminary[0].expected_text, "11월 13일 오후(12시~18시)")
        self.assertEqual(official["mokpo"].preliminary, [])

    def test_preliminary_header_and_bullet_on_one_line(self):
        text = "(1) 강풍 예비특보 o 06월 07일 아침 : 울릉도.독도 \n (2) 풍랑 예비특보 o 06월 07일 아침 : 동해중부전해상, 동해남부먼바다"
        raws = kma_warnings.parse_preliminary_text(text, LEVELS)
        self.assertEqual([(r.type_name, r.zone_name) for r in raws], [("강풍", "울릉도.독도"), ("풍랑", "동해중부전해상"), ("풍랑", "동해남부먼바다")])

    def test_start_times_from_code_history(self):
        items = [
            {"areaCode": "L1051200", "cancel": "0", "command": "1", "tmFc": 202611121700, "warnVar": 2, "warnStress": 0, "startTime": 202611121800, "endTime": 0},
            {"areaCode": "L1051200", "cancel": "0", "command": "2", "tmFc": 202611110900, "warnVar": 2, "warnStress": 0, "startTime": 0, "endTime": 202611110900},
            {"areaCode": "L1051200", "cancel": "1", "command": "1", "tmFc": 202611121730, "warnVar": 2, "warnStress": 0, "startTime": 202611122300},
            {"areaCode": "L1072610", "cancel": "0", "command": "7", "tmFc": 202608031000, "warnVar": 12, "warnStress": 2, "startTime": 202608031100},
            {"areaCode": "L1130110", "cancel": "0", "command": "1", "tmFc": 202608081600, "warnVar": 13, "warnStress": 0, "startTime": 202608081700},
        ]
        starts = kma_warnings.parse_pwn_cd(items)
        self.assertEqual(starts[("L1051200", "호우", "주의보")], kst(2026, 11, 12, 18))  # 해제·취소 행은 무시
        self.assertEqual(starts[("L1072610", "폭염", "중대경보")], kst(2026, 8, 3, 11))
        self.assertEqual(starts[("L1130110", "열대야", "주의보")], kst(2026, 8, 8, 17))

    def test_datagokr_flow_attaches_start_time(self):
        def fake(url, params, **_kw):
            if url.endswith("getPwnStatus"):
                item = {"t6": T6, "t7": T7, "tmFc": 202611121700, "tmEf": "202611121800", "other": "o 없음"}
            else:
                self.assertEqual(params["fromTmFc"], params["toTmFc"])  # 하루 단위 조회
                if params["fromTmFc"] != "20261112":
                    return {"response": {"header": {"resultCode": "03", "resultMsg": "NO_DATA"}}}
                item = {"areaCode": "L1051200", "cancel": "0", "command": "1", "tmFc": 202611121700, "warnVar": 2, "warnStress": 0, "startTime": 202611121800}
            return {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": [item]}, "totalCount": 1}}}

        with mock.patch.object(kma_warnings, "get_json", side_effect=fake):
            official, status, national = kma_warnings.fetch(CFG.regions, CFG.thresholds, kst(2026, 11, 12, 21), data_go_kr_key="k", apihub_key=None)
        self.assertEqual((status["ok"], status["provider"]), (True, "공공데이터포털"))
        rain = next(w for w in official["suncheon"].active if w.type_name == "호우")
        self.assertEqual(rain.effective_from, kst(2026, 11, 12, 18))
        self.assertEqual(rain.issued_at, kst(2026, 11, 12, 17))

    def test_unparseable_text_is_failure_not_empty(self):
        def fake(_url, _params, **_kw):
            item = {"t6": "특보 형식이 완전히 바뀐 문장", "t7": "o 없음", "tmFc": 202611121700}
            return {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": [item]}, "totalCount": 1}}}

        with mock.patch.object(kma_warnings, "get_json", side_effect=fake):
            official, status, national = kma_warnings.fetch(CFG.regions, CFG.thresholds, kst(2026, 11, 12, 21), data_go_kr_key="k", apihub_key=None)
        self.assertFalse(status["ok"])
        self.assertFalse(official["suncheon"].available)  # '특보 없음'이 아니라 '확인 불가'

    def test_no_keys(self):
        official, status, national = kma_warnings.fetch(CFG.regions, CFG.thresholds, kst(2026, 11, 12, 21), data_go_kr_key=None, apihub_key=None)
        self.assertEqual((status["ok"], status["configured"]), (False, False))
        self.assertFalse(official["suncheon"].available)


class ZoneTableTest(unittest.TestCase):
    """전국 특보구역 코드표로 이름을 코드로 바꾸는 경로 (실제 운영 경로)."""

    @classmethod
    def setUpClass(cls):
        from teukbo.zones import load_zone_table

        cls.table = load_zone_table()

    def test_table_shape(self):
        self.assertEqual(len(self.table), 414)
        self.assertEqual(len(self.table.leaf_codes(land_only=True)), 235)  # 2026-06-01 개편: 육상 235개 구역

    def test_resolve_names(self):
        r = self.table.resolve
        self.assertEqual(r("순천", "전라남도"), ["L1051200"])
        self.assertEqual(r("순천시", "전라남도"), ["L1051200"])  # 정식 이름으로 적혀도
        self.assertEqual(r("완도(여서도 제외)", "전라남도"), ["L1053310"])
        self.assertEqual(r("고성", "강원도"), ["L1022210", "L1022220"])  # 세분 구역으로 펼침
        self.assertEqual(r("고성", "경상남도"), ["L1082300"])
        self.assertEqual(r("고성"), [])  # 상위 구역 없이는 어느 고성인지 알 수 없다
        self.assertEqual(r("광주"), ["L1130110", "L1130120"])  # 광주광역시 (경기 광주가 아님)
        self.assertEqual(r("광주", "경기도"), ["L1013400"])
        self.assertEqual(r("울릉도.독도"), ["L1072100"])
        self.assertEqual(r("흑산도.홍도"), ["L1052500"])
        self.assertEqual(r("순천", "경상남도"), [])
        self.assertEqual(len(r("전라남도")), 31)
        self.assertEqual(r("서해남부북쪽바깥먼바다"), ["S1232120"])

    def test_text_is_resolved_to_leaf_codes(self):
        raws = kma_warnings.parse_active_text(T6, LEVELS, None, self.table)
        self.assertTrue(all(r.zone_code for r in raws))  # 전부 코드로 바뀜
        by_name = {}
        for r in raws:
            by_name.setdefault(r.type_name + r.level_name, []).append(r.zone_code)
        self.assertEqual(by_name["강풍주의보"], ["L1052600", "L1053320", "L1090500", "L1091000"])
        self.assertEqual(by_name["풍랑주의보"], ["S1232120", "S1322100"])
        self.assertEqual(by_name["폭염중대경보"], ["L1072610"])
        self.assertEqual(len(by_name["건조주의보"]), 31)  # '전라남도' → 전남의 모든 구역
        self.assertEqual(len(set(by_name["호우주의보"])), 6)

    def test_regions_from_resolved_codes(self):
        raws = kma_warnings.parse_active_text(T6, LEVELS, None, self.table)
        official = kma_warnings.assign_to_regions(raws, CFG.regions, CFG.thresholds)

        def names(rid):
            return {(w.type_name + w.level_name, w.zone_name) for w in official[rid].active}

        self.assertEqual(names("suncheon"), {("호우주의보", "순천"), ("건조주의보", "순천")})
        self.assertEqual(names("yeosu"), {("강풍주의보", "거문도.초도"), ("호우주의보", "거문도.초도"), ("건조주의보", "여수·거문도.초도")})
        self.assertEqual(names("goheung"), {("호우주의보", "고흥북부"), ("건조주의보", "고흥북부·고흥남부")})
        self.assertEqual(names("beolgyo"), {("건조주의보", "보성")})
        self.assertEqual(names("sinan"), {("건조주의보", "신안(흑산면제외)")})  # 흑산도.홍도는 '전라남도'에 포함되지 않는 별도 구역

    def test_preliminary_resolved(self):
        raws = kma_warnings.parse_preliminary_text(T7, LEVELS, None, self.table)
        codes = {(r.type_name, r.zone_code, r.expected_text) for r in raws}
        self.assertIn(("호우", "L1051200", "11월 13일 오전(06시~12시)"), codes)
        self.assertIn(("호우", "L1053010", "11월 13일 오후(12시~18시)"), codes)  # 구례평지
        hadong = self.table.resolve("하동", "경상남도")  # 대상 지역 밖의 구역도 전국 목록에는 들어간다
        self.assertTrue(hadong)
        self.assertTrue(all(("호우", code, "11월 13일 오전(06시~12시)") in codes for code in hadong))

    def test_unknown_zone_is_kept_without_code(self):
        raws = kma_warnings.parse_active_text("o 호우주의보 : 전라남도(순천, 새로생긴구역)", LEVELS, None, self.table)
        self.assertEqual([(r.zone_name, r.zone_code) for r in raws], [("순천", "L1051200"), ("새로생긴구역", None)])

    def test_apihub_parent_code_is_expanded(self):
        rows = [{"REG_UP": "L1000000", "REG_UP_KO": "전국", "REG_ID": "L1053100", "REG_KO": "고흥군", "TM_FC": "202611121700", "TM_EF": "202611121800", "WRN": "호우", "LVL": "주의", "CMD": "발표", "ED_TM": ""}]
        raws = kma_warnings.resolve_zones(kma_warnings.parse_apihub_now(rows), self.table)
        self.assertEqual([(r.zone_code, r.zone_name) for r in raws], [("L1053110", "고흥북부"), ("L1053120", "고흥남부")])
        self.assertTrue(all(r.effective_from == kst(2026, 11, 12, 18) for r in raws))

    def test_fetch_returns_national_list_with_start_time(self):
        def fake(url, params, **_kw):
            if url.endswith("getPwnStatus"):
                item = {"t6": T6, "t7": T7, "tmFc": 202611121700, "tmEf": "202611121800", "other": "o 없음"}
            elif params["fromTmFc"] != "20261112":
                return {"response": {"header": {"resultCode": "03", "resultMsg": "NO_DATA"}}}
            else:
                item = {"areaCode": "L1051200", "cancel": "0", "command": "1", "tmFc": 202611121700, "warnVar": 2, "warnStress": 0, "startTime": 202611121800}
            return {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": [item]}, "totalCount": 1}}}

        with mock.patch.object(kma_warnings, "get_json", side_effect=fake):
            official, status, national = kma_warnings.fetch(CFG.regions, CFG.thresholds, kst(2026, 11, 12, 21), data_go_kr_key="k", apihub_key=None)
        self.assertEqual(status["unresolved"], 0)
        rain = next(r for r in national if r.zone_code == "L1051200" and r.type_name == "호우" and r.status == "active")
        self.assertEqual(rain.effective_from, kst(2026, 11, 12, 18))
        self.assertTrue(any(r.zone_code == "L1072610" for r in national))  # 대상 지역 밖(경북)도 전국 목록에는 있다
        self.assertEqual(next(w for w in official["suncheon"].active if w.type_name == "호우").effective_from, kst(2026, 11, 12, 18))


APIHUB_ROWS = [
    {"REG_UP": "L1050000", "REG_UP_KO": "전라남도                                ", "REG_ID": "L1052100", "REG_KO": "목포시                                  ", "TM_FC": "202209051300", "TM_EF": "202209051400", "WRN": "태풍  ", "LVL": "주의    ", "CMD": "발표", "ED_TM": ""},
    {"REG_UP": "L1053100", "REG_UP_KO": "고흥군", "REG_ID": "L1053110", "REG_KO": "고흥군북부", "TM_FC": "202209050500", "TM_EF": "202209052358", "WRN": "호우  ", "LVL": "예비    ", "CMD": "발표", "ED_TM": ""},
    {"REG_UP": "L1050000", "REG_UP_KO": "전라남도", "REG_ID": "L1050900", "REG_KO": "보성군", "TM_FC": "202608081600", "TM_EF": "202608081700", "WRN": "열대야", "LVL": "주의    ", "CMD": "발표", "ED_TM": ""},
    {"REG_UP": "L1050000", "REG_UP_KO": "전라남도", "REG_ID": "L1051200", "REG_KO": "순천시", "TM_FC": "202608081600", "TM_EF": "202608081700", "WRN": "폭염", "LVL": "중대경보", "CMD": "변경", "ED_TM": ""},
    {"REG_UP": "S1131000", "REG_UP_KO": "동해남부앞바다", "REG_ID": "S1131200", "REG_KO": "경북남부앞바다", "TM_FC": "202209051610", "TM_EF": "202209051800", "WRN": "풍랑  ", "LVL": "주의    ", "CMD": "발표", "ED_TM": "07일 오전(09시~12시),"},
]


class ApiHubWarningTest(unittest.TestCase):
    def test_json_rows(self):
        raws = kma_warnings.parse_apihub_body(json.dumps(APIHUB_ROWS, ensure_ascii=False).encode("utf-8"))
        official = kma_warnings.assign_to_regions(raws, CFG.regions, CFG.thresholds)
        typhoon = official["mokpo"].active[0]
        self.assertEqual((typhoon.type, typhoon.level, typhoon.effective_from), ("typhoon", "advisory", kst(2022, 9, 5, 14)))
        pre = official["goheung"].preliminary[0]
        self.assertEqual((pre.type, pre.level, pre.expected_text), ("heavy_rain", None, "9월 5일 밤(~24시)"))
        self.assertEqual(official["beolgyo"].active[0].type, "tropical_night")
        severe = official["suncheon"].active[0]
        self.assertEqual((severe.type, severe.level), ("heat_wave", "severe"))

    def test_empty_array_means_no_warnings(self):
        self.assertEqual(kma_warnings.parse_apihub_body(b"[]"), [])

    def test_error_object(self):
        body = '{"result": {"status": 403, "message": "활용신청이 필요한 API 입니다."}}'.encode("utf-8")
        with self.assertRaisesRegex(FetchError, "활용신청"):
            kma_warnings.parse_apihub_body(body)

    def test_text_form_in_euc_kr(self):
        text = (
            "#START7777\n# REG_UP  REG_UP_KO-------------------------------  REG_ID    REG_KO----------------------------------  "
            "TM_FC         TM_EF         WRN   LVL   CMD   ED_TM\n"
            "L1050000, 전라남도, L1051200, 순천시, 202611121700, 202611121800, 호우, 경보, 발표, ,=\n"
        )
        raws = kma_warnings.parse_apihub_body(text.encode("euc-kr"))
        self.assertEqual([(r.zone_code, r.type_name, r.level_name) for r in raws], [("L1051200", "호우", "경보")])

    def test_garbage_is_failure(self):
        with self.assertRaises(FetchError):
            kma_warnings.parse_apihub_body("<html>점검 중</html>".encode("utf-8"))

    def test_fallback_order(self):
        def fake_json(*_a, **_kw):
            raise FetchError("특보현황: TimeoutError")

        with mock.patch.object(kma_warnings, "get_json", side_effect=fake_json), mock.patch.object(
            kma_warnings, "get_bytes", return_value=json.dumps(APIHUB_ROWS, ensure_ascii=False).encode("utf-8")
        ) as hub:
            official, status, national = kma_warnings.fetch(CFG.regions, CFG.thresholds, kst(2026, 11, 12, 21), data_go_kr_key="k1", apihub_key="k2")
        self.assertEqual((status["ok"], status["provider"]), (True, "기상청 API허브"))
        self.assertEqual(hub.call_args.args[1]["disp"], 1)
        self.assertTrue(official["mokpo"].available)


# ---------------------------------------------------------------- HTTP


def http_error(code, body=b"", headers=None):
    return urllib.error.HTTPError("https://example.test/x", code, "err", headers or {}, io.BytesIO(body))


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class HttpTest(unittest.TestCase):
    def test_secrets_are_redacted(self):
        url = http.build_url("https://apis.data.go.kr/x", {"serviceKey": "abc+def/==", "nx": 70})
        self.assertIn("serviceKey=abc%2Bdef%2F%3D%3D", url)
        shown = http.redact(url)
        self.assertNotIn("abc", shown)
        self.assertIn("nx=70", shown)
        self.assertNotIn("SECRET", http.redact("https://apihub.kma.go.kr/x?authKey=SECRET&disp=1"))

    def test_service_key_normalisation(self):
        self.assertEqual(http.normalize_service_key("abc%2Bdef%3D%3D"), "abc+def==")  # Encoding 키 → Decoding 키
        self.assertEqual(http.normalize_service_key(" abc+def== "), "abc+def==")
        self.assertIsNone(http.normalize_service_key(""))
        self.assertIsNone(http.normalize_service_key(None))

    def test_retries_then_succeeds(self):
        outcomes = [http_error(503), TimeoutError("timed out"), FakeResponse(b"ok")]
        with mock.patch("urllib.request.urlopen", side_effect=outcomes) as urlopen, mock.patch.object(http, "_sleep") as sleep:
            self.assertEqual(http.get_bytes("https://example.test/x", retries=3, backoff=1), b"ok")
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])  # 간격을 늘려 가며

    def test_gives_up_after_retries_without_leaking_key(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")) as urlopen, mock.patch.object(http, "_sleep"):
            with self.assertRaises(FetchError) as ctx:
                http.get_bytes("https://example.test/x", {"serviceKey": "TOPSECRET"}, retries=2, backoff=1)
        self.assertEqual(urlopen.call_count, 3)
        self.assertNotIn("TOPSECRET", str(ctx.exception))

    def test_client_error_is_not_retried(self):
        body = '{"OpenAPI_ServiceResponse":{"cmmMsgHeader":{"errMsg":"SERVICE_KEY_IS_NOT_REGISTERED_ERROR"}}}'.encode()
        with mock.patch("urllib.request.urlopen", side_effect=http_error(403, body)) as urlopen, mock.patch.object(http, "_sleep"):
            with self.assertRaisesRegex(FetchError, "SERVICE_KEY_IS_NOT_REGISTERED_ERROR") as ctx:
                http.get_bytes("https://example.test/x", retries=3)
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(ctx.exception.status, 403)

    def test_daily_limit_429_is_not_retried(self):
        err = http_error(429, b'{"error":true,"reason":"Daily API request limit exceeded."}', {"Retry-After": "42602"})
        with mock.patch("urllib.request.urlopen", side_effect=err) as urlopen, mock.patch.object(http, "_sleep") as sleep:
            with self.assertRaisesRegex(FetchError, "Daily API request limit"):
                http.get_bytes("https://example.test/x", retries=3)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_short_retry_after_is_honoured(self):
        outcomes = [http_error(429, b"", {"Retry-After": "20"}), FakeResponse(b"ok")]
        with mock.patch("urllib.request.urlopen", side_effect=outcomes), mock.patch.object(http, "_sleep") as sleep:
            self.assertEqual(http.get_bytes("https://example.test/x", retries=3, backoff=1), b"ok")
        self.assertTrue(20 <= sleep.call_args.args[0] <= 24)

    def test_non_json_body(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(b"<html>")):
            with self.assertRaisesRegex(FetchError, "JSON"):
                http.get_json("https://example.test/x", retries=0)


if __name__ == "__main__":
    unittest.main()
