"""실시간 자료(teukbo.live)와 초단기실황·초단기예보 수집(teukbo.sources.kma_ultra)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from teukbo import live
from teukbo.assess import OfficialWarning, RegionOfficial
from teukbo.http import FetchError
from teukbo.sources import kma_ultra, kma_warnings
from teukbo.sources.kma_warnings import RawWarning

from .helpers import config, kst

CFG = config()


def load_settings_live():
    import json

    from teukbo.config import ROOT

    return json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))["live"]


def ok(items):
    return {"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"}, "body": {"items": {"item": items}, "totalCount": len(items)}}}


NODATA = {"response": {"header": {"resultCode": "03", "resultMsg": "NO_DATA"}}}


def ncst_items(base_date="20261113", base_time="0100", **values):
    data = {"PTY": "0", "REH": "71", "RN1": "0", "T1H": "8.4", "UUU": "-1.2", "VEC": "297", "VVV": "0.6", "WSD": "1.4", **values}
    return [{"baseDate": base_date, "baseTime": base_time, "category": k, "nx": 70, "ny": 70, "obsrValue": v} for k, v in data.items()]


def fcst_items(base_date="20261113", base_time="0030", hours=("0100", "0200", "0300", "0400", "0500", "0600"), rain=None):
    rain = rain or {}
    out = []
    for i, hhmm in enumerate(hours):
        values = {"T1H": str(8 - i), "RN1": rain.get(hhmm, "강수없음"), "SKY": "3", "PTY": "1" if hhmm in rain else "0", "WSD": "2", "VEC": "300", "REH": "70", "LGT": "0", "UUU": "1", "VVV": "1", "POP": "70" if hhmm in rain else "10"}
        out += [{"baseDate": base_date, "baseTime": base_time, "category": k, "fcstDate": base_date, "fcstTime": hhmm, "fcstValue": v, "nx": 70, "ny": 70} for k, v in values.items()]
    return out


class BaseTimeTest(unittest.TestCase):
    def test_ncst_is_available_ten_minutes_past_the_hour(self):
        self.assertEqual(kma_ultra.ncst_bases(kst(2026, 11, 13, 1, 10))[0], ("20261113", "0000"))
        self.assertEqual(kma_ultra.ncst_bases(kst(2026, 11, 13, 1, 11))[0], ("20261113", "0100"))
        self.assertEqual(kma_ultra.ncst_bases(kst(2026, 11, 13, 0, 5)), [("20261112", "2300"), ("20261112", "2200")])  # 자정을 넘는 경우

    def test_fcst_is_issued_at_half_past_and_available_at_45(self):
        self.assertEqual(kma_ultra.fcst_bases(kst(2026, 11, 13, 1, 44))[0], ("20261113", "0030"))
        self.assertEqual(kma_ultra.fcst_bases(kst(2026, 11, 13, 1, 46))[0], ("20261113", "0130"))
        self.assertEqual(kma_ultra.fcst_bases(kst(2026, 11, 13, 0, 20)), [("20261112", "2330"), ("20261112", "2230")])


class ParseTest(unittest.TestCase):
    def test_ncst(self):
        self.assertEqual(kma_ultra.parse_ncst(ncst_items(RN1="2.5", PTY="1")), {"temp": 8.4, "rain": 2.5, "wind": 1.4, "wdir": 297, "rh": 71, "pty": 1})

    def test_ncst_missing_values_are_none_not_zero(self):
        obs = kma_ultra.parse_ncst(ncst_items(T1H="-998.9", RN1="-998.9"))
        self.assertIsNone(obs["temp"])
        self.assertIsNone(obs["rain"])
        self.assertEqual(obs["wind"], 1.4)
        self.assertIsNone(kma_ultra.parse_ncst([{"category": "T1H", "obsrValue": "-999"}]))  # 쓸 값이 하나도 없으면 None

    def test_fcst_keeps_official_rain_wording(self):
        rows = kma_ultra.parse_fcst(fcst_items(rain={"0200": "1mm 미만", "0300": "3.2mm", "0400": "30.0~50.0mm", "0500": "50.0mm 이상"}))
        self.assertEqual([r["t"] for r in rows][:2], ["2026-11-13T01:00+09:00", "2026-11-13T02:00+09:00"])
        self.assertEqual([r["rain"] for r in rows], [None, "1mm 미만", "3.2mm", "30.0~50.0mm", "50.0mm 이상", None])
        self.assertEqual([r["rain_mm"] for r in rows], [0.0, 0.5, 3.2, 40.0, 50.0, 0.0])
        self.assertEqual([r["pop"] for r in rows], [10, 70, 70, 70, 70, 10])
        self.assertEqual((rows[0]["temp"], rows[0]["sky"], rows[0]["pty"], rows[0]["wind"], rows[0]["wdir"], rows[0]["rh"], rows[0]["lgt"]), (8.0, 3, 0, 2.0, 300, 70, 0.0))

    def test_fcst_crossing_midnight_is_sorted_by_time(self):
        items = fcst_items(base_date="20261112", base_time="2130", hours=("2200", "2300"))
        nxt = fcst_items(base_date="20261112", base_time="2130", hours=("0000",))
        for it in nxt:
            it["fcstDate"] = "20261113"
        rows = kma_ultra.parse_fcst(nxt + items)
        self.assertEqual([r["t"][:16] for r in rows], ["2026-11-12T22:00", "2026-11-12T23:00", "2026-11-13T00:00"])


class FetchTest(unittest.TestCase):
    def test_ncst_fetches_each_grid_once_and_shares_it(self):
        calls = []

        def fake(url, params, **_kw):
            calls.append((url.rsplit("/", 1)[1], params["base_date"], params["base_time"], params["nx"], params["ny"], params["serviceKey"]))
            return ok(ncst_items(T1H=str(params["nx"] / 10)))

        with mock.patch.object(kma_ultra, "get_json", side_effect=fake):
            data, status = kma_ultra.fetch_ncst(CFG.regions, kst(2026, 11, 13, 1, 20), data_go_kr_key="k")
        grids = {(r.nx, r.ny) for r in CFG.regions}
        self.assertEqual(len(calls), len(grids))
        self.assertEqual({c[0] for c in calls}, {"getUltraSrtNcst"})
        self.assertEqual({c[1:3] for c in calls}, {("20261113", "0100")})
        self.assertEqual(set(data), {r.id for r in CFG.regions})
        self.assertTrue(status["ok"])
        self.assertEqual(status["base"], "2026-11-13T01:00+09:00")
        suncheon = next(r for r in CFG.regions if r.id == "suncheon")
        self.assertEqual(data["suncheon"]["temp"], suncheon.nx / 10)

    def test_falls_back_to_previous_base_when_not_published_yet(self):
        def fake(url, params, **_kw):
            return NODATA if params["base_time"] == "0100" else ok(ncst_items(base_time="0000"))

        with mock.patch.object(kma_ultra, "get_json", side_effect=fake):
            data, status = kma_ultra.fetch_ncst(CFG.regions[:2], kst(2026, 11, 13, 1, 12), data_go_kr_key="k")
        self.assertEqual(status["base"], "2026-11-13T00:00+09:00")
        self.assertEqual(len(data), 2)

    def test_one_failed_grid_does_not_lose_the_rest(self):
        regions = CFG.regions[:4]
        bad = (regions[2].nx, regions[2].ny)

        def fake(url, params, **_kw):
            if (params["nx"], params["ny"]) == bad:
                raise FetchError("시간 초과")
            return ok(fcst_items())

        with mock.patch.object(kma_ultra, "get_json", side_effect=fake):
            data, status = kma_ultra.fetch_fcst(regions, kst(2026, 11, 13, 0, 50), data_go_kr_key="k")
        self.assertNotIn(regions[2].id, data)
        self.assertEqual(status["failed"], 1)
        self.assertEqual(len(data[regions[0].id]), 6)

    def test_without_keys_is_skipped_quietly(self):
        data, status = kma_ultra.fetch_ncst(CFG.regions, kst(2026, 11, 13, 1, 20), data_go_kr_key=None)
        self.assertEqual((data, status["configured"], status["ok"]), ({}, False, False))

    def test_total_failure_reports_error(self):
        with mock.patch.object(kma_ultra, "get_json", side_effect=FetchError("연결 실패")):
            data, status = kma_ultra.fetch_ncst(CFG.regions, kst(2026, 11, 13, 1, 20), data_go_kr_key="k")
        self.assertEqual(data, {})
        self.assertFalse(status["ok"])
        self.assertIn("연결 실패", status["error"])


class LiveCollectTest(unittest.TestCase):
    """live.collect: 받을 때·건너뛸 때·실패했을 때의 동작과 결과 형식."""

    def setUp(self):
        self.calls = {"warnings": 0, "ncst": 0, "fcst": 0}
        self.fail = set()
        self.active = True

        def warnings(regions, thresholds, now, **_kw):
            self.calls["warnings"] += 1
            if "warnings" in self.fail:
                return {r.id: RegionOfficial(available=False) for r in regions}, {"ok": False, "configured": True, "provider": None, "error": "특보 조회 실패"}, []
            official = {r.id: RegionOfficial(available=True) for r in regions}
            raws = []
            if self.active:
                start = kst(2026, 11, 13, 0, 30)
                official["suncheon"].active.append(OfficialWarning("active", "heavy_rain", "호우", "warning", "경보", "L1051200", "순천", start, None))
                official["boseong"].preliminary.append(OfficialWarning("preliminary", "strong_wind", "강풍", None, "예비", "L1050900", "보성", None, None, "13일 오후"))
                raws = [RawWarning("active", "호우", "경보", "순천", None, "L1051200", start), RawWarning("preliminary", "강풍", "", "보성", None, "L1050900", None, None, "13일 오후")]
            return official, {"ok": True, "configured": True, "provider": "공공데이터포털", "error": None}, raws

        def ncst(regions, now, **_kw):
            self.calls["ncst"] += 1
            if "ncst" in self.fail:
                return {}, {"ok": False, "configured": True, "provider": None, "error": "실황 실패"}
            base = kma_ultra.base_time(kma_ultra.ncst_bases(now)[0])
            return {r.id: {"temp": 8.0 + self.calls["ncst"], "rain": 0.0, "wind": 1.5, "wdir": 300, "rh": 70, "pty": 0} for r in regions if r.id != "jindo"}, {
                "ok": True, "configured": True, "provider": "공공데이터포털", "base": live.iso(base), "regions": len(regions) - 1, "failed": 1, "error": None}

        def fcst(regions, now, **_kw):
            self.calls["fcst"] += 1
            if "fcst" in self.fail:
                return {}, {"ok": False, "configured": True, "provider": None, "error": "예보 실패"}
            base = kma_ultra.base_time(kma_ultra.fcst_bases(now)[0])
            rows = [{"t": live.iso(base + live.timedelta(minutes=30 + 60 * k)), "temp": 7.0, "rain": None, "rain_mm": 0.0, "sky": 1, "pty": 0, "wind": 2.0, "wdir": 0, "rh": 60, "lgt": 0.0} for k in range(6)]
            return {r.id: rows for r in regions}, {"ok": True, "configured": True, "provider": "공공데이터포털", "base": live.iso(base), "regions": len(regions), "failed": 0, "error": None}

        for target, name, fake in ((kma_warnings, "fetch", warnings), (kma_ultra, "fetch_ncst", ncst), (kma_ultra, "fetch_fcst", fcst)):
            patcher = mock.patch.object(target, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.env = {"DATA_GO_KR_SERVICE_KEY": "k"}
        # 지금 날씨·6시간 예보는 설정에서 꺼 두었다(전국으로 넓히며). 이 테스트들은 켠 상태의 동작을 본다.
        switch = mock.patch.dict(CFG.settings.raw["live"], {"obs": True, "fcst": True})
        switch.start()
        self.addCleanup(switch.stop)

    def test_weather_pulls_can_be_switched_off(self):
        """설정에서 끄면 실황·초단기예보는 아예 받지 않고(호출 0회), 특보는 그대로 받는다."""
        with mock.patch.dict(CFG.settings.raw["live"], {"obs": False, "fcst": False}):
            doc = self.run_at(kst(2026, 11, 13, 1, 20))
        self.assertEqual((self.calls["warnings"], self.calls["ncst"], self.calls["fcst"]), (1, 0, 0))
        self.assertTrue(doc["warnings"]["available"])
        self.assertEqual((doc["obs"]["available"], doc["obs"].get("disabled"), doc["fcst"]["available"]), (False, True, False))
        self.assertNotIn("now", doc["regions"]["suncheon"])
        self.assertIn("official", doc["regions"]["suncheon"])
        self.assertIs(load_settings_live()["obs"], False)  # 저장소 설정은 꺼 둔 상태

    def run_at(self, now, prev=None):
        return live.collect(CFG, now, self.env, prev)

    def test_first_run_structure(self):
        doc = self.run_at(kst(2026, 11, 13, 1, 20))
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["generated_at"], "2026-11-13T01:20+09:00")
        w = doc["warnings"]
        self.assertEqual((w["available"], w["checked_at"], w["error"]), (True, "2026-11-13T01:20+09:00", None))
        self.assertEqual([(a["code"], a["name"]) for a in w["active"]], [("L1051200", "호우경보")])
        self.assertEqual([(p["code"], p["name"], p["expected"]) for p in w["preliminary"]], [("L1050900", "강풍 예비특보", "13일 오후")])
        self.assertEqual((doc["obs"]["available"], doc["obs"]["base"], doc["obs"]["checked_at"]), (True, "2026-11-13T01:00+09:00", "2026-11-13T01:20+09:00"))
        self.assertEqual(doc["fcst"]["base"], "2026-11-13T00:30+09:00")
        self.assertEqual(set(doc["regions"]), {r.id for r in CFG.regions})
        suncheon = doc["regions"]["suncheon"]
        self.assertEqual(suncheon["official"]["active"][0]["name"], "호우경보")
        # 호우경보 발효 → 호우주의보·호우경보 모두 '발효 중'(본 수집과 같은 규칙)
        self.assertEqual(suncheon["hz"], {"heavy_rain_advisory": ["active", "warning", "2026-11-13T00:30+09:00"], "heavy_rain_warning": ["active", "warning", "2026-11-13T00:30+09:00"]})
        # 단계 미상의 예비특보는 가장 낮은 단계에만
        self.assertEqual(doc["regions"]["boseong"]["hz"], {"strong_wind_advisory": ["preliminary", None, None]})
        self.assertEqual(suncheon["now"]["temp"], 9.0)
        self.assertEqual(len(suncheon["fcst"]), 6)
        self.assertNotIn("now", doc["regions"]["jindo"])  # 받지 못한 곳은 값을 지어내지 않는다
        self.assertEqual(doc["regions"]["yeosu"]["hz"], {})
        json.dumps(doc)  # JSON 으로 쓸 수 있어야 한다

    def test_warnings_every_run_but_weather_only_when_due(self):
        first = self.run_at(kst(2026, 11, 13, 1, 20))
        second = self.run_at(kst(2026, 11, 13, 1, 25), first)
        self.assertEqual(self.calls, {"warnings": 2, "ncst": 1, "fcst": 1})
        self.assertEqual(second["warnings"]["checked_at"], "2026-11-13T01:25+09:00")
        self.assertEqual(second["obs"]["checked_at"], "2026-11-13T01:20+09:00")  # 다시 받지 않았으면 확인 시각도 그대로
        self.assertEqual(second["regions"]["suncheon"]["now"], first["regions"]["suncheon"]["now"])
        third = self.run_at(kst(2026, 11, 13, 1, 30), second)  # 실황 10분 경과
        self.assertEqual(self.calls, {"warnings": 3, "ncst": 2, "fcst": 1})
        self.assertEqual(third["obs"]["checked_at"], "2026-11-13T01:30+09:00")
        self.assertEqual(third["regions"]["suncheon"]["now"]["temp"], 10.0)
        fourth = self.run_at(kst(2026, 11, 13, 1, 46), third)  # 새 초단기예보(01:30 발표)가 나옴
        self.assertEqual(self.calls["fcst"], 2)
        self.assertEqual(fourth["fcst"]["base"], "2026-11-13T01:30+09:00")

    def test_new_observation_hour_is_fetched_immediately(self):
        first = self.run_at(kst(2026, 11, 13, 1, 8))  # 아직 00시 기준
        self.assertEqual(first["obs"]["base"], "2026-11-13T00:00+09:00")
        second = self.run_at(kst(2026, 11, 13, 1, 12), first)  # 4분밖에 안 지났지만 01시 실황이 나왔다
        self.assertEqual(self.calls["ncst"], 2)
        self.assertEqual(second["obs"]["base"], "2026-11-13T01:00+09:00")

    def test_one_missing_region_reuses_same_hour_value(self):
        first = self.run_at(kst(2026, 11, 13, 1, 20))
        first["regions"]["jindo"]["now"] = {"temp": 5.5, "rain": 0.0, "wind": 3.0, "wdir": 10, "rh": 80, "pty": 0}
        second = self.run_at(kst(2026, 11, 13, 1, 31), first)
        self.assertEqual(second["regions"]["jindo"]["now"]["temp"], 5.5)  # 같은 기준시각(01시)의 직전 값
        third = self.run_at(kst(2026, 11, 13, 2, 12), second)
        self.assertNotIn("now", third["regions"]["jindo"])  # 기준시각이 바뀌면 옛 값을 쓰지 않는다

    def test_failed_fetch_keeps_previous_values_with_old_check_time(self):
        first = self.run_at(kst(2026, 11, 13, 1, 20))
        self.fail = {"warnings", "ncst", "fcst"}
        second = self.run_at(kst(2026, 11, 13, 1, 45), first)
        self.assertEqual(second["generated_at"], "2026-11-13T01:45+09:00")
        for name, message in (("warnings", "특보 조회 실패"), ("obs", "실황 실패"), ("fcst", "예보 실패")):
            self.assertTrue(second[name]["available"], name)
            self.assertEqual(second[name]["checked_at"], "2026-11-13T01:20+09:00", name)  # 화면이 '25분 전 확인'으로 보여 줄 수 있다
            self.assertEqual(second[name]["error"], message, name)
        self.assertEqual(second["warnings"]["active"], first["warnings"]["active"])
        self.assertEqual(second["regions"]["suncheon"]["hz"], first["regions"]["suncheon"]["hz"])
        self.assertEqual(second["regions"]["suncheon"]["now"], first["regions"]["suncheon"]["now"])

    def test_too_old_values_are_dropped_instead_of_carried(self):
        first = self.run_at(kst(2026, 11, 13, 1, 20))
        self.fail = {"warnings", "ncst", "fcst"}
        late = self.run_at(kst(2026, 11, 13, 4, 30), first)  # 3시간 넘게 실패
        for name in ("warnings", "obs", "fcst"):
            self.assertFalse(late[name]["available"], name)
        self.assertEqual(late["regions"]["suncheon"], {})

    def test_cleared_warning_disappears(self):
        first = self.run_at(kst(2026, 11, 13, 1, 20))
        self.active = False
        second = self.run_at(kst(2026, 11, 13, 1, 25), first)
        self.assertEqual(second["warnings"]["active"], [])
        self.assertEqual(second["regions"]["suncheon"]["official"], {"available": True, "active": [], "preliminary": []})
        self.assertEqual(second["regions"]["suncheon"]["hz"], {})

    def test_past_forecast_hours_are_trimmed(self):
        first = self.run_at(kst(2026, 11, 13, 1, 20))
        self.fail = {"fcst"}
        second = self.run_at(kst(2026, 11, 13, 3, 10), first)
        times = [row["t"][11:16] for row in second["regions"]["suncheon"]["fcst"]]
        self.assertEqual(times, ["03:00", "04:00", "05:00", "06:00"])

    def test_without_keys(self):
        doc = live.collect(CFG, kst(2026, 11, 13, 1, 20), {}, None)
        self.assertEqual(self.calls["ncst"], 0)
        for name in ("obs", "fcst"):
            self.assertEqual((doc[name]["available"], doc[name]["configured"]), (False, False))

    def test_unknown_previous_file_is_ignored(self):
        doc = self.run_at(kst(2026, 11, 13, 1, 20), {"schema": 99, "obs": {"available": True, "checked_at": "2026-11-13T01:19+09:00"}})
        self.assertEqual(self.calls["ncst"], 1)
        self.assertTrue(doc["obs"]["available"])

    def test_command_line_writes_file_and_summary(self):
        out = Path(tempfile.mkdtemp()) / "live" / "live.json"
        summary = out.parent.parent / "summary.md"
        with mock.patch.dict("os.environ", {"DATA_GO_KR_SERVICE_KEY": "k", "GITHUB_STEP_SUMMARY": str(summary)}), mock.patch.object(live, "load_config", return_value=CFG):  # 실황·예보를 켠 설정으로
            self.assertEqual(live.main(["--out", str(out), "--prev", str(out), "--now", "2026-11-13T01:20+09:00"]), 0)  # 직전 파일이 없어도 된다
            self.assertEqual(live.main(["--out", str(out), "--prev", str(out), "--now", "2026-11-13T01:25+09:00"]), 0)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(doc["generated_at"], "2026-11-13T01:25+09:00")
        self.assertEqual(self.calls, {"warnings": 2, "ncst": 1, "fcst": 1})
        text = summary.read_text(encoding="utf-8")
        self.assertIn("특보 01:25 확인, 발효 1·예비 1건", text)
        self.assertIn("실황 01:20 확인(기준 01:00)", text)
