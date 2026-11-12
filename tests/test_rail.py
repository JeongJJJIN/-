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
