"""과거 검증(백테스트): 지난 예보를 그 시점에 볼 수 있었던 것만으로 이어 붙여 다시 계산하고, 실제 특보와 맞춰 본다."""

import unittest
from datetime import datetime, timedelta
from unittest import mock

from teukbo import backtest, rail
from teukbo.config import Config
from teukbo.http import FetchError
from teukbo.timeutil import KST

from .helpers import config

FULL = config()
START = datetime(2026, 10, 1, 0, tzinfo=KST)
HOURS = 24 * 16
CALM = {"precipitation": 0.0, "snowfall": 0.0, "temperature_2m": 12.0, "relative_humidity_2m": 60.0, "wind_speed_10m": 3.0, "wind_gusts_10m": 6.0}


def at(day: int, hour: int = 0) -> datetime:
    return START + timedelta(days=day, hours=hour)


def payload(regions, rain=None, model_suffix=None):
    """rain: {(지역 id, 선행): {날 번호: 시간당 mm}} — 그날 06~10시에 비. 나머지는 잔잔한 날씨."""
    rain = rain or {}
    times = [(START + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(HOURS)]
    out = []
    for region in regions:
        hourly = {"time": times}
        for name, calm in CALM.items():
            for lead in backtest.LEADS:
                column = [calm] * HOURS
                if name == "precipitation":
                    for day, amount in rain.get((region.id, lead), {}).items():
                        for hour in range(6, 11):
                            column[day * 24 + hour] = amount
                hourly[f"{name}_previous_day{lead}" + (f"_{model_suffix}" if model_suffix else "")] = column
        out.append({"hourly": hourly})
    return out


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.regions = [r for r in FULL.regions if r.id in ("suncheon", "mokpo")]
        self.model = next(m for m in FULL.settings.models if m.source == "open_meteo")

    def test_request_asks_for_one_and_two_day_old_forecasts(self):
        params = backtest.build_params(self.regions, self.model.id, 60)
        names = params["hourly"].split(",")
        self.assertIn("precipitation_previous_day1", names)
        self.assertIn("wind_gusts_10m_previous_day2", names)
        self.assertEqual(len(names), len(backtest.HOURLY_VARS) * 2)
        self.assertEqual((params["past_days"], params["models"], params["wind_speed_unit"], params["timezone"]), (60, self.model.id, "ms", "Asia/Seoul"))
        self.assertEqual(backtest.ENDPOINT, "https://previous-runs-api.open-meteo.com/v1/forecast")

    def test_parse_keeps_leads_apart(self):
        got = backtest.parse_archive(payload(self.regions, {("suncheon", 1): {3: 20.0}, ("suncheon", 2): {3: 5.0}}), self.regions, self.model.id)
        day1, day2 = got["suncheon"][1], got["suncheon"][2]
        index = 3 * 24 + 7
        self.assertEqual((day1.values["precip"][index], day2.values["precip"][index]), (20.0, 5.0))
        self.assertEqual(day1.times[0], START)
        self.assertEqual(got["mokpo"][1].values["precip"][index], 0.0)
        # 모델 이름이 붙은 열 이름으로 와도 읽는다. 한 지점만 오면 객체 하나로 온다
        suffixed = backtest.parse_archive(payload(self.regions[:1], model_suffix=self.model.id)[0], self.regions[:1], self.model.id)
        self.assertIn("wind", suffixed["suncheon"][2].values)
        with self.assertRaises(FetchError):
            backtest.parse_archive({"error": True, "reason": "Daily API request limit exceeded."}, self.regions, self.model.id)
        with self.assertRaises(FetchError):
            backtest.parse_archive(payload(self.regions[:1]), self.regions, self.model.id)

    def test_fetch_in_paced_chunks_and_keep_partial(self):
        regions = list(FULL.regions[:5])
        calls, pauses = [], []

        def fake(_url, params, **_kw):
            calls.append(params["latitude"].count(",") + 1)
            if len(calls) == 3:
                raise FetchError("HTTP 429", status=429)
            return payload(regions[: calls[-1]])

        with mock.patch.object(backtest, "get_json", side_effect=fake), mock.patch.object(backtest, "_pause", side_effect=pauses.append):
            got = backtest.fetch_archive(regions, self.model, 60, chunk=2, pause=30)
        self.assertEqual((calls, pauses), ([2, 2, 1], [30, 30]))
        self.assertEqual(len(got), 4)  # 받은 묶음은 쓴다

    def test_stitch_never_uses_a_forecast_made_after_the_replay_time(self):
        """재현 시점 T 에서: T+24시간까지는 하루 전에 예측한 값, 그 뒤는 이틀 전에 예측한 값 — 모두 T 이전에 나온 예보다."""
        region = self.regions[0]
        leads = backtest.parse_archive(payload([region], {(region.id, 1): {5: 30.0, 6: 30.0}, (region.id, 2): {5: 7.0, 6: 7.0}}), [region], self.model.id)[region.id]
        issue = at(5, 0)  # 5일 00시에 재현: 5일 06~10시(6~10시간 뒤)는 하루 전 예측, 6일 06~10시(30~34시간 뒤)는 이틀 전 예측
        series = backtest.stitch(leads, issue, past_hours=36, horizon_hours=48)
        value = dict(zip(series.times, series.values["precip"]))
        self.assertEqual(value[at(5, 7)], 30.0)
        self.assertEqual(value[at(6, 7)], 7.0)
        self.assertEqual((series.times[0], series.times[-1]), (issue - timedelta(hours=36), issue + timedelta(hours=48)))
        # 한쪽 선행만 있으면 그것으로, 둘 다 없으면 None
        self.assertEqual(dict(zip(*(lambda s: (s.times, s.values["precip"]))(backtest.stitch({2: leads[2]}, issue, 36, 48))))[at(5, 7)], 7.0)
        self.assertIsNone(backtest.stitch({}, issue, 36, 48))


class TruthTest(unittest.TestCase):
    def test_warnings_reach_regions_at_any_level(self):
        """특보는 말단 구역·시군·도 어느 단위로도 내려진다 — 그 아래 지역 모두의 특보로 본다. 계산하지 않는 종류(풍랑·건조)는 뺀다."""
        intervals = [
            rail.Interval("L1051200", "호우", "주의보", at(1), at(2)),  # 순천(말단)
            rail.Interval("L1050000", "강풍", "경보", at(3), None),  # 전라남도 전체
            rail.Interval("L1013700", "한파", "주의보", at(4), at(5)),  # 파주(시 전체 → 세 구역 모두)
            rail.Interval("L1013720", "호우", "주의보", at(6), at(7)),  # 파주서북부만
            rail.Interval("S1312010", "풍랑", "주의보", at(1), at(2)),
            rail.Interval("L1051200", "건조", "주의보", at(1), at(2)),
        ]
        truth = backtest.region_intervals(FULL, intervals)
        kinds = lambda rid: sorted((t, level) for t, level, *_ in truth[rid])  # noqa: E731
        self.assertEqual(kinds("suncheon"), [("heavy_rain", "advisory"), ("strong_wind", "warning")])
        self.assertEqual(kinds("mokpo"), [("strong_wind", "warning")])
        self.assertEqual(kinds("L1013710"), [("cold_wave", "advisory")])
        self.assertEqual(kinds("L1013720"), [("cold_wave", "advisory"), ("heavy_rain", "advisory")])
        self.assertEqual(kinds("L1100100"), [])
        official = backtest.official_at(FULL, truth["suncheon"], at(1, 12))
        self.assertEqual([(w.type, w.level) for w in official.active], [("heavy_rain", "advisory")])
        self.assertEqual(backtest.official_at(FULL, truth["suncheon"], at(2, 0)).active, [])  # 해제 시각부터는 없음
        self.assertEqual(len(backtest.official_at(FULL, truth["suncheon"], at(9)).active), 1)  # 아직 해제되지 않은 강풍경보

    def test_one_event_per_new_warning_in_a_region(self):
        """실제 특보 한 건 = 그 지역에 새로 발효되거나 단계가 올라간 것. 겹쳐 내려진 발표·이어진 발표·단계가 내려간 것은 새 건이 아니다."""
        intervals = [
            rail.Interval("L1051200", "강풍", "주의보", at(1, 10), at(1, 13)),  # 순천: 3시간짜리 짧은 특보(재현 시점 09시·15시 사이)
            rail.Interval("L1050000", "강풍", "주의보", at(1, 11), at(1, 18)),  # 전남 전체에 겹쳐 내려짐 → 순천에는 새 건이 아니다(목포에는 새 건)
            rail.Interval("L1051200", "호우", "주의보", at(3, 6), at(3, 12)),
            rail.Interval("L1051200", "호우", "경보", at(3, 12), at(3, 18)),  # 단계가 올라감 → 새 건
            rail.Interval("L1051200", "호우", "주의보", at(3, 18), at(4, 0)),  # 단계가 내려감 → 새 건이 아니다
            rail.Interval("L1051200", "호우", "주의보", at(6, 6), at(6, 9)),  # 해제된 뒤 다시 → 새 건
            rail.Interval("L1051200", "태풍", "주의보", at(7, 6), at(7, 9)),  # 계산하지 않는 종류(공식 발표만 본다)
        ]
        cfg = Config(regions=tuple(r for r in FULL.regions if r.id in ("suncheon", "mokpo")), thresholds=FULL.thresholds, settings=FULL.settings)
        events = backtest.truth_events(cfg, backtest.region_intervals(cfg, intervals))
        self.assertEqual([(e["region"], e["hazard"], e["time"]) for e in events], [
            ("suncheon", "strong_wind_advisory", at(1, 10)),
            ("mokpo", "strong_wind_advisory", at(1, 11)),
            ("suncheon", "heavy_rain_advisory", at(3, 6)),
            ("suncheon", "heavy_rain_warning", at(3, 12)),
            ("suncheon", "heavy_rain_advisory", at(6, 6)),
        ])


class ReplayTest(unittest.TestCase):
    """가짜 과거 예보와 가짜 특보 기록으로 처음부터 끝까지."""

    @classmethod
    def setUpClass(cls):
        regions = [r for r in FULL.regions if r.id in ("suncheon", "mokpo", "L1100100")]
        cls.cfg = Config(regions=tuple(regions), thresholds=FULL.thresholds, settings=FULL.settings)
        models = [m for m in FULL.settings.models if m.source == "open_meteo"]
        # 순천: 8일·12일에 큰비 예보(둘 다 실제 호우주의보). 목포: 8일에 큰비 예보였지만 특보 없음(헛짚음). 서울: 예보는 잔잔했는데 10일에 한파주의보(놓침)
        rain = {("suncheon", 1): {8: 26.0, 12: 26.0}, ("suncheon", 2): {8: 24.0, 12: 24.0}, ("mokpo", 1): {8: 26.0}, ("mokpo", 2): {8: 24.0}}
        cls.archive = {m.id: backtest.parse_archive(payload(regions, rain), regions, m.id) for m in models}
        intervals = [rail.Interval("L1051200", "호우", "주의보", at(8, 7), at(8, 20)), rail.Interval("L1051200", "호우", "주의보", at(12, 7), at(12, 20)),
                     rail.Interval("L1100000", "한파", "주의보", at(10, 21), at(11, 9))]
        cls.truth = backtest.region_intervals(cls.cfg, intervals)
        cls.times = backtest.replay_times(at(4), at(13, 23))
        cls.runs, cls.table, cls.days = backtest.replay(cls.cfg, cls.archive, cls.truth, cls.times, workers=1)
        cls.doc = backtest.build_document(cls.cfg, cls.runs, cls.table, cls.days, cls.truth, list(cls.archive), len(regions), at(16))

    def test_replay_times_are_six_hourly(self):
        self.assertEqual([t.hour for t in self.times[:5]], [3, 9, 15, 21, 3])
        self.assertEqual(len(self.times), 40)
        self.assertEqual(self.doc["period"], {"from": "2026-10-05T03:00+09:00", "to": "2026-10-14T21:00+09:00", "replays": 40, "step_hours": 6})

    def test_events_and_capture(self):
        doc = self.doc
        self.assertEqual((doc["events"], doc["event_days"]), (3, 3))
        self.assertEqual(doc["capture"]["24"], {"events": 3, "captured": 2, "rate": 0.667})  # 순천 두 번은 하루 전에 잡고, 서울 한파는 놓쳤다
        rain, cold = doc["by_type"]["heavy_rain"], doc["by_type"]["cold_wave"]
        self.assertEqual((rain["events"], rain["capture"]["24"]["captured"]), (2, 2))
        self.assertEqual((cold["events"], cold["capture"]["24"]["captured"], cold["false_alarm"]["caution_or_higher"]["alerts"]), (1, 0, 0))

    def test_short_warning_between_replays_is_still_an_event(self):
        """재현 시점(09시·15시) 사이에 났다가 끝난 3시간짜리 특보도 실제 특보 한 건으로 세고, 하루 전 재현에서 잡았는지 본다."""
        intervals = [rail.Interval("L1051200", "호우", "주의보", at(8, 10), at(8, 13)), rail.Interval("L1100000", "한파", "주의보", at(10, 22), at(11, 1))]
        truth = backtest.region_intervals(self.cfg, intervals)
        runs, table, days = backtest.replay(self.cfg, self.archive, truth, self.times, workers=1)
        self.assertFalse(any(rec.get("a") for run in runs for rec in run["regions"].values()))  # 어느 재현 시점에도 발효 중이 아니었다
        doc = backtest.build_document(self.cfg, runs, table, days, truth, list(self.archive), 3, at(16))
        self.assertEqual((doc["events"], doc["event_days"]), (2, 2))
        self.assertEqual(doc["capture"]["24"], {"events": 2, "captured": 1, "rate": 0.5})  # 순천 호우는 하루 전에 잡고, 서울 한파는 놓쳤다
        self.assertEqual(doc["by_type"]["heavy_rain"]["capture"]["12"], {"events": 1, "captured": 1, "rate": 1.0})

    def test_events_at_the_edges_of_the_period_are_left_out(self):
        """재현 구간의 처음(하루 전 재현이 없다)·끝(12시간 전 재현이 없다)에 걸친 특보는 '건수'에서 뺀다 — 건수와 평가한 건수가 어긋나지 않게."""
        intervals = [rail.Interval("L1051200", "호우", "주의보", at(4, 20), at(5, 2)), rail.Interval("L1051200", "호우", "주의보", at(8, 7), at(8, 20)),
                     rail.Interval("L1051200", "호우", "주의보", at(14, 12), at(14, 20))]
        truth = backtest.region_intervals(self.cfg, intervals)
        doc = backtest.build_document(self.cfg, self.runs, self.table, self.days, truth, list(self.archive), 3, at(16))
        self.assertEqual(doc["events"], 1)
        self.assertEqual(doc["capture"]["24"]["events"], 1)
        self.assertEqual(doc["capture"]["12"]["events"], 1)

    def test_right_and_wrong_alerts_come_from_the_calibration_table(self):
        """'주의' 이상·'높음'이 맞은 횟수는 보정표(48시간 안에 실제 특보가 시작됐는지)와 같은 수여야 한다."""
        doc = self.doc
        rows = doc["calibration"]
        alerts, high = doc["false_alarm"]["caution_or_higher"], doc["false_alarm"]["high"]
        self.assertEqual(alerts["alerts"], rows[2]["cases"] + rows[3]["cases"])
        self.assertEqual(alerts["alerts"] - alerts["false"], rows[2]["hits"] + rows[3]["hits"])
        self.assertEqual((high["alerts"], high["alerts"] - high["false"]), (rows[3]["cases"], rows[3]["hits"]))
        self.assertEqual((backtest.BUCKETS[2][0], backtest.BUCKETS[3][0]), (FULL.settings.raw["grades"]["caution_min"], FULL.settings.raw["grades"]["high_min"]))
        rain = doc["by_type"]["heavy_rain"]
        self.assertEqual(rain["false_alarm"]["high"]["alerts"], rain["calibration"][3]["cases"])
        self.assertEqual((rain["name"], rain["event_days"]), ("호우", 2))
        self.assertIn("폭염", doc["types_without_events"])  # 계산은 했지만 이 기간에 실제 특보가 없던 종류
        self.assertNotIn("호우", doc["types_without_events"])

    def test_false_alarms_are_counted(self):
        alerts = self.doc["by_type"]["heavy_rain"]["false_alarm"]["caution_or_higher"]
        self.assertGreater(alerts["alerts"], 0)
        self.assertGreater(alerts["false"], 0)  # 목포: 큰비가 예보됐지만 특보는 없었다
        self.assertLess(alerts["false"], alerts["alerts"])  # 순천: 맞았다

    def test_calibration_table(self):
        rows = {row["bucket"]: row for row in self.doc["calibration"]}
        self.assertEqual([row["bucket"] for row in self.doc["calibration"]], ["0%", "1~29% (낮음)", "30~59% (주의)", "60~100% (높음)"])
        zero, high = rows["0%"], rows["60~100% (높음)"]
        self.assertGreater(zero["cases"], 500)
        self.assertLess(zero["rate"], 0.02)  # 가능성 0%였는데 특보가 난 경우: 서울 한파뿐
        self.assertGreater(high["cases"], 0)
        self.assertTrue(0.3 < high["rate"] < 0.9, high)  # 순천은 맞고 목포는 틀렸다
        self.assertEqual(high["event_days"], 2)
        self.assertEqual(sum(row["cases"] for row in self.doc["calibration"]), sum(n for cells in self.table.values() for n, _ in cells))
        # 이미 발효 중인 특보는 '예측'으로 세지 않는다: 8일 09시 재현의 순천 호우주의보는 표본에서 빠진다
        run = next(r for r in self.runs if r["t"] == "2026-10-09T09:00+09:00")
        self.assertEqual(run["regions"]["suncheon"]["a"], [{"k": "heavy_rain", "l": "advisory", "n": "호우주의보", "f": "2026-10-09T07:00+09:00"}])

    def test_document_says_how_it_was_done(self):
        self.assertEqual(self.doc["models"], [m.id for m in FULL.settings.models if m.source == "open_meteo"])
        self.assertTrue(any("훔쳐보지" in note for note in self.doc["notes"]))
        self.assertTrue(any("단기예보" in note for note in self.doc["notes"]))
        text = "\n".join(backtest.summary_lines(self.doc))
        self.assertIn("24시간 전 사전 포착률: 2/3", text)

    def test_parallel_replay_gives_the_same_result(self):
        runs, table, _ = backtest.replay(self.cfg, self.archive, self.truth, self.times, workers=2)
        self.assertEqual(table, self.table)
        self.assertEqual([r["t"] for r in runs], [r["t"] for r in self.runs])


if __name__ == "__main__":
    unittest.main()
