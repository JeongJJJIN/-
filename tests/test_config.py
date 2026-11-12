import json
import tempfile
import unittest
from pathlib import Path

from teukbo.config import CONFIG_DIR, AllOf, ConfigError, load_regions, load_settings, load_thresholds
from teukbo.grid import latlon_to_grid

from .helpers import config


def _leaf_values(hazard):
    return {(leaf.indicator, leaf.op, leaf.value, leaf.consecutive_days) for leaf in hazard.leaves()}


class GridTest(unittest.TestCase):
    def test_known_official_cells(self):
        # 기상청 격자 위경도 표의 값
        self.assertEqual(latlon_to_grid(37.5665, 126.9780), (60, 127))  # 서울
        self.assertEqual(latlon_to_grid(35.1796, 129.0756), (98, 76))  # 부산
        self.assertEqual(latlon_to_grid(34.9507, 127.4872), (70, 70))  # 순천
        self.assertEqual(latlon_to_grid(33.4996, 126.5312), (53, 38))  # 제주

    def test_regions_grid_matches_latlon(self):
        for r in config().regions:
            self.assertEqual((r.nx, r.ny), latlon_to_grid(r.lat, r.lon), r.name)


class RegionsTest(unittest.TestCase):
    def test_required_regions_present(self):
        ids = {r.id for r in config().regions}
        self.assertIn("suncheon", ids)
        self.assertIn("beolgyo", ids)
        self.assertEqual(len(config().regions), 23)  # 전남 22개 시·군 + 벌교

    def test_zone_codes(self):
        by_id = {r.id: r for r in config().regions}
        self.assertEqual(by_id["suncheon"].zone_codes, ("L1051200",))
        self.assertEqual(by_id["beolgyo"].zone_codes, by_id["boseong"].zone_codes)  # 벌교는 보성 특보구역
        self.assertIn("L1053110", by_id["goheung"].zone_codes)  # 세분 구역(고흥북부)
        for r in config().regions:
            self.assertTrue(r.zone_codes, r.name)
            self.assertEqual(len(r.zone_codes), len(r.zone_names), r.name)
            self.assertIn("전라남도", r.group_names)
            for code in r.zone_codes:
                self.assertRegex(code, r"^L105\d{4}$")

    def test_tropical_night_regional_threshold(self):
        by_id = {r.id: r for r in config().regions}
        self.assertEqual(by_id["suncheon"].params["tropical_night_min"], 26)
        self.assertEqual(by_id["damyang"].params["tropical_night_min"], 25)


class ThresholdsTest(unittest.TestCase):
    """설정 파일의 기준값이 공식 발표기준(기상법 시행령 별표 1)과 같은지 지킨다."""

    def setUp(self):
        self.th = config().thresholds

    def test_heavy_rain(self):
        self.assertEqual(_leaf_values(self.th.hazard("heavy_rain_advisory")), {("rain_3h", ">=", 60, 1), ("rain_12h", ">=", 110, 1)})
        self.assertEqual(_leaf_values(self.th.hazard("heavy_rain_warning")), {("rain_3h", ">=", 90, 1), ("rain_12h", ">=", 180, 1)})
        self.assertEqual(self.th.indicators["rain_3h"].window_hours, 3)
        self.assertEqual(self.th.indicators["rain_12h"].window_hours, 12)

    def test_strong_wind(self):
        self.assertEqual(_leaf_values(self.th.hazard("strong_wind_advisory")), {("wind", ">=", 14, 1), ("gust", ">=", 20, 1)})
        self.assertEqual(_leaf_values(self.th.hazard("strong_wind_warning")), {("wind", ">=", 21, 1), ("gust", ">=", 26, 1)})

    def test_heavy_snow(self):
        self.assertEqual(_leaf_values(self.th.hazard("heavy_snow_advisory")), {("snow_24h", ">=", 5, 1)})
        self.assertEqual(_leaf_values(self.th.hazard("heavy_snow_warning")), {("snow_24h", ">=", 20, 1)})
        self.assertEqual(self.th.indicators["snow_24h"].window_hours, 24)

    def test_heat_wave(self):
        self.assertEqual(_leaf_values(self.th.hazard("heat_wave_advisory")), {("apparent_max", ">=", 33, 2)})
        self.assertEqual(_leaf_values(self.th.hazard("heat_wave_warning")), {("apparent_max", ">=", 35, 2)})
        # 폭염중대경보(2026 신설): 체감 38℃ 이상 또는 기온 39℃ 이상, 1일
        self.assertEqual(_leaf_values(self.th.hazard("heat_wave_severe")), {("apparent_max", ">=", 38, 1), ("temp_max", ">=", 39, 1)})
        self.assertEqual(self.th.hazard("heat_wave_severe").level, "severe")

    def test_tropical_night(self):
        hz = self.th.hazard("tropical_night_advisory")
        self.assertEqual(len(hz.criteria), 1)
        self.assertIsInstance(hz.criteria[0], AllOf)
        self.assertEqual(_leaf_values(hz), {("apparent_max", ">=", 33, 2), ("night_min", ">=", 25, 1)})
        night = self.th.indicators["night_min"]
        self.assertEqual((night.start_hour, night.end_hour, night.stat), (18, 9, "min"))

    def test_cold_wave(self):
        adv = self.th.hazard("cold_wave_advisory")
        self.assertEqual(_leaf_values(adv), {("morning_drop", "<=", -10, 1), ("morning_min", "<=", 3, 1), ("morning_min", "<=", -12, 2)})
        self.assertEqual(set(adv.months), {10, 11, 12, 1, 2, 3, 4})
        warn = self.th.hazard("cold_wave_warning")
        self.assertEqual(_leaf_values(warn), {("morning_drop", "<=", -15, 1), ("morning_min", "<=", 3, 1), ("morning_min", "<=", -15, 2)})

    def test_typhoon_is_official_only(self):
        self.assertEqual(self.th.hazard("typhoon_advisory").method, "official_only")
        self.assertEqual(self.th.hazard("typhoon_warning").method, "official_only")

    def test_level_ranks(self):
        self.assertLess(self.th.level_rank("advisory"), self.th.level_rank("warning"))
        self.assertLess(self.th.level_rank("warning"), self.th.level_rank("severe"))


class ValidationTest(unittest.TestCase):
    def _write(self, name, data):
        d = Path(tempfile.mkdtemp())
        path = d / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def _thresholds(self):
        return json.loads((CONFIG_DIR / "thresholds.json").read_text(encoding="utf-8"))

    def test_unknown_indicator_rejected(self):
        data = self._thresholds()
        data["hazards"][0]["criteria"][0]["indicator"] = "nope"
        with self.assertRaises(ConfigError):
            load_thresholds(self._write("thresholds.json", data))

    def test_bad_operator_rejected(self):
        data = self._thresholds()
        data["hazards"][0]["criteria"][0]["op"] = "=="
        with self.assertRaises(ConfigError):
            load_thresholds(self._write("thresholds.json", data))

    def test_baseline_on_wrong_side_rejected(self):
        data = self._thresholds()
        data["hazards"][0]["criteria"][0]["baseline"] = 100
        with self.assertRaises(ConfigError):
            load_thresholds(self._write("thresholds.json", data))

    def test_duplicate_region_rejected(self):
        data = json.loads((CONFIG_DIR / "regions.json").read_text(encoding="utf-8"))
        data["regions"].append(dict(data["regions"][0]))
        with self.assertRaises(ConfigError):
            load_regions(self._write("regions.json", data))

    def test_weights_must_sum_to_one(self):
        data = json.loads((CONFIG_DIR / "settings.json").read_text(encoding="utf-8"))
        data["scoring"]["weight_exceed_ratio"] = 0.9
        with self.assertRaises(ConfigError):
            load_settings(self._write("settings.json", data))

    def test_threshold_change_takes_effect_without_code_change(self):
        data = self._thresholds()
        data["hazards"][0]["criteria"][0]["value"] = 55
        th = load_thresholds(self._write("thresholds.json", data))
        self.assertIn(("rain_3h", ">=", 55, 1), _leaf_values(th.hazard("heavy_rain_advisory")))


if __name__ == "__main__":
    unittest.main()
