"""주변 지역의 예보까지 보는 가능성 보정(호우): 주변에 큰비가 예보되면 그 지역의 가능성도 올린다."""

import unittest

from teukbo import area
from teukbo.assess import OfficialWarning, RegionOfficial, assess_region
from teukbo.config import Config

from .helpers import burst, config, const, kst, no_official, series

FULL = config()
NOW = kst(2026, 7, 10, 9)
MODELS = [m.id for m in FULL.settings.models]


def cfg_of(*ids: str) -> Config:
    return Config(regions=tuple(r for r in FULL.regions if r.id in ids), thresholds=FULL.thresholds, settings=FULL.settings)


def assess(cfg: Config, rain: dict[str, dict[str, float]], official: dict[str, RegionOfficial] | None = None):
    """rain: 지역 id → {모델 id: 12~15시간 뒤 3시간 동안의 시간당 강수량(mm)}. 적지 않은 모델·지역은 비 없음."""
    out = []
    for region in cfg.regions:
        forecasts = {m: series(m, NOW, precip=burst(rain.get(region.id, {}).get(m, 0.0), 12, 3), temp=const(24.0), wind=const(2.0)) for m in MODELS}
        out.append(assess_region(region, forecasts, (official or {}).get(region.id, no_official()), cfg, NOW))
    return out


def rain_of(assessments, region_id: str):
    ra = next(a for a in assessments if a.region.id == region_id)
    return next(h for h in ra.hazards if h.hazard.id == "heavy_rain_advisory")


class HelpersTest(unittest.TestCase):
    def test_points_become_a_percent_along_straight_lines(self):
        points = [[0.3, 0], [0.5, 30], [0.85, 60], [1.3, 95]]
        self.assertEqual([area.interpolate(points, x) for x in (0.0, 0.3, 0.4, 0.5, 0.675, 0.85, 1.3, 2.0)], [0, 0, 15, 30, 45, 60, 95, 95])
        self.assertEqual(area.interpolate([], 0.9), 0)

    def test_top_models_average(self):
        self.assertAlmostEqual(area.model_score([0.1, 0.9, 0.5, 0.7], 2), 0.8)
        self.assertAlmostEqual(area.model_score([0.4], 2), 0.4)
        self.assertIsNone(area.model_score([], 2))

    def test_neighbors_are_regions_within_the_radius(self):
        near = area.neighbors(FULL.regions, 30)
        self.assertIn("gwangyang", near["suncheon"])  # 순천–광양 약 19km
        self.assertIn("suncheon", near["suncheon"])
        self.assertNotIn("mokpo", near["suncheon"])  # 순천–목포 약 100km
        self.assertNotIn("boseong", near["suncheon"])  # 약 42km


class AreaScoreTest(unittest.TestCase):
    def test_setting_is_in_the_thresholds_file(self):
        conf = FULL.thresholds.types["heavy_rain"]["area_score"]
        self.assertEqual((conf["radius_km"], conf["top_models"], conf["levels"]), (30, 2, ["advisory"]))  # 경보 단계는 원래 계산식 그대로
        caution, high = FULL.settings.grade_caution_min, FULL.settings.grade_high_min
        self.assertEqual(area.interpolate(conf["points"], 0.5), caution)  # 0.5 이상이면 '주의'
        self.assertEqual(area.interpolate(conf["points"], 0.85), high)  # 0.85 이상이면 '높음'
        for other in ("strong_wind", "cold_wave", "heavy_snow", "heat_wave", "tropical_night"):
            self.assertNotIn("area_score", FULL.thresholds.types[other])  # 지금은 호우에만 쓴다

    def test_heavy_rain_forecast_nearby_raises_the_region(self):
        """광양에 모델 두 개가 3시간 45mm(기준 60mm 의 75%)를 예보 → 광양은 물론, 비 예보가 없는 이웃 순천도 '주의'. 먼 목포는 그대로."""
        cfg = cfg_of("suncheon", "gwangyang", "mokpo")
        assessments = assess(cfg, {"gwangyang": {MODELS[1]: 15.0, MODELS[2]: 15.0}})
        before = {rid: rain_of(assessments, rid).probability for rid in ("suncheon", "gwangyang", "mokpo")}
        self.assertEqual((before["suncheon"], before["mokpo"]), (0, 0))
        self.assertLess(before["gwangyang"], FULL.settings.grade_caution_min)  # 원래 계산식으로는 '주의'에 못 미친다
        changed = area.apply_area_scores(assessments, cfg)
        gwangyang, suncheon, mokpo = (rain_of(assessments, rid) for rid in ("gwangyang", "suncheon", "mokpo"))
        self.assertEqual(changed, 2)
        self.assertEqual((gwangyang.probability, gwangyang.model_grade, gwangyang.grade), (51, "caution", "caution"))  # 0.75 → 30 + 30×(0.25/0.35)
        self.assertEqual((gwangyang.area_value, gwangyang.area_from), (0.75, None))
        self.assertEqual((suncheon.probability, suncheon.grade, suncheon.area_from), (51, "caution", "광양시"))
        self.assertEqual((mokpo.probability, mokpo.grade, mokpo.area_value), (0, "low", None))

    def test_it_only_raises_never_lowers(self):
        """모든 모델이 기준을 넘기면 원래 계산식이 100% — 주변 보정이 이를 낮추지 않는다."""
        cfg = cfg_of("suncheon", "gwangyang")
        assessments = assess(cfg, {"suncheon": {m: 30.0 for m in MODELS}})
        self.assertEqual(rain_of(assessments, "suncheon").probability, 100)
        area.apply_area_scores(assessments, cfg)
        self.assertEqual(rain_of(assessments, "suncheon").probability, 100)
        self.assertIsNone(rain_of(assessments, "suncheon").area_value)
        self.assertGreaterEqual(rain_of(assessments, "gwangyang").probability, FULL.settings.grade_high_min)  # 이웃은 올라간다

    def test_official_warning_still_comes_first(self):
        """이미 발효 중인 특보가 있으면 등급은 '발효 중' 그대로다(가능성 숫자만 올라간다)."""
        cfg = cfg_of("suncheon", "gwangyang")
        active = RegionOfficial(available=True, active=[OfficialWarning("active", "heavy_rain", "호우", "advisory", "주의보", "L1051200", "", NOW)])
        assessments = assess(cfg, {"gwangyang": {MODELS[1]: 15.0, MODELS[2]: 15.0}}, {"suncheon": active})
        area.apply_area_scores(assessments, cfg)
        suncheon = rain_of(assessments, "suncheon")
        self.assertEqual((suncheon.grade, suncheon.model_grade), ("active", "caution"))

    def test_other_hazards_are_untouched(self):
        cfg = cfg_of("suncheon", "gwangyang")
        assessments = assess(cfg, {"gwangyang": {MODELS[1]: 15.0, MODELS[2]: 15.0}})
        before = [(h.hazard.id, h.probability) for ra in assessments for h in ra.hazards if h.hazard.type != "heavy_rain"]
        area.apply_area_scores(assessments, cfg)
        self.assertEqual(before, [(h.hazard.id, h.probability) for ra in assessments for h in ra.hazards if h.hazard.type != "heavy_rain"])


if __name__ == "__main__":
    unittest.main()
