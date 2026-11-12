import unittest
from datetime import timedelta

from teukbo import rail
from teukbo.sources.kma_warnings import WARN_STRESS, WARN_VAR

from .helpers import kst

CLASSES = [
    {"min": 1, "max": 899, "name": "KTX"},
    {"min": 1000, "max": 1199, "name": "ITX-새마을·마음"},
    {"min": 1200, "max": 1999, "name": "무궁화·누리로"},
    {"min": 9000, "max": 9999, "name": "SRT"},
]
RANK = {"주의보": 1, "경보": 2, "중대경보": 3}


def plan(trn_no, origin, dest, dep, arr, ymd="20261111"):
    return {"run_ymd": ymd, "trn_no": trn_no, "dptre_stn_nm": origin, "dptre_stn_cd": "0", "arvl_stn_nm": dest, "arvl_stn_cd": "0",
            "trn_plan_dptre_dt": f"2026-11-11 {dep}:00.0", "trn_plan_arvl_dt": f"2026-11-11 {arr}:00.0"}


def stop(trn_no, sn, name, code, kind, arr, dep, line="전라선", ymd="20261111"):
    """실제 응답과 같은 모양의 운행정보 한 행."""
    return {"run_ymd": ymd, "trn_no": trn_no, "trn_run_sn": str(sn), "stn_nm": name, "stn_cd": code, "mrnt_nm": line, "mrnt_cd": "04",
            "uppln_dn_se_cd": "D", "stop_se_cd": kind, "stop_se_nm": {"01": "시발", "05": "종착"}.get(kind, "여객승하차"),
            "trn_arvl_dt": f"2026-11-11 {arr}:00.0" if arr else None, "trn_dptre_dt": f"2026-11-11 {dep}:00.0" if dep else None}


def ktx501(arrive="08:20"):
    """용산 05:10 → 순천 → 여수엑스포 08:05 계획."""
    return [
        stop("00501", 1, "용산", "S1", "01", None, "05:10"),
        stop("00501", 9, "순천", "S2", "11", "07:52", "07:54"),
        stop("00501", 10, "여수엑스포", "S3", "05", arrive, None),
    ]


class BuildRunsTest(unittest.TestCase):
    def test_delay_from_plan_and_terminus_record(self):
        runs = rail.build_runs([plan("00501", "용산", "여수엑스포", "05:10", "08:05")], ktx501("08:20"))
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual((run.origin, run.dest, run.line, run.direction), ("용산", "여수엑스포", "전라선", "D"))
        self.assertEqual(run.arr_delay, 15)
        self.assertEqual(run.dep_delay, 0)
        self.assertEqual([s.name for s in run.stops], ["용산", "순천", "여수엑스포"])
        self.assertEqual(run.stops[1].time, kst(2026, 11, 11, 7, 52))
        self.assertEqual(run.number, 501)

    def test_rows_are_ordered_by_sequence_number(self):
        rows = list(reversed(ktx501()))
        run = rail.build_runs([plan("00501", "용산", "여수엑스포", "05:10", "08:05")], rows)[0]
        self.assertEqual([s.name for s in run.stops], ["용산", "순천", "여수엑스포"])  # '10' 이 '9' 뒤 (문자열 정렬이 아님)

    def test_early_arrival_is_negative_but_counts_as_zero_in_stats(self):
        run = rail.build_runs([plan("00501", "용산", "여수엑스포", "05:10", "08:05")], ktx501("08:02"))[0]
        self.assertEqual(run.arr_delay, -3)
        self.assertEqual(rail.delay_stats([run.arr_delay])["mean"], 0)

    def test_planned_but_no_record(self):
        run = rail.build_runs([plan("01441", "순천", "목포", "06:00", "08:30")], [])[0]
        self.assertFalse(run.has_record)
        self.assertIsNone(run.arr_delay)

    def test_no_terminus_record_means_no_delay(self):
        rows = ktx501()[:2]  # 종착 기록이 없는 열차(운행 중단 등)
        run = rail.build_runs([plan("00501", "용산", "여수엑스포", "05:10", "08:05")], rows)[0]
        self.assertTrue(run.has_record)
        self.assertIsNone(run.arr_delay)

    def test_record_without_plan(self):
        run = rail.build_runs([], ktx501())[0]
        self.assertEqual((run.origin, run.dest), ("용산", "여수엑스포"))
        self.assertIsNone(run.arr_delay)

    def test_absurd_difference_is_discarded(self):
        p = plan("00501", "용산", "여수엑스포", "05:10", "08:05")
        p["trn_plan_arvl_dt"] = "2026-11-09 08:05:00.0"  # 날짜가 어긋난 자료
        self.assertIsNone(rail.build_runs([p], ktx501())[0].arr_delay)

    def test_midnight_crossing(self):
        p = plan("09693", "S수서", "광주송정", "22:22", "23:59")
        p["trn_plan_arvl_dt"] = "2026-11-12 00:04:00.0"
        rows = [stop("09693", 1, "S수서", "A", "01", None, "22:22", "호남고속선"), stop("09693", 6, "광주송정", "B", "05", None, None, "호남고속선")]
        rows[1]["trn_arvl_dt"] = "2026-11-12 00:06:00.0"
        run = rail.build_runs([p], rows)[0]
        self.assertEqual(run.arr_delay, 2)

    def test_train_class_by_number(self):
        self.assertEqual(rail.train_class(501, CLASSES), "KTX")
        self.assertEqual(rail.train_class(1441, CLASSES), "무궁화·누리로")
        self.assertEqual(rail.train_class(9693, CLASSES), "SRT")
        self.assertEqual(rail.train_class(4851, CLASSES), "기타")
        self.assertEqual(rail.train_class(None, CLASSES), "기타")

    def test_parse_dt(self):
        self.assertEqual(rail.parse_dt("2026-11-11 05:13:00.0"), kst(2026, 11, 11, 5, 13))
        self.assertIsNone(rail.parse_dt(None))
        self.assertIsNone(rail.parse_dt(""))


def event(code, var, stress, command, tm, start=0, end=0, cancel="0"):
    """실제 getPwnCd 응답과 같은 모양."""
    return {"areaCode": code, "warnVar": var, "warnStress": stress, "command": command, "cancel": cancel, "tmFc": tm, "startTime": start, "endTime": end, "stnId": "108"}


def intervals(rows):
    events = [e for e in (rail.normalize_event(r, WARN_VAR, WARN_STRESS) for r in rows) if e]
    return rail.build_intervals(events)


class WarningIntervalTest(unittest.TestCase):
    def test_issue_then_release(self):
        got = intervals([
            event("L1051200", 2, 0, "1", 202611110500, start=202611110600),
            event("L1051200", 2, 0, "2", 202611111400, end=202611111400),
        ])
        self.assertEqual(got, [rail.Interval("L1051200", "호우", "주의보", kst(2026, 11, 11, 6), kst(2026, 11, 11, 14))])

    def test_still_in_effect_has_no_end(self):
        got = intervals([event("L1051200", 1, 0, "1", 202611110500, start=202611110600)])
        self.assertIsNone(got[0].end)

    def test_upgrade_closes_lower_level(self):
        got = intervals([
            event("L1051200", 2, 0, "1", 202611110500, start=202611110600),  # 호우주의보
            event("L1051200", 2, 1, "7", 202611110900, start=202611110930),  # 호우경보로 변경
            event("L1051200", 2, 0, "8", 202611110900, end=202611110930),  # 주의보 변경해제 (이미 닫힘)
            event("L1051200", 2, 1, "2", 202611111500, end=202611111500),
        ])
        self.assertEqual([(i.level_name, i.start.hour, i.end.hour if i.end else None) for i in got], [("주의보", 6, 9), ("경보", 9, 15)])

    def test_cancelled_and_unknown_rows_ignored(self):
        got = intervals([
            event("L1051200", 2, 0, "1", 202611110500, start=202611110600, cancel="1"),
            event("L1051200", 99, 0, "1", 202611110500, start=202611110600),
            event("L1051200", 2, 0, "3", 202611110700),  # 연장
        ])
        self.assertEqual(got, [])

    def test_release_without_known_issue_is_ignored(self):
        self.assertEqual(intervals([event("L1051200", 4, 0, "2", 202611110500, end=202611110500)]), [])

    def test_2026_types(self):
        got = intervals([event("L1072610", 12, 2, "7", 202608031000, start=202608031100), event("L1150100", 13, 0, "1", 202608081600, start=202608081700)])
        self.assertEqual({(i.type_name, i.level_name) for i in got}, {("폭염", "중대경보"), ("열대야", "주의보")})

    def test_timeline_lookup(self):
        timeline = rail.WarningTimeline(intervals([
            event("L1051200", 2, 0, "1", 202611110500, start=202611110600),
            event("L1051200", 2, 0, "2", 202611111400, end=202611111400),
        ]))
        self.assertEqual(len(timeline.active("L1051200", kst(2026, 11, 11, 7, 52))), 1)
        self.assertEqual(timeline.active("L1051200", kst(2026, 11, 11, 5, 59)), [])
        self.assertEqual(timeline.active("L1051200", kst(2026, 11, 11, 14, 0)), [])  # 해제 시각부터는 아님
        self.assertEqual(timeline.active("L1051100", kst(2026, 11, 11, 7, 52)), [])


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        self.station_zones = {"S2": ["L1051200"], "S3": ["L1051000"]}  # 순천역 → 순천, 여수엑스포역 → 여수
        self.timeline = rail.WarningTimeline(intervals([
            event("L1051200", 2, 0, "1", 202611110500, start=202611110600),  # 순천 호우주의보 06시~
            event("L1051000", 2, 1, "1", 202611110500, start=202611110700),  # 여수 호우경보 07시~
            event("L1051000", 1, 0, "1", 202611110500, start=202611112200),  # 여수 강풍주의보 22시~ (열차가 지난 뒤)
        ]))
        plans = [plan("00501", "용산", "여수엑스포", "05:10", "08:05"), plan("00001", "서울", "부산", "05:13", "07:50"), plan("01441", "순천", "목포", "06:00", "08:30")]
        rows = ktx501("08:20") + [
            stop("00001", 1, "서울", "S9", "01", None, "05:13", "경부선"),
            stop("00001", 7, "부산", "S8", "05", "07:52", None, "경부선"),
        ]
        self.runs = rail.build_runs(plans, rows)
        rail.attach_warnings(self.runs, self.timeline, self.station_zones, RANK)
        self.by_no = {r.trn_no: r for r in self.runs}

    def test_warnings_attached_by_station_and_time(self):
        self.assertEqual(self.by_no["00501"].warnings, {"호우": "경보"})  # 순천 주의보 + 여수 경보 → 최고 단계
        self.assertEqual(self.by_no["00001"].warnings, {})  # 특보 구역을 지나지 않음
        self.assertEqual(self.by_no["01441"].warnings, {})  # 운행 기록 없음

    def test_day_summary(self):
        day = rail.summarize_day("20261111", self.runs, CLASSES, kst(2026, 11, 12, 5, 30))
        self.assertEqual(day["date"], "2026-11-11")
        self.assertEqual(day["counts"], {"planned": 3, "recorded": 2, "measured": 2, "no_record": 1, "no_terminus": 0})
        self.assertEqual(day["all"]["n"], 2)
        self.assertEqual(day["by_warning"]["호우"], {"n": 1, "mean": 15.0, "median": 15.0, "max": 15, "late5": 1, "late10": 1, "late20": 0})
        self.assertEqual(day["clear"]["mean"], 2.0)
        self.assertEqual(day["by_line"]["전라선"]["max"], 15)
        self.assertEqual(day["by_class"]["KTX"]["n"], 2)
        row = dict(zip(day["fields"], next(t for t in day["trains"] if t[0] == "00501")))
        self.assertEqual((row["origin"], row["dest"], row["plan_arr"], row["arr_delay"], row["warnings"], row["class"]), ("용산", "여수엑스포", "08:05", 15, ["호우경보"], "KTX"))
        missing = dict(zip(day["fields"], next(t for t in day["trains"] if t[0] == "01441")))
        self.assertIsNone(missing["arr_delay"])

    def test_aggregate_compares_warned_and_clear(self):
        day1 = rail.summarize_day("20261111", self.runs, CLASSES, kst(2026, 11, 12, 5, 30))
        day2 = dict(day1, date="2026-11-12")
        agg = rail.aggregate([day2, day1])
        self.assertEqual(agg["period"], {"from": "2026-11-11", "to": "2026-11-12", "days": 2})
        self.assertEqual(agg["warned"]["n"], 2)
        self.assertEqual(agg["warned"]["mean"], 15.0)
        self.assertEqual(agg["clear"]["mean"], 2.0)
        self.assertEqual(agg["by_type"]["호우"]["n"], 2)
        self.assertEqual(agg["by_warning"]["호우경보"]["late10"], 2)
        self.assertEqual(agg["by_line"]["전라선"]["warn"]["n"], 2)
        self.assertEqual(agg["by_line"]["경부선"]["clear"]["n"], 2)
        self.assertEqual([d["date"] for d in agg["daily"]], ["2026-11-11", "2026-11-12"])
        self.assertEqual(agg["daily"][0]["warned"], 1)

    def test_empty(self):
        self.assertEqual(rail.aggregate([])["period"]["days"], 0)
        self.assertEqual(rail.delay_stats([]), {"n": 0})

    def test_window_days(self):
        self.assertEqual(rail.window_days(kst(2026, 11, 12, 5, 30), 3), ["20261111", "20261110", "20261109"])
        self.assertEqual(rail.window_days(kst(2026, 11, 12, 5, 30) + timedelta(days=0), 1), ["20261111"])


if __name__ == "__main__":
    unittest.main()


class AdjustedDifferenceTest(unittest.TestCase):
    """같은 노선끼리 비교한 지연 차이."""

    def setUp(self):
        from teukbo import rail

        self.rail = rail

    def test_line_mix_does_not_fake_an_effect(self):
        # 붐비는 노선(평소 10분)의 열차만 특보 구간을 지났다면, 전체 평균끼리는 차이가 나 보여도 같은 노선끼리는 0이다
        by_line = {
            "경부선": {"warn": [10] * 40, "clear": [10, 9, 11] * 40},
            "전라선": {"warn": [], "clear": [1, 2, 0] * 60},
        }
        out = self.rail.adjusted_difference(by_line)
        self.assertEqual((out["n"], out["lines"], out["diff"]), (40, 1, 0.0))
        self.assertEqual(out["verdict"], "no_difference")

    def test_clear_effect_is_reported_with_interval(self):
        by_line = {
            "전라선": {"warn": [12, 15, 18, 14, 16] * 8, "clear": [2, 3, 1, 4, 2] * 30},
            "경전선": {"warn": [9, 11, 10, 12] * 5, "clear": [3, 2, 4, 3] * 20},
        }
        out = self.rail.adjusted_difference(by_line)
        self.assertEqual((out["n"], out["lines"]), (60, 2))
        # 전라선 +12.6분(40편), 경전선 +7.5분(20편) → 가중 평균 10.9분
        self.assertAlmostEqual(out["diff"], 10.9, places=1)
        self.assertLess(out["low"], out["diff"])
        self.assertGreater(out["low"], 9.0)
        self.assertLess(out["high"], 12.5)
        self.assertEqual(out["verdict"], "later")

    def test_small_samples_are_not_judged(self):
        out = self.rail.adjusted_difference({"전라선": {"warn": [30, 40, 50], "clear": [1, 2, 3, 2, 1]}})
        self.assertEqual((out["n"], out["verdict"]), (3, "insufficient"))
        self.assertGreater(out["diff"], 30)

    def test_small_difference_is_not_called_an_effect(self):
        by_line = {"경부선": {"warn": [3.5] * 400, "clear": [3.0] * 4000}}  # 0.5분 차이는 뚜렷해도 운영상 의미가 없다
        self.assertEqual(self.rail.adjusted_difference(by_line)["verdict"], "no_difference")

    def test_earlier_and_empty(self):
        by_line = {"경부선": {"warn": [1, 0, 2, 1] * 20, "clear": [6, 7, 5, 8] * 50}}
        self.assertEqual(self.rail.adjusted_difference(by_line)["verdict"], "earlier")
        self.assertEqual(self.rail.adjusted_difference({}), {"n": 0, "lines": 0, "verdict": "insufficient"})
        self.assertEqual(self.rail.adjusted_difference({"경부선": {"warn": [5, 6], "clear": []}})["n"], 0)  # 비교할 열차가 없는 노선은 쓰지 않는다

    def test_early_arrivals_count_as_zero(self):
        out = self.rail.adjusted_difference({"경부선": {"warn": [-5, -3] * 20, "clear": [0, 0, 0] * 20}})
        self.assertEqual(out["diff"], 0.0)

    def test_aggregate_includes_adjusted_by_type(self):
        fields = ["trn_no", "line", "class", "origin", "dest", "plan_dep", "plan_arr", "dep_delay", "arr_delay", "warnings"]
        trains = [[f"{i:05d}", "전라선", "KTX", "용산", "여수엑스포", "05:00", "08:00", 0, 14 + i % 3, ["호우경보"]] for i in range(40)]
        trains += [[f"{100 + i:05d}", "전라선", "KTX", "용산", "여수엑스포", "05:00", "08:00", 0, 2 + i % 3, []] for i in range(80)]
        trains += [[f"{300 + i:05d}", "경부선", "KTX", "서울", "부산", "05:00", "08:00", 0, 9, []] for i in range(200)]
        summary = self.rail.aggregate([{"date": "2026-11-11", "fields": fields, "trains": trains, "all": {"n": 320, "mean": 6.0, "late10": 40, "max": 16}}])
        self.assertEqual(summary["adjusted"]["all"]["verdict"], "later")
        self.assertEqual(summary["adjusted"]["all"]["diff"], 12.0)
        self.assertEqual(set(summary["adjusted"]["by_type"]), {"호우"})
        self.assertEqual(summary["adjusted"]["by_type"]["호우"]["n"], 40)
        # 전체 평균끼리는 9.0 vs 7.0 가까이로 차이가 작아 보인다 → 같은 노선 비교가 필요한 이유
        self.assertLess(summary["warned"]["mean"] - summary["clear"]["mean"], 9.0)


class CubeTest(unittest.TestCase):
    """날짜 × 노선 × 열차 종류 × 특보 묶음(분석 화면의 바탕)과, 화면 쪽 계산의 일치."""

    FIELDS = ["trn_no", "line", "class", "origin", "dest", "plan_dep", "plan_arr", "dep_delay", "arr_delay", "warnings"]

    def days(self):
        def train(no, line, cls, delay, warnings):
            return [f"{no:05d}", line, cls, "서울", "부산", "05:00", "08:00", 0, delay, warnings]

        day1 = [train(i, "전라선", "KTX", 12 + i % 7, ["호우경보"]) for i in range(40)]
        day1 += [train(100 + i, "전라선", "KTX", i % 5, []) for i in range(60)]
        day1 += [train(300 + i, "경부선", "KTX", 8 + i % 4, []) for i in range(150)]
        day1 += [train(500 + i, "경부선", "무궁화·누리로", 3 + i % 9, ["강풍주의보"]) for i in range(35)]
        day1 += [train(600, "경전선", "무궁화·누리로", -4, ["호우경보", "강풍주의보"]), train(601, "경전선", "무궁화·누리로", None, [])]
        day2 = [train(i, "전라선", "KTX", i % 4, []) for i in range(80)] + [train(300 + i, "경부선", "KTX", 7 + i % 5, []) for i in range(150)] + [train(700, "중앙선", "KTX", 75, [])]
        return [
            {"date": "2026-11-11", "final": False, "fields": self.FIELDS, "trains": day1, "counts": {"planned": 290, "recorded": 287, "no_record": 3}, "all": {"n": 286, "mean": 7.0, "late10": 60, "max": 18}},
            {"date": "2026-11-10", "final": True, "fields": self.FIELDS, "trains": day2, "counts": {"planned": 231, "recorded": 231, "no_record": 0}, "all": {"n": 231, "mean": 6.0, "late10": 30, "max": 75}},
        ]

    def test_cube_structure(self):
        from teukbo import rail

        cube = rail.build_cube(self.days())
        self.assertEqual(cube["bins"], [0, 1, 5, 10, 20, 30, 60])
        self.assertEqual([d["date"] for d in cube["days"]], ["2026-11-10", "2026-11-11"])  # 날짜순
        self.assertEqual((cube["days"][1]["planned"], cube["days"][1]["no_record"], cube["days"][1]["final"]), (290, 3, False))
        self.assertEqual(set(cube["lines"]), {"전라선", "경부선", "경전선", "중앙선"})
        self.assertEqual(set(cube["warnings"]), {"호우경보", "강풍주의보"})
        total = sum(cell[3] for day in cube["days"] for cell in day["cells"])
        self.assertEqual(total, 40 + 60 + 150 + 35 + 1 + 80 + 150 + 1)  # 지연을 계산하지 못한 열차(601)는 빠진다
        for day in cube["days"]:
            for cell in day["cells"]:
                self.assertEqual(sum(cell[7:]), cell[3])  # 구간별 열차 수의 합 = 열차 수
                self.assertEqual(len(cell), 7 + 7)

        def find(line, cls, names):
            return next(c for c in cube["days"][1]["cells"] if cube["lines"][c[0]] == line and cube["classes"][c[1]] == cls and sorted(cube["warnings"][i] for i in c[2]) == sorted(names))

        rain = find("전라선", "KTX", ["호우경보"])
        delays = [12 + i % 7 for i in range(40)]
        self.assertEqual(rain[3:7], [40, sum(delays), sum(d * d for d in delays), 18])
        self.assertEqual(rain[7:], [0, 0, 0, 40, 0, 0, 0])  # 12~18분 → 모두 10~19분 구간
        both = find("경전선", "무궁화·누리로", ["호우경보", "강풍주의보"])
        self.assertEqual(both[3:8], [1, 0, 0, 0, 1])  # 조착(-4분)은 0분으로
        late = next(c for c in cube["days"][0]["cells"] if cube["lines"][c[0]] == "중앙선")
        self.assertEqual(late[7:], [0, 0, 0, 0, 0, 0, 1])  # 75분 → 60분 이상

    def test_browser_calculation_matches_server(self):
        """화면의 다시 세기(analysis-core.js)가 서버의 집계·판정과 같은 값을 낸다."""
        import json
        import shutil
        import subprocess
        from pathlib import Path

        from teukbo import rail

        node = shutil.which("node")
        if node is None:
            self.skipTest("node 가 없습니다")
        days = self.days()
        cube, summary = rail.build_cube(days), rail.aggregate(days)
        script = """
            const fs = require('fs');
            global.window = {};
            eval(fs.readFileSync(process.argv[1], 'utf8'));
            const C = window.TeukboAnalysisCore, cube = JSON.parse(process.argv[2]);
            const pick = (r) => ({ from: r.from, to: r.to, all: C.stats(r.all), warn: C.stats(r.warn), clear: C.stats(r.clear), adjusted: r.adjusted, planned: r.planned, noRecord: r.noRecord,
              names: Object.fromEntries([...r.byName].map(([k, v]) => [k, C.stats(v)])), lines: Object.fromEntries([...r.byLine].map(([k, v]) => [k, [v.warn.n, v.clear.n]])), days: r.days.map((d) => [d.date, d.all.n, d.warn.n]) });
            const f = (over) => ({ period: 'all', from: null, to: null, type: 'all', line: 'all', cls: 'all', ...over });
            console.log(JSON.stringify({ all: pick(C.compute(cube, f({}))), rain: pick(C.compute(cube, f({ type: '호우' }))), line: pick(C.compute(cube, f({ line: '경부선', cls: 'KTX' }))),
              one: pick(C.compute(cube, f({ period: 'custom', from: '2026-11-11', to: '2026-11-11' }))), week: pick(C.compute(cube, f({ period: '7' }))), swapped: pick(C.compute(cube, f({ period: 'custom', from: '2026-11-11', to: '2026-11-10' }))) }));
        """
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run([node, "-e", script, str(root / "web" / "analysis-core.js"), json.dumps(cube)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        everything = out["all"]
        # 전체 조건: 서버의 누적 통계와 같다
        self.assertEqual((everything["warn"]["n"], everything["clear"]["n"]), (summary["warned"]["n"], summary["clear"]["n"]))
        self.assertAlmostEqual(everything["warn"]["mean"], summary["warned"]["mean"], delta=0.05)
        self.assertAlmostEqual(everything["clear"]["mean"], summary["clear"]["mean"], delta=0.05)
        self.assertEqual(everything["warn"]["late10"], summary["warned"]["late10"])
        self.assertEqual(everything["clear"]["late5"], summary["clear"]["late5"])
        self.assertEqual(everything["adjusted"], summary["adjusted"]["all"])  # 같은 노선 대비 차이·95% 구간·판정까지
        self.assertEqual(out["rain"]["adjusted"], summary["adjusted"]["by_type"]["호우"])
        for name, stat in summary["by_warning"].items():
            self.assertEqual(everything["names"][name]["n"], stat["n"], name)
            self.assertAlmostEqual(everything["names"][name]["mean"], stat["mean"], delta=0.05)
        self.assertEqual((everything["planned"], everything["noRecord"]), (521, 3))
        self.assertEqual(everything["days"], [["2026-11-10", 231, 0], ["2026-11-11", 286, 76]])
        # 특보를 고르면: 다른 종류의 특보만 지난 열차(강풍 35편)는 어느 쪽에도 넣지 않는다
        self.assertEqual((out["rain"]["warn"]["n"], out["rain"]["clear"]["n"]), (41, summary["clear"]["n"]))
        self.assertEqual(sorted(out["rain"]["names"]), ["호우경보"])
        # 노선·열차 종류를 고르면 그것만
        self.assertEqual((out["line"]["all"]["n"], out["line"]["lines"]), (300, {"경부선": [0, 300]}))
        self.assertEqual(out["line"]["adjusted"]["verdict"], "insufficient")
        # 기간
        self.assertEqual((out["one"]["from"], out["one"]["to"], out["one"]["all"]["n"], out["one"]["planned"]), ("2026-11-11", "2026-11-11", 286, 290))
        self.assertEqual((out["week"]["from"], out["week"]["to"]), ("2026-11-10", "2026-11-11"))  # 자료가 있는 범위 안으로
        self.assertEqual((out["swapped"]["from"], out["swapped"]["to"]), ("2026-11-10", "2026-11-11"))  # 시작·끝을 거꾸로 넣어도


class UsualTrainsTest(unittest.TestCase):
    """평소 같은 요일 대비 운행 열차 수 — 운휴로 열차가 계획에서 빠진 날도 드러나야 한다."""

    def run_js(self, cube, expression):
        import json
        import shutil
        import subprocess
        from pathlib import Path

        node = shutil.which("node")
        if node is None:
            self.skipTest("node 가 없습니다")
        script = "const fs = require('fs'); global.window = {}; eval(fs.readFileSync(process.argv[1], 'utf8')); const C = window.TeukboAnalysisCore, cube = JSON.parse(process.argv[2]);" \
                 f"console.log(JSON.stringify({expression}));"
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run([node, "-e", script, str(root / "web" / "analysis-core.js"), json.dumps(cube)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def cube(self):
        def day(date, recorded, final=True, warned=0, planned=None):
            cells = [[0, 0, [], recorded - warned, 0, 0, 0, recorded - warned, 0, 0, 0, 0, 0, 0]]
            if warned:
                cells.append([0, 0, [0], warned, 0, 0, 0, warned, 0, 0, 0, 0, 0, 0])
            planned = recorded if planned is None else planned
            return {"date": date, "final": final, "planned": planned, "recorded": recorded, "no_record": planned - recorded, "cells": cells}

        # 2026-10-05 는 월요일. 월요일 다섯 번(한 번은 태풍으로 운휴 — 계획에서도 빠져 계획 대비로는 100%), 화요일 두 번, 확정 전 월요일 한 번
        return {"schema": 1, "bins": [0, 1, 5, 10, 20, 30, 60], "lines": ["경부선"], "classes": ["KTX"], "warnings": ["태풍경보"], "days": [
            day("2026-10-05", 100), day("2026-10-06", 90), day("2026-10-12", 102), day("2026-10-13", 90),
            day("2026-10-19", 60, warned=40), day("2026-10-26", 100), day("2026-11-02", 98, planned=100), day("2026-11-09", 40, final=False)]}

    def test_usual_is_same_weekday_median_of_final_days(self):
        out = self.run_js(self.cube(), "(() => { const u = C.usualRuns(cube); return ['2026-10-19', '2026-10-06', '2026-11-16'].map(u); })()")
        self.assertEqual(out, [100, None, 100])  # 월요일 [60, 98, 100, 100, 102] 의 중앙값 · 화요일은 이틀뿐이라 없음 · 확정 전(40)은 넣지 않음

    def test_hit_days_show_fewer_trains_even_when_plan_rate_is_full(self):
        out = self.run_js(self.cube(), "(() => { const r = C.compute(cube, { period: 'all', type: 'all', line: 'all', cls: 'all' });"
                                       " return { hit: r.hitDays, calm: r.calmDays, days: r.days.map((d) => [d.date, d.recorded, d.usual]) }; })()")
        self.assertEqual(out["hit"], {"days": 1, "planned": 60, "noRecord": 0, "ran": 60, "usual": 100})  # 계획 대비 100% 이지만 평소의 60%
        self.assertEqual(out["calm"], {"days": 7, "planned": 622, "noRecord": 2, "ran": 400, "usual": 400})  # 평소 값이 있는 날(확정된 월요일 네 번: 100+102+100+98)만 견준다
        self.assertEqual(out["days"][-1], ["2026-11-09", 40, None])  # 확정 전인 날은 평소와 견주지 않는다


class RailRunTest(unittest.TestCase):
    """실행부: 받아 온 자료로 날짜별 파일·이력 보관·누적 통계를 만든다(네트워크는 가짜)."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from teukbo import rail_run
        from teukbo.sources import kma_warnings, korail

        from .helpers import config

        self.rail_run, self.mock = rail_run, mock
        self.cfg = config()
        self.dir = Path(tempfile.mkdtemp())
        self.now = kst(2026, 11, 12, 5, 30)
        self.calls = {"info": [], "plan": [], "events": []}

        def info(_key, ymd, _opts=None):
            self.calls["info"].append(ymd)
            if ymd == "20261111":
                return ktx501("08:20")
            if ymd == "20261110":
                rows = ktx501("08:05")
                for r in rows:
                    r["run_ymd"] = "20261110"
                    for k in ("trn_arvl_dt", "trn_dptre_dt"):
                        if r[k]:
                            r[k] = r[k].replace("2026-11-11", "2026-11-10")
                return rows
            return []  # 그 밖의 날은 자료 없음

        def plan_rows(_key, ymd, _opts=None):
            self.calls["plan"].append(ymd)
            p = plan("00501", "용산", "여수엑스포", "05:10", "08:05", ymd=ymd)
            for k in ("trn_plan_dptre_dt", "trn_plan_arvl_dt"):
                p[k] = p[k].replace("2026-11-11", f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}")
            return [p]

        def events(_key, ymd, _opts=None):
            self.calls["events"].append(ymd)
            if ymd == "20261111":  # 순천 호우주의보 06시~14시
                return [event("L1051200", 2, 0, "1", 202611110500, start=202611110600), event("L1051200", 2, 0, "2", 202611111400, end=202611111400)]
            return []

        self.patches = [
            mock.patch.object(korail, "fetch_run_info", side_effect=info),
            mock.patch.object(korail, "fetch_run_plan", side_effect=plan_rows),
            mock.patch.object(kma_warnings, "fetch_pwn_events", side_effect=events),
            # 시험용 역 표: 순천역(S2) → 순천 구역
            mock.patch.object(rail_run, "load_station_zones", return_value=({"S2": ["L1051200"]}, {"여수엑스포": ["L1051000"]})),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.env = {"DATA_GO_KR_SERVICE_KEY": "k"}

    def _day(self, name):
        import json

        return json.loads((self.dir / "rail" / "days" / name).read_text(encoding="utf-8"))

    def test_processes_recent_days_and_writes_summary(self):
        import json

        self.assertEqual(self.rail_run.run(self.cfg, self.now, self.dir, self.env, max_days=3), 0)
        self.assertEqual(self.calls["info"], ["20261111", "20261110", "20261109"])  # 최근 날짜부터, 한 번에 3일
        files = sorted(p.name for p in (self.dir / "rail" / "days").glob("*.json"))
        self.assertEqual(files, ["2026-11-10.json", "2026-11-11.json"])  # 자료 없는 날은 파일을 만들지 않는다
        day = self._day("2026-11-11.json")
        row = dict(zip(day["fields"], day["trains"][0]))
        self.assertEqual((row["arr_delay"], row["warnings"], row["class"]), (15, ["호우주의보"], "KTX"))
        self.assertEqual(day["unmatched_stations"], {"용산": 1})  # 표에 없는 역은 이름을 남겨 보완할 수 있게
        self.assertEqual(dict(zip(day["fields"], self._day("2026-11-10.json")["trains"][0]))["warnings"], [])
        summary = json.loads((self.dir / "rail" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["period"], {"from": "2026-11-10", "to": "2026-11-11", "days": 2})
        self.assertEqual((summary["warned"]["n"], summary["warned"]["mean"], summary["clear"]["n"], summary["clear"]["mean"]), (1, 15.0, 1, 0.0))
        self.assertEqual(summary["latest_day"]["date"], "2026-11-11")
        self.assertEqual(summary["latest_day"]["top_delayed"][0]["trn_no"], "00501")
        cube = json.loads((self.dir / "rail" / "cube.json").read_text(encoding="utf-8"))  # 분석 화면이 쓰는 묶음
        self.assertEqual([d["date"] for d in cube["days"]], ["2026-11-10", "2026-11-11"])
        self.assertEqual(sum(c[3] for d in cube["days"] for c in d["cells"]), 2)

    def test_events_are_cached_and_past_days_not_refetched(self):
        import json

        self.rail_run.run(self.cfg, self.now, self.dir, self.env, max_days=1)
        first = len(self.calls["events"])
        self.assertEqual(first, 60)  # 지난 59일 + 오늘
        cache = json.loads((self.dir / "rail" / "events" / "2026-11.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cache["days"]["20261111"]), 2)
        self.calls["events"].clear()
        self.rail_run.run(self.cfg, self.now, self.dir, self.env, max_days=1)
        self.assertEqual(self.calls["events"], ["20261112"])  # 아직 끝나지 않은 오늘만 다시 받는다

    def test_second_run_continues_with_older_days(self):
        self.rail_run.run(self.cfg, self.now, self.dir, self.env, max_days=1)
        self.calls["info"].clear()
        self.rail_run.run(self.cfg, self.now, self.dir, self.env, max_days=2)
        # 어제(11일)는 아직 확정 전이라 다시 확인하고, 그와 별도로 없는 날을 2일 더 채운다
        self.assertEqual(self.calls["info"], ["20261111", "20261110", "20261109"])

    def test_recent_day_is_rechecked_until_final(self):
        from teukbo.sources import korail

        self.rail_run.run(self.cfg, self.now, self.dir, self.env, max_days=2)
        self.assertFalse(self._day("2026-11-11.json")["final"])  # 어제 → 확정 전
        self.assertTrue(self._day("2026-11-10.json")["final"])  # 그저께 → 확정
        built = self._day("2026-11-11.json")["built_at"]

        # 같은 날 다시 실행: 내용이 같으면 파일을 건드리지 않는다(쓸데없는 커밋 방지)
        self.calls["info"].clear()
        self.rail_run.run(self.cfg, kst(2026, 11, 12, 8, 30), self.dir, self.env, max_days=0)
        self.assertEqual(self.calls["info"], ["20261111"])
        self.assertEqual(self._day("2026-11-11.json")["built_at"], built)

        # 뒤늦게 기록이 더 올라오면(도착 시각이 바뀜) 고쳐 쓴다
        with self.mock.patch.object(korail, "fetch_run_info", side_effect=lambda _k, ymd, _o=None: ktx501("08:35") if ymd == "20261111" else []):
            self.rail_run.run(self.cfg, kst(2026, 11, 12, 11, 30), self.dir, self.env, max_days=0)
            day = self._day("2026-11-11.json")
            self.assertEqual(dict(zip(day["fields"], day["trains"][0]))["arr_delay"], 30)
            self.assertFalse(day["final"])
            # 하루가 더 지나면 한 번 더 확인하고 확정한다
            self.rail_run.run(self.cfg, kst(2026, 11, 13, 5, 30), self.dir, self.env, max_days=0)
            self.assertTrue(self._day("2026-11-11.json")["final"])
        # 확정된 날은 다시 받지 않는다
        self.calls["info"].clear()
        self.rail_run.run(self.cfg, kst(2026, 11, 13, 8, 30), self.dir, self.env, max_days=0)
        self.assertNotIn("20261111", self.calls["info"])

    def test_day_without_plan_is_not_saved(self):
        from teukbo.sources import korail

        with self.mock.patch.object(korail, "fetch_run_plan", return_value=[]):
            self.rail_run.run(self.cfg, self.now, self.dir, self.env, max_days=1)
        self.assertFalse((self.dir / "rail" / "days" / "2026-11-11.json").exists())  # 올라오는 중인 자료로 파일을 만들지 않는다

    def test_time_budget_leaves_the_rest_for_next_run(self):
        from teukbo import rail_run as real

        ticks = iter([0.0, 10.0, 500.0, 500.0, 500.0])
        with self.mock.patch.object(real.time, "monotonic", side_effect=lambda: next(ticks)):
            self.rail_run.run(self.cfg, self.now, self.dir, self.env, max_days=5)
        self.assertEqual(self.calls["info"], ["20261111"])  # 한 날만 처리하고 멈춘다
        self.assertTrue((self.dir / "rail" / "summary.json").is_file())  # 처리한 만큼은 요약에 반영

    def test_no_key_does_nothing(self):
        self.assertEqual(self.rail_run.run(self.cfg, self.now, self.dir, {}, max_days=3), 0)
        self.assertFalse((self.dir / "rail").exists())
        self.assertEqual(self.calls["info"], [])

    def test_unsubscribed_api_stops_quietly(self):
        from teukbo.http import FetchError
        from teukbo.sources import korail

        with self.mock.patch.object(korail, "fetch_run_info", side_effect=FetchError("코레일: SERVICE_KEY_IS_NOT_REGISTERED_ERROR")) as denied:
            self.assertEqual(self.rail_run.run(self.cfg, self.now, self.dir, self.env, max_days=5), 0)
        self.assertEqual(denied.call_count, 1)  # 신청이 안 된 상태면 한 번만 시도하고 멈춘다
        self.assertFalse((self.dir / "rail" / "days").exists())

    def test_real_station_table_loads(self):
        for p in self.patches:
            pass
        from teukbo import rail_run as real

        self.patches[3].stop()
        try:
            by_code, by_name = real.load_station_zones()
        finally:
            self.patches[3].start()
        self.assertEqual(by_code["3900271"], ["L1051200"])  # 순천역 → 순천
        self.assertEqual(by_code["3900714"], ["L1050900"])  # 벌교역 → 보성
        self.assertEqual(by_name["강진"], ["L1051400"])  # 역코드표에 없는 새 역은 이름으로
        self.assertGreater(len(by_code), 400)
        # 이름이 겹치거나 표기가 다른 역 (실제 운행 기록에서 확인한 이름들)
        self.assertEqual(by_code["3900896"], ["L1082900"])  # KTX 울산역(울산서부) — 옛 울산역(지금의 태화강)과 다른 코드
        self.assertEqual(by_name["울산"], ["L1082900"])
        self.assertEqual(by_name["태화강"], ["L1082800"])
        self.assertEqual(by_name["김천구미"], by_name["김천(구미)"])
        self.assertNotEqual(by_name["김천구미"], None)
        self.assertEqual(by_name["진부"], ["L1022420"])
        self.assertEqual(by_name["신창"], ["L1030400"])
        self.assertEqual(by_name["지탄"], ["L1040600"])
        self.assertEqual(by_code["3900343"], ["L1031500"])  # 장항선 판교(서천)
        self.assertEqual(by_name["판교(경기)"], ["L1012000"])  # 경강선 판교(성남)

    def test_station_name_variants_are_matched(self):
        from teukbo import rail

        self.assertEqual(rail.plain_name("김천(구미)"), "김천구미")
        self.assertEqual(rail.plain_name("신창 (순천향대)"), "신창순천향대")
        run = rail.TrainRun(ymd="20261111", trn_no="00101", line="경부선", direction="D", origin="서울", dest="부산")
        run.stops = [rail.Stop("X1", "김천 구미", rail.STOP_ORIGIN, None, kst(2026, 11, 11, 7, 0))]
        timeline = rail.WarningTimeline([rail.Interval("L1072610", "강풍", "주의보", kst(2026, 11, 11, 6, 0), None)])
        unmatched = rail.attach_warnings([run], timeline, {}, {"주의보": 1, "경보": 2}, {"김천구미": ["L1072610"]})
        self.assertEqual(run.warnings, {"강풍": "주의보"})
        self.assertFalse(unmatched)
