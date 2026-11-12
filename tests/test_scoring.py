import unittest

from teukbo.config import ScoringParams
from teukbo.scoring import apply_official, compute_probability, grade_for, max_grade, reach_ratio

from .helpers import config

PARAMS = ScoringParams(weight_exceed_ratio=0.5, weight_max_reach=0.2, weight_mean_reach=0.3, reach_floor=0.5)


class ReachRatioTest(unittest.TestCase):
    def test_plain_ratio_when_baseline_zero(self):
        self.assertAlmostEqual(reach_ratio(45, 60), 0.75)
        self.assertAlmostEqual(reach_ratio(90, 60), 1.5)

    def test_baseline_shifts_zero_point(self):
        # 체감온도 33℃ 기준, 바닥 25℃: 29℃ 는 절반
        self.assertAlmostEqual(reach_ratio(29, 33, 25), 0.5)

    def test_below_type_threshold(self):
        # 한파 -12℃ 이하: -6℃ 는 절반, -15℃ 는 초과
        self.assertAlmostEqual(reach_ratio(-6, -12), 0.5)
        self.assertGreater(reach_ratio(-15, -12), 1.0)

    def test_never_negative(self):
        self.assertEqual(reach_ratio(5, -12), 0.0)
        self.assertEqual(reach_ratio(20, 33, 25), 0.0)


class ProbabilityTest(unittest.TestCase):
    def test_all_models_exceed_is_100(self):
        p = compute_probability([1.2, 1.0, 1.5, 2.0], [True] * 4, PARAMS)
        self.assertEqual(p.percent, 100)
        self.assertEqual(p.exceed_ratio, 1.0)
        self.assertEqual((p.models_total, p.models_exceeding), (4, 4))

    def test_nothing_near_threshold_is_0(self):
        p = compute_probability([0.1, 0.3, 0.5, 0.0], [False] * 4, PARAMS)
        self.assertEqual(p.percent, 0)

    def test_consensus_just_below_threshold(self):
        # 모든 모델이 기준의 90%: 초과 0, 점수(0.9)=0.8 → (0.2+0.3)×0.8 = 40%
        p = compute_probability([0.9] * 4, [False] * 4, PARAMS)
        self.assertEqual(p.percent, 40)

    def test_half_exceed(self):
        # 초과 비율 0.5 → 25, 최대 도달 1.25 → 20, 평균(1,0.75,1,0)=0.6875 → 0.3×0.375=11.25
        p = compute_probability([1.25, 0.75, 1.1, 0.0], [True, False, True, False], PARAMS)
        self.assertEqual(p.percent, 56)
        self.assertAlmostEqual(p.reach_max, 1.25)
        self.assertAlmostEqual(p.exceed_ratio, 0.5)

    def test_single_outlier_does_not_dominate(self):
        p = compute_probability([2.0, 0.3, 0.3, 0.3], [True, False, False, False], PARAMS)
        self.assertEqual(p.percent, 32)  # 12.5 + 20 + 0

    def test_monotonic_in_reach(self):
        previous = -1
        for r in (0.0, 0.4, 0.6, 0.8, 0.95, 1.0, 1.3):
            p = compute_probability([r] * 3, [r >= 1.0] * 3, PARAMS).percent
            self.assertGreaterEqual(p, previous)
            previous = p

    def test_single_model(self):
        self.assertEqual(compute_probability([1.1], [True], PARAMS).percent, 100)
        self.assertEqual(compute_probability([0.75], [False], PARAMS).percent, 25)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            compute_probability([], [], PARAMS)
        with self.assertRaises(ValueError):
            compute_probability([1.0], [True, False], PARAMS)

    def test_settings_weights_sum_to_one(self):
        s = config().settings.scoring
        self.assertAlmostEqual(s.weight_exceed_ratio + s.weight_max_reach + s.weight_mean_reach, 1.0)


class GradeTest(unittest.TestCase):
    def test_grade_boundaries(self):
        self.assertEqual(grade_for(0, 30, 60), "low")
        self.assertEqual(grade_for(29, 30, 60), "low")
        self.assertEqual(grade_for(30, 30, 60), "caution")
        self.assertEqual(grade_for(59, 30, 60), "caution")
        self.assertEqual(grade_for(60, 30, 60), "high")
        self.assertEqual(grade_for(None, 30, 60), "unknown")

    def test_official_overrides(self):
        self.assertEqual(apply_official("low", "active"), "active")
        self.assertEqual(apply_official("high", "active"), "active")
        self.assertEqual(apply_official("low", "preliminary"), "high")
        self.assertEqual(apply_official("caution", "preliminary"), "high")
        self.assertEqual(apply_official("high", "preliminary"), "high")
        self.assertEqual(apply_official("caution", None), "caution")

    def test_max_grade(self):
        self.assertEqual(max_grade("low", "high"), "high")
        self.assertEqual(max_grade("active", "high"), "active")


if __name__ == "__main__":
    unittest.main()
