import unittest
from datetime import timedelta

from teukbo.assess import OfficialWarning, RegionOfficial, assess_region

from .helpers import burst, config, const, kst, no_official, region, series

CFG = config()


def by_id(assessment):
    return {h.hazard.id: h for h in assessment.hazards}


def official_warning(status, type_id, level=None, effective_from=None, expected=""):
    th = CFG.thresholds
    return OfficialWarning(
        status=status,
        type=type_id,
        type_name=th.type_name(type_id) if type_id else "건조",
        level=level,
        level_name=th.level_name(level) if level else "예비",
        zone_name="순천",
        effective_from=effective_from,
        expected_text=expected,
    )


class HeavyRainTest(unittest.TestCase):
    def setUp(self):
        self.now = kst(2026, 11, 12, 21)
        self.models = {
            "ecmwf_ifs025": series("ecmwf_ifs025", self.now, precip=burst(25.0, 10, 3)),  # 3시간 75mm
            "ncep_gfs_seamless": series("ncep_gfs_seamless", self.now, precip=burst(15.0, 10, 3)),  # 45mm
            "dwd_icon_seamless": series("dwd_icon_seamless", self.now, precip=burst(22.0, 11, 3)),  # 66mm, 1시간 늦게
            "jma_seamless": series("jma_seamless", self.now, precip=const(0.0)),
        }
        self.result = by_id(assess_region(region(), self.models, no_official(), CFG, self.now))

    def test_advisory_probability_and_stats(self):
        a = self.result["heavy_rain_advisory"]
        self.assertEqual(a.stats.models_total, 4)
        self.assertEqual(a.stats.models_exceeding, 2)
        self.assertAlmostEqual(a.stats.exceed_ratio, 0.5)
        self.assertAlmostEqual(a.stats.reach_max, 1.25)
        self.assertEqual(a.probability, 56)
        self.assertEqual(a.model_grade, "caution")
        self.assertEqual(a.grade, "caution")

    def test_expected_window_is_first_exceed_range(self):
        a = self.result["heavy_rain_advisory"]
        # 25mm×3: +10,+11,+12시 → 3시간 합이 60을 넘는 첫 시각은 +12시. 22mm×3(+11~+13) → +13시
        self.assertEqual(a.window_from, self.now + timedelta(hours=12))
        self.assertEqual(a.window_to, self.now + timedelta(hours=13))
        self.assertEqual(a.peak_model, "ecmwf_ifs025")

    def test_per_model_results(self):
        pm = self.result["heavy_rain_advisory"].per_model
        self.assertAlmostEqual(pm["ecmwf_ifs025"].reach, 1.25)
        self.assertTrue(pm["ecmwf_ifs025"].exceeds)
        self.assertAlmostEqual(pm["ncep_gfs_seamless"].reach, 0.75)
        self.assertFalse(pm["ncep_gfs_seamless"].exceeds)
        self.assertIsNone(pm["ncep_gfs_seamless"].first_exceed)
        self.assertEqual(pm["jma_seamless"].reach, 0.0)

    def test_warning_level_uses_its_own_thresholds(self):
        w = self.result["heavy_rain_warning"]
        self.assertEqual(w.stats.models_exceeding, 0)
        self.assertAlmostEqual(w.stats.reach_max, 75 / 90)
        self.assertIsNone(w.window_from)
        self.assertLess(w.probability, self.result["heavy_rain_advisory"].probability)

    def test_criteria_rows_for_table(self):
        rows = self.result["heavy_rain_advisory"].rows
        self.assertEqual([r.leaf.indicator for r in rows], ["rain_3h", "rain_12h"])
        self.assertEqual(rows[0].threshold, 60)
        self.assertEqual(rows[0].models["ecmwf_ifs025"].value, 75.0)
        self.assertTrue(rows[0].models["ecmwf_ifs025"].exceeds)
        self.assertEqual(rows[1].models["ecmwf_ifs025"].value, 75.0)  # 12시간 합도 75mm
        self.assertFalse(rows[1].models["ecmwf_ifs025"].exceeds)

    def test_twelve_hour_criterion_alone_can_trigger(self):
        models = {"m": series("ecmwf_ifs025", self.now, precip=burst(10.0, 5, 12))}  # 3시간 30mm, 12시간 120mm
        a = by_id(assess_region(region(), {"ecmwf_ifs025": models["m"]}, no_official(), CFG, self.now))["heavy_rain_advisory"]
        self.assertTrue(a.per_model["ecmwf_ifs025"].exceeds)
        self.assertEqual(a.window_from, self.now + timedelta(hours=15))  # 11번째 시간에 110mm 도달

    def test_rain_beyond_horizon_is_ignored(self):
        models = {"ecmwf_ifs025": series("ecmwf_ifs025", self.now, precip=burst(40.0, 55, 3))}  # +55시간
        a = by_id(assess_region(region(), models, no_official(), CFG, self.now))["heavy_rain_advisory"]
        self.assertEqual(a.probability, 0)

    def test_rain_already_fallen_counts_toward_running_total(self):
        # 지난 2시간에 50mm, 앞으로 1시간에 15mm → 3시간 합 65mm (진행 중인 비)
        models = {"ecmwf_ifs025": series("ecmwf_ifs025", self.now, precip=lambda _t, lead: 25.0 if lead in (-1, 0) else (15.0 if lead == 1 else 0.0))}
        a = by_id(assess_region(region(), models, no_official(), CFG, self.now))["heavy_rain_advisory"]
        self.assertTrue(a.per_model["ecmwf_ifs025"].exceeds)

    def test_forecast_only_source_without_past_data(self):
        # 단기예보처럼 과거 자료가 없는 출처: 받은 구간 안에서만 누적을 만든다
        models = {"kma_dfs": series("kma_dfs", self.now + timedelta(hours=1), past=0, future=60, precip=burst(30.0, 4, 3))}
        a = by_id(assess_region(region(), models, no_official(), CFG, self.now))["heavy_rain_advisory"]
        self.assertTrue(a.per_model["kma_dfs"].exceeds)
        self.assertAlmostEqual(a.per_model["kma_dfs"].reach, 1.5)


class StrongWindTest(unittest.TestCase):
    def test_model_without_gust_still_counts_on_wind_speed(self):
        now = kst(2026, 11, 12, 21)
        models = {
            "ecmwf_ifs025": series("ecmwf_ifs025", now, wind=const(10.0), gust=burst(21.0, 5, 2)),  # 순간풍속으로 초과
            "jma_seamless": series("jma_seamless", now, wind=const(15.0)),  # 돌풍 자료 없음, 풍속으로 초과
            "kma_dfs": series("kma_dfs", now, precip=const(0.0)),  # 바람 자료 없음 → 이 특보에서는 제외
        }
        a = by_id(assess_region(region(), models, no_official(), CFG, now))["strong_wind_advisory"]
        self.assertEqual(a.stats.models_total, 2)
        self.assertEqual(a.stats.models_exceeding, 2)
        self.assertEqual(a.probability, 100)
        gust_row = next(r for r in a.rows if r.leaf.indicator == "gust")
        self.assertIsNone(gust_row.models["jma_seamless"])


class HeatWaveTest(unittest.TestCase):
    def setUp(self):
        self.now = kst(2026, 7, 20, 8)

    def _model(self, name, peaks):
        return series(name, self.now, temp=const(28.0), apparent=lambda t, _l: peaks.get(t.day, 28.0) if t.hour == 15 else 26.0)

    def test_two_consecutive_days_required(self):
        models = {
            "ecmwf_ifs025": self._model("ecmwf_ifs025", {20: 34.0, 21: 33.5, 22: 31.0}),  # 이틀 연속 33 이상
            "ncep_gfs_seamless": self._model("ncep_gfs_seamless", {20: 34.0, 21: 32.0, 22: 34.0}),  # 하루씩 끊김
        }
        a = by_id(assess_region(region(), models, no_official(), CFG, self.now))["heat_wave_advisory"]
        self.assertTrue(a.per_model["ecmwf_ifs025"].exceeds)
        self.assertFalse(a.per_model["ncep_gfs_seamless"].exceeds)
        # 끊긴 모델의 도달률은 '이틀 유지 수준'(32℃) 기준: (32-25)/(33-25)
        self.assertAlmostEqual(a.per_model["ncep_gfs_seamless"].reach, 0.875)
        self.assertEqual(a.stats.models_exceeding, 1)

    def test_warning_and_severe_levels(self):
        models = {"ecmwf_ifs025": self._model("ecmwf_ifs025", {20: 38.5, 21: 36.0, 22: 35.5})}
        result = by_id(assess_region(region(), models, no_official(), CFG, self.now))
        self.assertTrue(result["heat_wave_warning"].per_model["ecmwf_ifs025"].exceeds)  # 35 이상 2일
        self.assertTrue(result["heat_wave_severe"].per_model["ecmwf_ifs025"].exceeds)  # 38 이상 1일

    def test_severe_by_air_temperature_39_inclusive(self):
        models = {"ecmwf_ifs025": series("ecmwf_ifs025", self.now, apparent=const(30.0), temp=lambda t, _l: 39.0 if (t.day, t.hour) == (21, 15) else 30.0)}
        a = by_id(assess_region(region(), models, no_official(), CFG, self.now))["heat_wave_severe"]
        self.assertTrue(a.per_model["ecmwf_ifs025"].exceeds)  # '39℃ 이상' (초과가 아님)

    def test_winter_values_give_zero(self):
        now = kst(2026, 1, 10, 9)
        models = {"ecmwf_ifs025": series("ecmwf_ifs025", now, temp=const(2.0), rh=const(60.0))}
        result = by_id(assess_region(region(), models, no_official(), CFG, now))
        self.assertEqual(result["heat_wave_advisory"].probability, 0)
        self.assertEqual(result["heat_wave_advisory"].grade, "low")


class TropicalNightTest(unittest.TestCase):
    def test_regional_threshold(self):
        now = kst(2026, 7, 20, 8)
        # 낮에는 체감 34℃(2일 연속), 밤 최저 25.5℃
        model = lambda: series(  # noqa: E731
            "ecmwf_ifs025",
            now,
            apparent=lambda t, _l: 34.0 if t.hour == 15 else 27.0,
            temp=lambda t, _l: 25.5 if (t.hour >= 19 or t.hour <= 9) else 31.0,
        )
        coastal = by_id(assess_region(region("suncheon"), {"ecmwf_ifs025": model()}, no_official(), CFG, now))["tropical_night_advisory"]
        inland = by_id(assess_region(region("damyang"), {"ecmwf_ifs025": model()}, no_official(), CFG, now))["tropical_night_advisory"]
        self.assertFalse(coastal.per_model["ecmwf_ifs025"].exceeds)  # 순천은 26℃ 기준
        self.assertTrue(inland.per_model["ecmwf_ifs025"].exceeds)  # 담양은 25℃ 기준
        night_row = next(r for r in coastal.rows if r.leaf.indicator == "night_min")
        self.assertEqual(night_row.threshold, 26)
        self.assertTrue(night_row.joint)

    def test_needs_heat_condition_too(self):
        now = kst(2026, 7, 20, 8)
        models = {"ecmwf_ifs025": series("ecmwf_ifs025", now, apparent=const(30.0), temp=const(27.0))}  # 밤은 덥지만 낮 체감 30℃
        a = by_id(assess_region(region("damyang"), models, no_official(), CFG, now))["tropical_night_advisory"]
        self.assertFalse(a.per_model["ecmwf_ifs025"].exceeds)
        self.assertAlmostEqual(a.per_model["ecmwf_ifs025"].reach, (30 - 25) / (33 - 25))  # 묶음에서는 낮은 쪽


class ColdWaveTest(unittest.TestCase):
    def setUp(self):
        self.now = kst(2026, 1, 10, 15)

    def _model(self, mins):
        # 아침 06시에 그날 최저, 나머지 시간은 따뜻하게
        return series("ecmwf_ifs025", self.now, temp=lambda t, _l: mins.get(t.day, 5.0) if t.hour == 6 else 20.0)

    def test_sharp_drop_criterion(self):
        # 10일 아침 2℃ → 11일 아침 -9℃: 11℃ 하강, 3℃ 이하
        result = by_id(assess_region(region(), {"ecmwf_ifs025": self._model({10: 2.0, 11: -9.0, 12: -6.0, 13: -6.0})}, no_official(), CFG, self.now))
        adv = result["cold_wave_advisory"]
        self.assertTrue(adv.per_model["ecmwf_ifs025"].exceeds)
        self.assertEqual(adv.window_from, kst(2026, 1, 11, 6))
        warn = result["cold_wave_warning"]
        self.assertFalse(warn.per_model["ecmwf_ifs025"].exceeds)  # 15℃ 이상 하강은 아님
        self.assertAlmostEqual(warn.per_model["ecmwf_ifs025"].reach, 11 / 15, places=2)

    def test_drop_without_low_temperature_does_not_trigger(self):
        # 16℃ → 5℃: 11℃ 하강했지만 3℃ 이하가 아님
        a = by_id(assess_region(region(), {"ecmwf_ifs025": self._model({10: 16.0, 11: 5.0, 12: 5.0, 13: 5.0})}, no_official(), CFG, self.now))["cold_wave_advisory"]
        self.assertFalse(a.per_model["ecmwf_ifs025"].exceeds)
        self.assertAlmostEqual(a.per_model["ecmwf_ifs025"].reach, 0.8)  # (5-13)/(3-13)

    def test_two_days_below_minus_12(self):
        a = by_id(assess_region(region(), {"ecmwf_ifs025": self._model({10: -8.0, 11: -10.0, 12: -13.0, 13: -13.0})}, no_official(), CFG, self.now))["cold_wave_advisory"]
        self.assertTrue(a.per_model["ecmwf_ifs025"].exceeds)
        self.assertEqual(a.window_from, kst(2026, 1, 12, 6))

    def test_single_cold_day_is_not_enough(self):
        a = by_id(assess_region(region(), {"ecmwf_ifs025": self._model({10: -8.0, 11: -13.0, 12: -8.0, 13: -8.0})}, no_official(), CFG, self.now))["cold_wave_advisory"]
        self.assertFalse(a.per_model["ecmwf_ifs025"].exceeds)

    def test_off_season_is_zero_not_unknown(self):
        now = kst(2026, 7, 10, 15)
        models = {"ecmwf_ifs025": series("ecmwf_ifs025", now, temp=const(-20.0))}  # 여름에 말이 안 되는 값이 와도
        a = by_id(assess_region(region(), models, no_official(), CFG, now))["cold_wave_advisory"]
        self.assertTrue(a.off_season)
        self.assertEqual(a.probability, 0)
        self.assertEqual(a.grade, "low")


class OfficialOverrideTest(unittest.TestCase):
    def setUp(self):
        self.now = kst(2026, 11, 12, 21)
        self.models = {
            "ecmwf_ifs025": series("ecmwf_ifs025", self.now, precip=const(0.0), snow=const(0.0), wind=const(3.0), gust=const(5.0), temp=const(10.0), rh=const(60.0))
        }

    def _assess(self, official):
        return by_id(assess_region(region(), self.models, official, CFG, self.now))

    def test_preliminary_raises_lowest_level_to_high(self):
        official = RegionOfficial(available=True, preliminary=[official_warning("preliminary", "heavy_rain", expected="13일 새벽")])
        result = self._assess(official)
        self.assertEqual(result["heavy_rain_advisory"].model_grade, "low")
        self.assertEqual(result["heavy_rain_advisory"].grade, "high")
        self.assertEqual(result["heavy_rain_advisory"].official_status, "preliminary")
        self.assertEqual(result["heavy_rain_warning"].grade, "low")  # 단계 미상 예비특보는 주의보에만
        self.assertEqual(result["strong_wind_advisory"].grade, "low")  # 다른 종류에는 영향 없음

    def test_active_advisory(self):
        start = kst(2026, 11, 12, 18)
        official = RegionOfficial(available=True, active=[official_warning("active", "heavy_rain", "advisory", start)])
        result = self._assess(official)
        self.assertEqual(result["heavy_rain_advisory"].grade, "active")
        self.assertEqual(result["heavy_rain_advisory"].official_from, start)
        self.assertEqual(result["heavy_rain_advisory"].probability, 0)  # 계산값은 그대로 남긴다
        self.assertEqual(result["heavy_rain_warning"].grade, "low")

    def test_active_warning_covers_lower_level(self):
        official = RegionOfficial(available=True, active=[official_warning("active", "heavy_rain", "warning")])
        result = self._assess(official)
        self.assertEqual(result["heavy_rain_warning"].grade, "active")
        self.assertEqual(result["heavy_rain_advisory"].grade, "active")
        self.assertEqual(result["heavy_rain_advisory"].official_level, "warning")

    def test_typhoon_only_from_official(self):
        self.assertEqual(self._assess(no_official(True))["typhoon_advisory"].grade, "low")
        self.assertIsNone(self._assess(no_official(True))["typhoon_advisory"].probability)
        self.assertEqual(self._assess(no_official(False))["typhoon_advisory"].grade, "unknown")
        official = RegionOfficial(available=True, preliminary=[official_warning("preliminary", "typhoon")])
        self.assertEqual(self._assess(official)["typhoon_advisory"].grade, "high")
        official = RegionOfficial(available=True, active=[official_warning("active", "typhoon", "warning")])
        self.assertEqual(self._assess(official)["typhoon_warning"].grade, "active")

    def test_uncomputed_type_is_listed_but_changes_nothing(self):
        official = RegionOfficial(available=True, active=[official_warning("active", None, "advisory")])  # 건조주의보
        assessment = assess_region(region(), self.models, official, CFG, self.now)
        self.assertEqual(len(assessment.official.active), 1)
        self.assertEqual({h.grade for h in assessment.hazards}, {"low"})


class RegionSummaryTest(unittest.TestCase):
    def test_top_picks_highest_grade_then_probability(self):
        now = kst(2026, 11, 12, 21)
        models = {"ecmwf_ifs025": series("ecmwf_ifs025", now, precip=burst(25.0, 10, 3), wind=const(3.0))}
        assessment = assess_region(region(), models, no_official(), CFG, now)
        self.assertEqual(assessment.top().hazard.id, "heavy_rain_advisory")
        official = RegionOfficial(available=True, active=[official_warning("active", "strong_wind", "advisory")])
        self.assertEqual(assess_region(region(), models, official, CFG, now).top().hazard.id, "strong_wind_advisory")

    def test_no_models_gives_unknown(self):
        now = kst(2026, 11, 12, 21)
        assessment = assess_region(region(), {}, no_official(False), CFG, now)
        self.assertTrue(all(h.grade == "unknown" for h in assessment.hazards))
        self.assertIsNone(assessment.top())

    def test_extremes_for_history(self):
        now = kst(2026, 11, 12, 21)
        models = {"ecmwf_ifs025": series("ecmwf_ifs025", now, precip=burst(25.0, 10, 3), temp=const(8.0))}
        extremes = assess_region(region(), models, no_official(), CFG, now).extremes["ecmwf_ifs025"]
        self.assertEqual(extremes["rain_3h"], (75.0, 12))
        self.assertEqual(extremes["morning_min"][0], 8.0)
        self.assertNotIn("gust", extremes)


if __name__ == "__main__":
    unittest.main()
