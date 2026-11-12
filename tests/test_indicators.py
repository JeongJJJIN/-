import unittest
from datetime import timedelta

from teukbo.config import Indicator
from teukbo.indicators import HOUR, ModelSeries, add_apparent_temp, in_horizon, indicator_points, kma_summer_apparent_temp

from .helpers import burst, config, const, kst, series

IND = config().thresholds.indicators


def points(key, ms, now):
    return indicator_points(IND[key], IND, ms, now)


class RollingSumTest(unittest.TestCase):
    def test_three_hour_sum(self):
        now = kst(2026, 7, 1, 9)
        ms = series("m", now, past=0, future=10, precip=burst(20.0, 2, 3))  # +2,+3,+4시에 20mm씩
        by_lead = {int((p.time - now) / HOUR): p.value for p in points("rain_3h", ms, now)}
        self.assertNotIn(0, by_lead)  # 앞의 2시간 자료가 없으면 만들지 않는다
        self.assertNotIn(1, by_lead)
        self.assertEqual(by_lead[2], 20.0)
        self.assertEqual(by_lead[3], 40.0)
        self.assertEqual(by_lead[4], 60.0)
        self.assertEqual(by_lead[5], 40.0)
        self.assertEqual(by_lead[7], 0.0)

    def test_window_bounds(self):
        now = kst(2026, 7, 1, 9)
        ms = series("m", now, past=0, future=10, precip=const(1.0))
        p = points("rain_3h", ms, now)[0]
        self.assertEqual(p.end, p.time)
        self.assertEqual(p.start, p.time - 3 * HOUR)

    def test_gap_breaks_window(self):
        now = kst(2026, 7, 1, 9)
        ms = series("m", now, past=0, future=10, precip=lambda _t, lead: None if lead == 5 else 10.0)
        leads = {int((p.time - now) / HOUR) for p in points("rain_3h", ms, now)}
        self.assertEqual(leads, {2, 3, 4, 8, 9, 10})  # 결측(+5)이 낀 5,6,7시 창은 제외

    def test_twelve_hour_sum(self):
        now = kst(2026, 7, 1, 9)
        ms = series("m", now, past=12, future=24, precip=const(10.0))
        self.assertTrue(all(p.value == 120.0 for p in points("rain_12h", ms, now)))

    def test_missing_variable_gives_no_points(self):
        now = kst(2026, 7, 1, 9)
        ms = series("m", now, past=0, future=10, temp=const(20.0))
        self.assertEqual(points("rain_3h", ms, now), [])
        self.assertEqual(points("gust", ms, now), [])


class PeriodExtremeTest(unittest.TestCase):
    def test_daily_max(self):
        now = kst(2026, 7, 20, 8)
        ms = series("m", now, apparent=lambda t, _l: 34.0 if t.hour == 15 else 25.0)
        by_day = {p.slot: p for p in points("apparent_max", ms, now)}
        self.assertEqual(by_day["2026-07-20"].value, 34.0)
        self.assertEqual(by_day["2026-07-20"].time, kst(2026, 7, 20, 15))
        self.assertEqual(by_day["2026-07-20"].start, kst(2026, 7, 20, 0))
        self.assertEqual(by_day["2026-07-20"].end, kst(2026, 7, 21, 0))

    def test_night_window_crosses_midnight(self):
        now = kst(2026, 7, 20, 8)
        # 18시 초과 ~ 다음 날 9시 이하: 21일 05시의 24.0 이 20일 밤의 최저
        ms = series("m", now, temp=lambda t, _l: 24.0 if (t.day, t.hour) == (21, 5) else 28.0)
        by_day = {p.slot: p for p in points("night_min", ms, now)}
        self.assertEqual(by_day["2026-07-20"].value, 24.0)
        self.assertEqual(by_day["2026-07-20"].time, kst(2026, 7, 21, 5))
        self.assertEqual(by_day["2026-07-21"].value, 28.0)

    def test_window_edges_are_exclusive_inclusive(self):
        now = kst(2026, 1, 10, 0)
        # 아침(03시 초과 ~ 09시 이하): 03시 값은 제외, 09시 값은 포함
        ms = series("m", now, past=0, future=30, temp=lambda t, _l: -20.0 if t.hour == 3 else (-5.0 if t.hour == 9 else 0.0))
        self.assertEqual({p.slot: p.value for p in points("morning_min", ms, now)}["2026-01-10"], -5.0)

    def test_partly_past_window_without_past_data_uses_remaining_hours(self):
        now = kst(2026, 1, 10, 7)  # 아침 시간대(04~09시) 도중, 과거 자료 없음(단기예보처럼)
        ms = series("m", now, past=0, future=40, temp=lambda t, _l: -3.0 if t.hour == 8 else 1.0)
        self.assertEqual({p.slot: p.value for p in points("morning_min", ms, now)}["2026-01-10"], -3.0)

    def test_future_gap_invalidates_day(self):
        now = kst(2026, 1, 10, 0)
        ms = series("m", now, past=0, future=40, temp=lambda t, _l: None if (t.day, t.hour) == (10, 6) else 1.0)
        slots = {p.slot for p in points("morning_min", ms, now)}
        self.assertNotIn("2026-01-10", slots)
        self.assertIn("2026-01-11", slots)

    def test_fully_past_window_needs_complete_data(self):
        now = kst(2026, 1, 10, 15)
        ms = series("m", now, past=9, future=40, temp=const(1.0))  # 06시부터만 있음 → 오늘 아침(04~09시) 불완전
        self.assertNotIn("2026-01-10", {p.slot for p in points("morning_min", ms, now)})
        full = series("m", now, past=36, future=40, temp=const(1.0))
        self.assertIn("2026-01-10", {p.slot for p in points("morning_min", full, now)})


class PeriodChangeTest(unittest.TestCase):
    def test_drop_from_previous_morning(self):
        now = kst(2026, 1, 10, 15)
        mins = {10: 2.0, 11: -9.0, 12: -13.0}
        ms = series("m", now, temp=lambda t, _l: mins.get(t.day, 5.0) if t.hour == 6 else 5.0)
        change = {p.slot: p.value for p in points("morning_drop", ms, now)}
        self.assertEqual(change["2026-01-11"], -11.0)
        self.assertEqual(change["2026-01-12"], -4.0)


class HorizonTest(unittest.TestCase):
    def test_hourly_points_strictly_after_now(self):
        now = kst(2026, 7, 1, 9)
        ms = series("m", now, past=3, future=60, wind=const(5.0))
        end = now + timedelta(hours=48)
        leads = sorted(int((p.time - now) / HOUR) for p in points("wind", ms, now) if in_horizon(p, now, end))
        self.assertEqual(leads[0], 1)
        self.assertEqual(leads[-1], 48)

    def test_ongoing_day_counts_even_if_peak_passed(self):
        now = kst(2026, 7, 20, 17)
        ms = series("m", now, apparent=lambda t, _l: 34.0 if t.hour == 15 else 25.0)
        end = now + timedelta(hours=48)
        today = next(p for p in points("apparent_max", ms, now) if p.slot == "2026-07-20")
        self.assertTrue(in_horizon(today, now, end))  # 오늘 낮 최고는 지났지만 오늘은 아직 진행 중

    def test_day_whose_peak_is_beyond_horizon_is_excluded(self):
        now = kst(2026, 7, 20, 8)
        ms = series("m", now, apparent=lambda t, _l: 34.0 if t.hour == 15 else 25.0)
        end = now + timedelta(hours=48)
        by_day = {p.slot: p for p in points("apparent_max", ms, now)}
        self.assertTrue(in_horizon(by_day["2026-07-21"], now, end))  # +31시간
        self.assertFalse(in_horizon(by_day["2026-07-22"], now, end))  # 낮 최고가 +55시간


class ApparentTempTest(unittest.TestCase):
    def test_kma_formula_reference_values(self):
        # 기상청 산출식(예보업무규정 별표 11)을 손으로 계산한 값: 35℃·70% → Tw 30.26℃ → 체감 36.4℃
        self.assertAlmostEqual(kma_summer_apparent_temp(35.0, 70.0), 36.4, delta=0.1)
        self.assertAlmostEqual(kma_summer_apparent_temp(33.0, 50.0), 32.6, delta=0.2)

    def test_kma_rule_of_thumb(self):
        # 기상청 설명: 습도 55% 부근에서 체감온도 ≈ 기온, 습도 10%p 마다 약 1℃ 변화
        for temp in (30.0, 33.0, 36.0):
            self.assertAlmostEqual(kma_summer_apparent_temp(temp, 55.0), temp, delta=0.6)
            step = kma_summer_apparent_temp(temp, 65.0) - kma_summer_apparent_temp(temp, 55.0)
            self.assertTrue(0.6 < step < 1.4, step)

    def test_increases_with_humidity_and_temperature(self):
        self.assertGreater(kma_summer_apparent_temp(33, 80), kma_summer_apparent_temp(33, 40))
        self.assertGreater(kma_summer_apparent_temp(35, 60), kma_summer_apparent_temp(31, 60))

    def test_added_from_temp_and_humidity(self):
        now = kst(2026, 7, 20, 8)
        ms = series("m", now, past=0, future=5, temp=const(33.0), rh=lambda _t, lead: None if lead == 2 else 50.0)
        add_apparent_temp(ms)
        self.assertIsNone(ms.values["apparent"][2])
        self.assertAlmostEqual(ms.values["apparent"][0], 32.6, delta=0.3)

    def test_not_added_without_humidity(self):
        ms = ModelSeries("m", [kst(2026, 7, 20, 8)], {"temp": [33.0]})
        add_apparent_temp(ms)
        self.assertNotIn("apparent", ms.values)


class UnknownKindTest(unittest.TestCase):
    def test_raises(self):
        with self.assertRaises(ValueError):
            indicator_points(Indicator(key="x", kind="nope", label="x", unit=""), IND, ModelSeries("m", [], {}), kst(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
