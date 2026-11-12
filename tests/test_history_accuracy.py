import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from teukbo import accuracy, storage
from teukbo.assess import OfficialWarning, RegionOfficial, assess_region
from teukbo.timeutil import iso

from .helpers import burst, config, const, kst, region, series

CFG = config()


def run_record(t, regions):
    return {"t": iso(t), "ok": {}, "models": ["m"], "ind": ["rain_3h"], "regions": regions}


def region_record(grades=None, active=(), known=True):
    """grades: 특보 id → 모델 등급. active: [(종류, 단계, 발효 시각 또는 None)]"""
    rec = {"x": {"m": [[1.0, 1]]}, "h": {hid: {"p": 50, "mg": g, "g": g} for hid, g in (grades or {}).items()}}
    if known:
        rec["a"] = [{"k": k, "l": level, "n": "", "f": iso(f) if f else None} for k, level, f in active]
        rec["pre"] = []
    return rec


class HistoryFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_append_by_day_and_never_drop(self):
        t0 = kst(2026, 11, 12, 21)
        storage.append_history(self.dir, run_record(t0, {}), t0)
        storage.append_history(self.dir, run_record(t0 + timedelta(hours=3), {}), t0 + timedelta(hours=3))  # 다음 날 00시
        storage.append_history(self.dir, run_record(t0 - timedelta(hours=3), {}), t0 - timedelta(hours=3))
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.json")), ["2026-11-12.json", "2026-11-13.json"])
        doc = json.loads((self.dir / "2026-11-12.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["date"], "2026-11-12")
        self.assertEqual([r["t"] for r in doc["runs"]], [iso(t0 - timedelta(hours=3)), iso(t0)])  # 시간순

    def test_same_time_replaces(self):
        t0 = kst(2026, 11, 12, 21)
        storage.append_history(self.dir, run_record(t0, {"a": {}}), t0)
        storage.append_history(self.dir, run_record(t0, {"b": {}}), t0)
        doc = json.loads((self.dir / "2026-11-12.json").read_text(encoding="utf-8"))
        self.assertEqual(len(doc["runs"]), 1)
        self.assertEqual(list(doc["runs"][0]["regions"]), ["b"])

    def test_history_run_content(self):
        now = kst(2026, 11, 12, 21)
        models = {"ecmwf_ifs025": series("ecmwf_ifs025", now, precip=burst(25.0, 10, 3), temp=const(10.0), wind=const(3.0))}
        th = CFG.thresholds
        official = RegionOfficial(
            available=True,
            active=[OfficialWarning("active", "strong_wind", th.type_name("strong_wind"), "advisory", th.level_name("advisory"), effective_from=kst(2026, 11, 12, 18))],
        )
        run = storage.build_history_run([assess_region(region(), models, official, CFG, now)], CFG, now, {"open_meteo": True})
        rec = run["regions"]["suncheon"]
        self.assertEqual(run["t"], "2026-11-12T21:00+09:00")
        self.assertEqual(run["thresholds"], th.version)
        idx = run["ind"].index("rain_3h")
        self.assertEqual(rec["x"]["ecmwf_ifs025"][idx], [75.0, 12])  # [극값, 선행시간]
        self.assertEqual(rec["h"]["heavy_rain_advisory"]["mg"], "high")
        self.assertEqual(rec["h"]["heavy_rain_advisory"]["p"], 100)
        self.assertEqual(rec["h"]["heavy_rain_advisory"]["w"][0], "2026-11-13T09:00+09:00")
        self.assertEqual(rec["h"]["strong_wind_advisory"]["g"], "active")
        self.assertEqual(rec["h"]["strong_wind_advisory"]["mg"], "low")
        self.assertNotIn("heavy_snow_advisory", rec["h"])  # 가능성 0% 는 생략
        self.assertEqual(rec["a"], [{"k": "strong_wind", "l": "advisory", "n": "강풍주의보", "f": "2026-11-12T18:00+09:00"}])

    def test_quiet_regions_keep_grades_but_drop_raw_extremes(self):
        """전국으로 넓히며: 어느 기준에도 한참 못 미친 조용한 지역은 모델별 극값을 남기지 않는다(등급·특보 기록은 남는다)."""
        now = kst(2026, 11, 12, 21)
        calm = {"ecmwf_ifs025": series("ecmwf_ifs025", now, precip=const(0.0), temp=const(10.0), wind=const(2.0))}
        wet = {"ecmwf_ifs025": series("ecmwf_ifs025", now, precip=burst(25.0, 10, 3), temp=const(10.0), wind=const(2.0))}
        known = RegionOfficial(available=True)
        run = storage.build_history_run([assess_region(region("suncheon"), calm, known, CFG, now), assess_region(region("mokpo"), wet, known, CFG, now)], CFG, now, {"open_meteo": True})
        quiet, busy = run["regions"]["suncheon"], run["regions"]["mokpo"]
        self.assertEqual((quiet.get("q"), "x" in quiet, quiet["h"], quiet["a"], quiet["pre"]), (1, False, {}, [], []))
        self.assertIn("x", busy)
        self.assertNotIn("q", busy)
        self.assertLess(len(json.dumps(quiet)), 60)
        # 조용해도 공식 특보·예비특보가 있으면 극값을 남긴다
        th = CFG.thresholds
        warned = RegionOfficial(available=True, preliminary=[OfficialWarning("preliminary", "heavy_rain", th.type_name("heavy_rain"), None, "예비")])
        self.assertIn("x", storage.build_history_run([assess_region(region("suncheon"), calm, warned, CFG, now)], CFG, now, {})["regions"]["suncheon"])
        # 적중률: 조용해서 극값을 생략한 회차도 '예측이 있었다(낮음)'로 센다 — 놓친 특보로 잡힌다
        self.assertEqual(accuracy._model_grade({"regions": {"suncheon": quiet}}, "suncheon", "heavy_rain_advisory"), "low")
        self.assertIsNone(accuracy._model_grade({"regions": {"suncheon": {"h": {}}}}, "suncheon", "heavy_rain_advisory"))

    def test_unavailable_warnings_are_not_recorded_as_none(self):
        now = kst(2026, 11, 12, 21)
        models = {"ecmwf_ifs025": series("ecmwf_ifs025", now, precip=const(0.0))}
        run = storage.build_history_run([assess_region(region(), models, RegionOfficial(available=False), CFG, now)], CFG, now, {})
        self.assertNotIn("a", run["regions"]["suncheon"])  # '특보 없음'과 '조회 실패'를 구별


class AccuracyTest(unittest.TestCase):
    """72시간(3시간 간격 25회) 가상 이력.

    순천: 29시간째 호우주의보 발효. 1~5회차 '주의', 6~8회차 '높음' → 12·24시간 전 모두 포착.
    목포: 0~3회차 '주의'였지만 특보 없음 → 오경보.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.t0 = kst(2026, 11, 1, 0)
        effective = self.t0 + timedelta(hours=29)
        for k in range(25):
            t = self.t0 + timedelta(hours=3 * k)
            grade = "low" if k == 0 else ("caution" if k <= 5 else ("high" if k <= 8 else "low"))
            active = [("heavy_rain", "advisory", effective)] if 10 <= k <= 12 else []
            regions = {
                "suncheon": region_record({"heavy_rain_advisory": grade} if grade != "low" else {}, active),
                "mokpo": region_record({"heavy_rain_advisory": "caution"} if k <= 3 else {}),
            }
            storage.append_history(self.dir, run_record(t, regions), t)
        self.now = self.t0 + timedelta(hours=72)
        self.result = accuracy.compute_accuracy(self.dir, CFG, self.now)

    def test_event_detected_once_with_official_time(self):
        self.assertEqual(self.result["events"], 1)
        event = self.result["recent_events"][0]
        self.assertEqual((event["region"], event["hazard"]), ("suncheon", "heavy_rain_advisory"))
        self.assertEqual(event["time"], iso(self.t0 + timedelta(hours=29)))
        self.assertEqual(event["grades"], {"12": "caution", "24": "caution"})

    def test_capture_rate(self):
        self.assertEqual(self.result["capture"]["12"], {"events": 1, "captured": 1, "rate": 1.0})
        self.assertEqual(self.result["capture"]["24"], {"events": 1, "captured": 1, "rate": 1.0})

    def test_false_alarm_ratio(self):
        # 검증 가능한 회차는 0~8회차(48시간 뒤까지 이력이 있는 것). 순천 8건 적중, 목포 4건 오경보
        self.assertEqual(self.result["false_alarm"]["caution_or_higher"], {"alerts": 12, "false": 4, "rate": 0.333})
        self.assertEqual(self.result["false_alarm"]["high"], {"alerts": 3, "false": 0, "rate": 0.0})

    def test_by_type_and_period(self):
        self.assertEqual(self.result["by_type"]["heavy_rain"]["events"], 1)
        self.assertEqual(self.result["period"]["runs"], 25)
        self.assertEqual(self.result["basis"], "model_grade")

    def test_missed_event_counts_against_capture(self):
        t = self.now + timedelta(hours=3)
        late = {"suncheon": region_record(), "mokpo": region_record(active=[("strong_wind", "advisory", None)])}
        storage.append_history(self.dir, run_record(t, late), t)
        result = accuracy.compute_accuracy(self.dir, CFG, t)
        self.assertEqual(result["events"], 2)
        self.assertEqual(result["capture"]["12"], {"events": 2, "captured": 1, "rate": 0.5})
        wind = next(e for e in result["recent_events"] if e["hazard"] == "strong_wind_advisory")
        self.assertEqual(wind["time"], iso(t))  # 발효 시각이 없으면 처음 확인된 회차 시각
        self.assertEqual(wind["grades"]["12"], "low")

    def test_upgrade_is_new_event_but_downgrade_is_not(self):
        d = Path(tempfile.mkdtemp())
        levels = [None, "advisory", "warning", "advisory", None]
        for k, level in enumerate(levels):
            t = self.t0 + timedelta(hours=3 * k)
            active = [("heavy_rain", level, None)] if level else []
            storage.append_history(d, run_record(t, {"suncheon": region_record(active=active)}), t)
        events = accuracy.find_events(accuracy.load_runs(d), CFG)
        self.assertEqual([e["hazard"] for e in events], ["heavy_rain_advisory", "heavy_rain_warning"])

    def test_unknown_previous_state_is_not_an_event(self):
        d = Path(tempfile.mkdtemp())
        t1, t2 = self.t0, self.t0 + timedelta(hours=3)
        storage.append_history(d, run_record(t1, {"suncheon": region_record(known=False)}), t1)  # 특보 조회 실패 회차
        storage.append_history(d, run_record(t2, {"suncheon": region_record(active=[("heavy_rain", "advisory", None)])}), t2)
        self.assertEqual(accuracy.find_events(accuracy.load_runs(d), CFG), [])

    def test_empty_history(self):
        result = accuracy.compute_accuracy(Path(tempfile.mkdtemp()), CFG, self.now)
        self.assertEqual(result["events"], 0)
        self.assertIsNone(result["capture"]["12"]["rate"])
        self.assertIsNone(result["false_alarm"]["caution_or_higher"]["rate"])


if __name__ == "__main__":
    unittest.main()
