import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from teukbo import run, site
from teukbo.config import ROOT
from teukbo.http import FetchError
from teukbo.sources import kma_forecast, kma_warnings, open_meteo

from .helpers import config, kst, small_config

FULL = config()  # 전국
CFG = small_config()  # 수집·계산 전체를 돌리는 테스트는 전남 23곳으로(빠르게)


class DemoPipelineTest(unittest.TestCase):
    """가상 자료로 수집 이후의 전 과정을 돌려 결과 파일의 구조를 확인한다."""

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.now = kst(2026, 11, 12, 21, 20)
        cls.code = run.run_demo(CFG, cls.now, cls.dir, days=1)
        cls.latest = json.loads((cls.dir / "latest.json").read_text(encoding="utf-8"))

    def test_exit_code_and_files(self):
        self.assertEqual(self.code, 0)
        self.assertTrue((self.dir / "accuracy.json").is_file())
        self.assertEqual({p.stem for p in (self.dir / "detail").glob("*.json")}, {r.id for r in CFG.regions})
        self.assertTrue(list((self.dir / "history").glob("*.json")))

    def test_latest_structure(self):
        latest = self.latest
        self.assertTrue(latest["demo"])
        self.assertEqual(latest["notice"], "참고용입니다. 공식 특보·예비특보가 우선합니다.")
        self.assertEqual(latest["generated_at"], "2026-11-12T21:20+09:00")
        self.assertEqual(latest["horizon_hours"], 48)
        self.assertEqual(latest["live"], {"url": "data/live.json", "stale_after_minutes": 30, "poll_seconds": 120})
        self.assertEqual([m["id"] for m in latest["models"]], [m.id for m in CFG.settings.models])
        self.assertEqual([h["id"] for h in latest["hazards"]], [h.id for h in CFG.thresholds.hazards])
        self.assertEqual([r["id"] for r in latest["regions"]], [r.id for r in CFG.regions])
        for r, region in zip(latest["regions"], CFG.regions):
            self.assertEqual(r["zones"], list(region.zone_codes))  # 지도에서 지역 ↔ 특보구역을 잇는다
            self.assertEqual(set(r["hazards"]), {h.id for h in CFG.thresholds.hazards})
            for h in r["hazards"].values():
                self.assertIn(h["g"], ("low", "caution", "high", "active", "unknown"))

    def test_scenario_is_visible(self):
        suncheon = next(r for r in self.latest["regions"] if r["id"] == "suncheon")
        rain = suncheon["hazards"]["heavy_rain_advisory"]
        self.assertEqual(rain["g"], "high")
        self.assertGreaterEqual(rain["p"], 60)
        self.assertIn("from", rain)
        self.assertEqual(rain["official"], "preliminary")
        self.assertEqual(suncheon["official"]["preliminary"][0]["name"], "호우 예비특보")
        jindo = next(r for r in self.latest["regions"] if r["id"] == "jindo")
        self.assertEqual(jindo["hazards"]["heavy_rain_advisory"]["g"], "low")

    def test_detail_structure(self):
        detail = json.loads((self.dir / "detail" / "suncheon.json").read_text(encoding="utf-8"))
        self.assertEqual(len(detail["times"]), 49)
        self.assertEqual(detail["times"][0], "2026-11-12T21:00+09:00")
        for key in ("i:rain_3h", "i:rain_12h", "i:wind", "i:gust", "i:snow_24h", "v:temp", "v:apparent"):
            self.assertIn(key, detail["series"], key)
            for values in detail["series"][key].values():
                self.assertEqual(len(values), 49)
        self.assertNotIn("jma_seamless", detail["series"]["i:gust"])  # 돌풍 자료가 없는 모델
        rows = detail["hazards"]["heavy_rain_advisory"]["criteria"]
        self.assertEqual([(r["indicator"], r["value"]) for r in rows], [("rain_3h", 60), ("rain_12h", 110)])
        self.assertEqual(set(rows[0]["models"]), set(m.id for m in CFG.settings.models))
        self.assertEqual(detail["indicators"]["apparent_max"]["chart"], "v:apparent")
        self.assertEqual(detail["indicators"]["morning_drop"]["chart"], "v:temp")
        self.assertNotIn("typhoon_advisory", detail["hazards"])

    def test_national_warnings_file(self):
        doc = json.loads((self.dir / "warnings.json").read_text(encoding="utf-8"))
        self.assertTrue(doc["available"])
        self.assertEqual(doc["unresolved"], [])
        pre = {(w["code"], w["type"]) for w in doc["preliminary"]}
        self.assertIn(("L1051200", "heavy_rain"), pre)  # 순천 호우 예비특보
        self.assertEqual(len(pre), len(doc["preliminary"]))  # 구역당 한 건(벌교·보성은 같은 구역)
        history = json.loads(sorted((self.dir / "history").glob("*.json"))[-1].read_text(encoding="utf-8"))
        self.assertIn("L1051200", history["runs"][-1]["nat"]["p"]["호우"])
        # 전국 지도 점검용 가상 특보(계산 대상 지역 밖)
        active = {(w["code"], w["name"]) for w in doc["active"]}
        self.assertIn(("L1082400", "호우경보"), active)  # 남해
        self.assertIn(("L1082500", "강풍주의보"), active)  # 부산동부

    def test_live_file_matches_latest(self):
        """데모의 실시간 자료는 같은 특보로 만들었으므로, 지역별 공식 발표가 latest.json 과 같아야 한다."""
        doc = json.loads((self.dir / "live.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["schema"], 1)
        self.assertTrue(doc["demo"])
        for name in ("warnings", "obs", "fcst"):
            self.assertTrue(doc[name]["available"], name)
            self.assertEqual(doc[name]["checked_at"], "2026-11-12T21:20+09:00", name)
        for r in self.latest["regions"]:
            entry = doc["regions"][r["id"]]
            self.assertEqual(entry["official"], r["official"], r["name"])
            expected = {hid: [h["official"], h.get("official_level"), h.get("official_from")] for hid, h in r["hazards"].items() if h.get("official")}
            self.assertEqual(entry["hz"], expected, r["name"])
            self.assertEqual(len(entry["fcst"]), 6)
            self.assertEqual(set(entry["now"]), {"temp", "rain", "wind", "wdir", "rh", "pty"})

    def test_browser_overlay_reproduces_server_grades(self):
        """화면의 덮어쓰기 계산(live.js applyToLatest)이 서버의 계산과 같은 결과를 내는지 node 로 확인한다."""
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skipTest("node 가 없습니다")
        script = """
            const fs = require('fs');
            global.window = {}; global.document = { addEventListener() {} };
            const [, liveJs, latestPath, livePath] = process.argv;  // node -e 에서는 인수가 argv[1] 부터 온다
            eval(fs.readFileSync(liveJs, 'utf8'));
            const L = window.TeukboLive, base = JSON.parse(fs.readFileSync(latestPath)), live = JSON.parse(fs.readFileSync(livePath));
            const before = JSON.stringify(base);
            const now = new Date(live.warnings.checked_at).getTime();
            Date.now = () => now + 60000;
            const same = L.applyToLatest(base, live, 30);
            // 실시간 특보가 달라진 경우: 순천에 호우경보 발효, 나머지는 해제
            const changed = JSON.parse(JSON.stringify(live));
            for (const id of Object.keys(changed.regions)) { changed.regions[id].official = { available: true, active: [], preliminary: [] }; changed.regions[id].hz = {}; }
            changed.regions.suncheon.hz = { heavy_rain_advisory: ['active', 'warning', '2026-11-12T21:00+09:00'], heavy_rain_warning: ['active', 'warning', '2026-11-12T21:00+09:00'] };
            const other = L.applyToLatest(base, changed, 30);
            Date.now = () => now + 31 * 60000;
            const stale = L.applyToLatest(base, changed, 30);
            const pick = (doc) => doc.regions.map((r) => [r.id, r.top, Object.fromEntries(Object.entries(r.hazards).map(([k, v]) => [k, [v.g, v.mg, v.official || null, v.official_level || null, v.official_from || null]]))]);
            console.log(JSON.stringify({ same: pick(same), base: pick(base), other: pick(other), staleIsBase: stale === base, untouched: JSON.stringify(base) === before,
              checked: same.warnings_checked_at, direction: [L.windDir(0), L.windDir(200), L.windDir(350)], sky: [L.skyText({ sky: 3, pty: 0 }), L.skyText({ sky: 4, pty: 1 })] }));
        """
        result = subprocess.run([node, "-e", script, str(ROOT / "web" / "live.js"), str(self.dir / "latest.json"), str(self.dir / "live.json")], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["same"], out["base"])  # 같은 특보 → 서버가 계산한 등급·대표 특보와 완전히 같다
        self.assertTrue(out["staleIsBase"])  # 오래된 실시간 자료는 쓰지 않는다
        self.assertTrue(out["untouched"])  # 받아 온 원본은 바꾸지 않는다
        self.assertEqual(out["checked"], "2026-11-12T21:20+09:00")
        self.assertEqual(out["direction"], ["북", "남남서", "북"])
        self.assertEqual(out["sky"], ["구름많음", "비"])
        other = {rid: (top, hz) for rid, top, hz in out["other"]}
        self.assertEqual(other["suncheon"][1]["heavy_rain_warning"][0], "active")
        self.assertEqual(other["suncheon"][0]["grade"], "active")
        self.assertEqual(other["suncheon"][1]["heavy_rain_warning"][2:], ["active", "warning", "2026-11-12T21:00+09:00"])
        for rid, (top, hz) in other.items():
            for hid, (g, mg, official, _level, _from) in hz.items():
                if rid == "suncheon" and hid.startswith("heavy_rain"):
                    continue
                self.assertIsNone(official, f"{rid} {hid}")
                self.assertEqual(g, mg, f"{rid} {hid}")  # 공식 발표가 없으면 계산 등급 그대로

    def test_rail_summary(self):
        doc = json.loads((self.dir / "rail" / "summary.json").read_text(encoding="utf-8"))
        self.assertTrue(doc["demo"])
        self.assertEqual(doc["period"], {"from": "2026-10-13", "to": "2026-11-11", "days": 30})
        self.assertEqual(len(doc["daily"]), 14)
        self.assertEqual(doc["latest_day"]["date"], "2026-11-11")
        self.assertGreater(doc["warned"]["mean"], doc["clear"]["mean"])  # 시나리오: 특보 구간을 지난 열차가 더 늦다
        self.assertEqual(set(doc["by_warning"]), {"호우경보", "호우주의보", "강풍주의보", "대설주의보"})
        matched = doc["matched"]
        self.assertEqual((matched["all"]["verdict"], matched["all"]["days"]), ("later", 10))  # 특보일 10일 → 판정
        self.assertEqual((matched["by_type"]["호우"]["verdict"], matched["by_type"]["호우"]["reason"]), ("hold", "few_days"))  # 종류 하나로는 날 수가 모자라 판단 보류
        self.assertGreater(doc["daily"][-1]["warned"], 0)
        top = doc["latest_day"]["top_delayed"][0]
        self.assertEqual(set(top), {"trn_no", "line", "class", "origin", "dest", "plan_arr", "arr_delay", "warnings"})
        for key in ("late5", "late10", "late20"):
            self.assertIn(key, doc["clear"])

    def test_site_builds_cube_when_missing(self):
        """묶음 파일이 없어도 날짜별 기록이 있으면 배포 때 만들어 넣는다."""
        import shutil

        copy = Path(tempfile.mkdtemp()) / "data"
        shutil.copytree(self.dir, copy)
        expected = json.loads((copy / "rail" / "cube.json").read_text(encoding="utf-8"))
        (copy / "rail" / "cube.json").unlink()
        out = Path(tempfile.mkdtemp()) / "_site"
        site.build_site(copy, out)
        built = json.loads((out / "data" / "rail-cube.json").read_text(encoding="utf-8"))
        self.assertEqual(built["days"], expected["days"])
        self.assertEqual((built["lines"], built["warnings"]), (expected["lines"], expected["warnings"]))

    def test_rail_cube_and_days(self):
        """분석 화면이 쓰는 묶음과 날짜별 목록(데모)."""
        cube = json.loads((self.dir / "rail" / "cube.json").read_text(encoding="utf-8"))
        self.assertTrue(cube["demo"])
        self.assertEqual(len(cube["days"]), 30)
        self.assertEqual(len(list((self.dir / "rail" / "days").glob("*.json"))), 30)
        self.assertEqual([d["final"] for d in cube["days"][-2:]], [True, False])  # 어제 것은 아직 잠정
        short = [d for d in cube["days"] if d["no_record"]]
        self.assertEqual(len(short), 1)  # 운행 기록이 없는 열차가 있는 날 → 운행률 100% 미만
        self.assertLess(short[0]["no_record"], short[0]["planned"] // 10)
        names = {cube["warnings"][i] for d in cube["days"] for c in d["cells"] for i in c[2]}
        self.assertEqual(names, {"호우경보", "호우주의보", "강풍주의보", "대설주의보"})
        summary = json.loads((self.dir / "rail" / "summary.json").read_text(encoding="utf-8"))
        total = sum(c[3] for d in cube["days"] for c in d["cells"])
        self.assertEqual(total, summary["warned"]["n"] + summary["clear"]["n"])  # 묶음과 누적 통계가 같은 열차를 센다

    def test_site_build(self):
        out = Path(tempfile.mkdtemp()) / "_site"
        build = site.build_site(self.dir, out)
        self.assertEqual(len(build), 10)
        for name in ("index.html", "app.js", "style.css", "sw.js", "manifest.webmanifest", "data/latest.json", "data/accuracy.json", "data/warnings.json", "data/detail/suncheon.json",
                     "rail.html", "rail.js", "live.js", "analysis.html", "analysis.js", "analysis-core.js", "ui.js",
                     "geo/zones.json", "geo/rail.json", "geo/stations.json", "geo/lines.json", "data/rail.json", "data/live.json",
                     "data/rail-cube.json", "data/rail-days/2026-11-11.json"):
            self.assertTrue((out / name).is_file(), name)
        self.assertFalse((out / "data" / "rail").exists())  # 날짜별 원자료는 배포하지 않는다
        self.assertIn(build, (out / "sw.js").read_text(encoding="utf-8"))
        for path in out.rglob("*"):
            if path.is_file() and path.suffix in (".html", ".js", ".css"):
                self.assertNotIn(site.BUILD_TOKEN, path.read_text(encoding="utf-8"), path.name)

    def test_site_build_requires_data(self):
        with self.assertRaises(FileNotFoundError):
            site.build_site(Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "_site")


class RealRunFailureModesTest(unittest.TestCase):
    """출처가 실패할 때의 동작 (네트워크 호출은 모두 가짜)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.now = kst(2026, 11, 12, 21, 20)

    def test_no_forecast_at_all_writes_nothing(self):
        with mock.patch.object(open_meteo, "get_json", side_effect=FetchError("HTTP 429")):
            code = run.run_real(CFG, self.now, self.dir, {})
        self.assertEqual(code, run.EXIT_NO_FORECAST)
        self.assertFalse((self.dir / "latest.json").exists())
        self.assertFalse((self.dir / "history").exists())

    def test_open_meteo_only_without_keys(self):
        from teukbo import demo

        forecasts, _ = demo.generate(CFG, self.now, [self.now])
        om = {m.id: forecasts[m.id] for m in CFG.settings.models if m.source == "open_meteo"}
        with mock.patch.object(open_meteo, "fetch_all", return_value=(om, {})):
            code = run.run_real(CFG, self.now, self.dir, {})
        self.assertEqual(code, 0)
        latest = json.loads((self.dir / "latest.json").read_text(encoding="utf-8"))
        self.assertFalse(latest["demo"])
        self.assertTrue(latest["sources"]["open_meteo"]["ok"])
        self.assertEqual(latest["sources"]["kma_warnings"]["configured"], False)
        suncheon = latest["regions"][0]
        self.assertFalse(suncheon["official"]["available"])  # 특보 현황은 '확인 불가'
        self.assertEqual(suncheon["hazards"]["typhoon_advisory"]["g"], "unknown")
        self.assertNotIn("kma_dfs", suncheon["models"])
        history = json.loads(next((self.dir / "history").glob("*.json")).read_text(encoding="utf-8"))
        self.assertNotIn("a", history["runs"][0]["regions"]["suncheon"])
        self.assertNotIn("nat", history["runs"][0])  # 특보 조회 실패 회차에는 전국 특보도 기록하지 않는다
        self.assertFalse(json.loads((self.dir / "warnings.json").read_text(encoding="utf-8"))["available"])
        self.assertTrue((self.dir / "accuracy.json").is_file())

    def test_keys_are_read_from_environment(self):
        seen = {}

        def fake_forecast(regions, model_id, now, *, data_go_kr_key, apihub_key, **_kw):
            seen["forecast"] = (data_go_kr_key, apihub_key)
            return {}, {"ok": False, "configured": True, "provider": None, "error": "x"}

        def fake_warnings(regions, thresholds, now, *, data_go_kr_key, apihub_key, **_kw):
            seen["warnings"] = (data_go_kr_key, apihub_key)
            return {}, {"ok": False, "configured": True, "provider": None, "error": "x"}, []

        env = {"DATA_GO_KR_SERVICE_KEY": "abc%2Bdef%3D%3D", "KMA_APIHUB_KEY": " hubkey "}
        with mock.patch.object(open_meteo, "fetch_all", return_value=({}, {})), mock.patch.object(
            kma_forecast, "fetch", side_effect=fake_forecast
        ), mock.patch.object(kma_warnings, "fetch", side_effect=fake_warnings):
            run.collect(CFG, self.now, env)
        self.assertEqual(seen["forecast"], ("abc+def==", "hubkey"))
        self.assertEqual(seen["warnings"], ("abc+def==", "hubkey"))


class SourceCheckTest(unittest.TestCase):
    def test_reports_failure_when_nothing_is_reachable(self):
        import contextlib
        import io

        from teukbo import check

        out = io.StringIO()
        with mock.patch.object(open_meteo, "get_json", side_effect=FetchError("HTTP 429")), mock.patch.dict(
            "os.environ", {"DATA_GO_KR_SERVICE_KEY": "", "KMA_APIHUB_KEY": ""}
        ), contextlib.redirect_stdout(out):
            code = check.main()
        self.assertEqual(code, 1)
        self.assertIn("인증키 미설정", out.getvalue())

    def test_reports_models_when_reachable(self):
        import contextlib
        import io

        from teukbo import check, demo

        now = kst(2026, 11, 12, 21)
        forecasts, _ = demo.generate(CFG, now, [now])

        sizes = []

        def fake(regions, model, *_a, **_kw):
            sizes.append(len(regions))
            first = next(iter(forecasts[model.id].values()))
            return {r.id: forecasts[model.id].get(r.id, first) for r in regions}

        from teukbo import backtest

        def past(regions, model, *_a, **_kw):  # 과거 예보(검증용)도 실제로 받아 보는지
            first = next(iter(forecasts[model.id].values()))
            return {r.id: {1: first, 2: first} for r in regions}

        out = io.StringIO()
        with mock.patch.object(open_meteo, "fetch_model", side_effect=fake), mock.patch.object(backtest, "fetch_archive", side_effect=past), mock.patch.dict(
            "os.environ", {"DATA_GO_KR_SERVICE_KEY": "", "KMA_APIHUB_KEY": ""}
        ), contextlib.redirect_stdout(out):
            code = check.main()
        self.assertIn("정상  ecmwf_ifs025 2일 전 예측", out.getvalue())
        self.assertEqual(code, 0)
        self.assertIn("정상  ecmwf_ifs025", out.getvalue())
        self.assertIn("없는 변수: gust", out.getvalue())  # JMA 는 돌풍 자료가 없다
        self.assertIn("정상  묶음 요청", out.getvalue())  # 전국 수집에 쓰는 여러 지점 묶음 요청도 한 번 확인한다
        self.assertEqual(max(sizes), FULL.settings.raw["fetch"]["open_meteo_chunk"])


class LocalModeTest(unittest.TestCase):
    """내 컴퓨터용 실행기(teukbo.local)."""

    def setUp(self):
        from teukbo import local

        self.local = local
        self.web = Path("/srv/web")
        self.data = Path("/srv/data")

    def test_paths_served(self):
        r = lambda p: self.local.resolve_path(p, self.web, self.data)  # noqa: E731
        self.assertEqual(r("/"), self.web / "index.html")
        self.assertEqual(r("/app.js?v=__BUILD__"), self.web / "app.js")
        self.assertEqual(r("/icons/icon-192.png"), self.web / "icons" / "icon-192.png")
        self.assertEqual(r("/data/latest.json"), self.data / "latest.json")
        self.assertEqual(r("/data/accuracy.json"), self.data / "accuracy.json")
        self.assertEqual(r("/data/warnings.json"), self.data / "warnings.json")
        self.assertEqual(r("/data/detail/suncheon.json"), self.data / "detail" / "suncheon.json")
        self.assertEqual(r("/data/rail.json"), self.data / "rail" / "summary.json")
        self.assertEqual(r("/data/live.json"), self.data / "live.json")
        self.assertEqual(r("/data/rail-cube.json"), self.data / "rail" / "cube.json")
        self.assertEqual(r("/data/rail-days/2026-11-11.json"), self.data / "rail" / "days" / "2026-11-11.json")
        self.assertEqual(r("/rail.html"), self.web / "rail.html")
        self.assertEqual(r("/geo/zones.json"), self.web / "geo" / "zones.json")

    def test_paths_refused(self):
        r = lambda p: self.local.resolve_path(p, self.web, self.data)  # noqa: E731
        for path in (
            "/data/history/2026-11-12.json",  # 이력은 화면에 필요 없다
            "/data/rail/days/2026-11-11.json",
            "/data/rail/summary.json",
            "/data/rail/cube.json",  # 화면에는 rail-cube.json 이라는 이름으로만
            "/data/rail-days/../summary.json",
            "/data/rail-days/x.txt",
            "/data/rail-days/a/2026-11-11.json",
            "/data/../local_keys.txt",
            "/../local_keys.txt",
            "/%2e%2e/local_keys.txt",
            "/data/detail/../../config/settings.json",
            "/data/detail/x.txt",
            "/C:/Windows/win.ini",
            "/..\\local_keys.txt",
        ):
            self.assertIsNone(r(path), path)

    def test_key_file_round_trip(self):
        path = Path(tempfile.mkdtemp()) / "local_keys.txt"
        self.assertEqual(self.local.read_keys(path), {})
        self.local.write_keys(path, {"DATA_GO_KR_SERVICE_KEY": "abc+def==", "KMA_APIHUB_KEY": ""})
        self.assertEqual(self.local.read_keys(path), {"DATA_GO_KR_SERVICE_KEY": "abc+def==", "KMA_APIHUB_KEY": "", "GITHUB_TRIGGER_TOKEN": ""})
        self.local.write_keys(path, {"DATA_GO_KR_SERVICE_KEY": "k", "GITHUB_TRIGGER_TOKEN": "github_pat_x"})
        self.assertEqual(self.local.read_keys(path)["GITHUB_TRIGGER_TOKEN"], "github_pat_x")  # (선택) 공개 사이트 갱신 신호용 토큰도 같은 파일에
        path.write_text("\ufeff# 메모\nDATA_GO_KR_SERVICE_KEY = xyz \nOTHER=1\n", encoding="utf-8")  # 메모장이 붙이는 BOM, 공백
        self.assertEqual(self.local.read_keys(path), {"DATA_GO_KR_SERVICE_KEY": "xyz"})

    def test_server_serves_site_and_blocks_other_files(self):
        import urllib.error
        import urllib.request

        root = Path(tempfile.mkdtemp())
        (root / "web").mkdir()
        (root / "web" / "index.html").write_text("<p>ok</p>", encoding="utf-8")
        (root / "data").mkdir()
        (root / "data" / "latest.json").write_text("{}", encoding="utf-8")
        (root / "secret.txt").write_text("x", encoding="utf-8")
        server, port = self.local.start_server(root / "web", root / "data", range(8650, 8660))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.assertEqual(server.server_address[0], "127.0.0.1")  # 이 컴퓨터에서만 접속 가능
        self.assertEqual(opener.open(f"http://127.0.0.1:{port}/").read(), b"<p>ok</p>")
        self.assertEqual(opener.open(f"http://127.0.0.1:{port}/data/latest.json").read(), b"{}")
        with self.assertRaises(urllib.error.HTTPError):
            opener.open(f"http://127.0.0.1:{port}/data/secret.txt")

    def test_share_mode_listens_on_the_network(self):
        root = Path(tempfile.mkdtemp())
        (root / "web").mkdir()
        (root / "data").mkdir()
        server, _ = self.local.start_server(root / "web", root / "data", range(8660, 8670), host="0.0.0.0")
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual(server.server_address[0], "0.0.0.0")  # --share 를 준 때만

    # ---- 실시간 자료 (몇 분마다)

    def _cfg(self):
        from teukbo.config import load_config

        return load_config()

    def test_live_once_writes_live_json_and_keeps_state(self):
        from unittest import mock

        cfg, data = self._cfg(), Path(tempfile.mkdtemp())
        calls = []

        def fake(cfg_, now, env, prev, state):
            calls.append((prev, state, dict(env)))
            return {"schema": 1, "generated_at": "t", "n": len(calls)}, {"frames": len(calls)}

        with mock.patch.object(self.local.live_mod, "collect_all", fake):
            first = self.local.live_once(cfg, data, {"DATA_GO_KR_SERVICE_KEY": "k"})
            second = self.local.live_once(cfg, data, {"DATA_GO_KR_SERVICE_KEY": "k"})
        self.assertEqual((first["n"], second["n"]), (1, 2))
        self.assertEqual(calls[0][:2], (None, None))  # 처음에는 직전 값이 없다
        self.assertEqual(calls[1][:2], ({"schema": 1, "generated_at": "t", "n": 1}, {"frames": 1}))  # 다음에는 직전 결과와 상태를 이어 쓴다
        self.assertEqual(json.loads((data / "live.json").read_text(encoding="utf-8"))["n"], 2)
        self.assertEqual(json.loads((data / "live_state.json").read_text(encoding="utf-8")), {"frames": 2})
        self.assertIsNone(self.local.resolve_path("/data/live_state.json", self.web, self.data))  # 상태 파일은 화면에 내보내지 않는다

    def test_live_signature_changes_only_when_something_notable_changes(self):
        sig = self.local.live_signature
        base = {"generated_at": "02:00", "warnings": {"available": True, "active": [{"zone": "L1", "name": "호우주의보"}], "preliminary": []}, "obs": {"available": True, "checked_at": "02:00"},
                "fcst": {"available": True}, "rail_rain": {"available": True, "lines": [{"name": "전라선", "now": 0}]}}
        later = json.loads(json.dumps(base))
        later["generated_at"], later["obs"]["checked_at"] = "02:02", "02:02"
        self.assertEqual(sig(base), sig(later))  # 시각만 달라진 것은 알리지 않는다
        lifted = json.loads(json.dumps(base))
        lifted["warnings"]["active"] = []
        self.assertNotEqual(sig(base), sig(lifted))  # 특보가 풀리면 알린다
        wet = json.loads(json.dumps(base))
        wet["rail_rain"]["lines"][0]["now"] = 5
        self.assertNotEqual(sig(base), sig(wet))  # 선로에 비가 오기 시작하면 알린다

    def test_latest_points_to_local_live(self):
        cfg, data = self._cfg(), Path(tempfile.mkdtemp())
        (data / "latest.json").write_text(json.dumps({"regions": [], "live": {"url": "https://example.invalid/live.json", "stale_after_minutes": 30, "poll_seconds": 120}}), encoding="utf-8")
        self.local.use_local_live(data, cfg)
        live = json.loads((data / "latest.json").read_text(encoding="utf-8"))["live"]
        self.assertEqual(live["url"], "data/live.json")  # 이 컴퓨터가 방금 받은 파일
        self.assertEqual(live["poll_seconds"], cfg.settings.raw["local"]["poll_seconds"])
        self.assertLessEqual(cfg.settings.raw["local"]["live_interval_seconds"], 300)

    def test_cube_is_built_from_day_files_when_missing(self):
        data = Path(tempfile.mkdtemp())
        self.local.ensure_cube(data)  # 기록이 없으면 아무것도 하지 않는다
        self.assertFalse((data / "rail" / "cube.json").exists())
        fields = ["trn_no", "line", "class", "origin", "dest", "plan_dep", "plan_arr", "dep_delay", "arr_delay", "warnings"]
        day = {"date": "2026-11-11", "final": True, "fields": fields, "trains": [["00501", "전라선", "KTX", "용산", "여수엑스포", "05:10", "08:20", 0, 7, ["호우주의보"]]],
               "counts": {"planned": 1, "recorded": 1, "no_record": 0}, "all": {"n": 1}}
        (data / "rail" / "days").mkdir(parents=True)
        (data / "rail" / "days" / "2026-11-11.json").write_text(json.dumps(day, ensure_ascii=False), encoding="utf-8")
        self.local.ensure_cube(data)
        cube = json.loads((data / "rail" / "cube.json").read_text(encoding="utf-8"))
        self.assertEqual((cube["days"][0]["date"], cube["days"][0]["cells"][0][3], cube["warnings"]), ("2026-11-11", 1, ["호우주의보"]))

    # ---- 공개 사이트 갱신 신호

    def _trigger(self, runs):
        """runs: 실행 파일 → GitHub 에 기록된 마지막 실행 시각(ISO) 또는 None. 요청은 기록만 한다."""
        from teukbo.config import load_config

        sent = []

        def request(method, path, token, body=None):
            sent.append((method, path, token, body))
            workflow = path.split("/workflows/")[1].split("/")[0]
            if method == "GET":
                value = runs.get(workflow)
                if isinstance(value, int):
                    return value, {"message": "x"}
                return 200, {"workflow_runs": [{"created_at": value}] if value else []}
            runs[workflow] = "dispatched"
            return 204, None

        settings = load_config().settings.raw["local"]["trigger"]
        return self.local.Trigger(settings, "github_pat_secret", request), sent

    def test_trigger_slots_match_the_collect_workflow_schedule(self):
        import re as re_mod
        from datetime import datetime, timezone

        trigger, _ = self._trigger({})
        text = (ROOT / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8")
        minute, hours = re_mod.search(r'cron: "(\d+) ([\d,]+) \* \* \*"', text).groups()
        self.assertEqual((trigger.collect_minute, trigger.collect_hours), (int(minute), sorted(int(h) for h in hours.split(","))))  # 설정과 예약이 같아야 한다
        at = lambda h, m, d=13: datetime(2026, 11, d, h, m, tzinfo=timezone.utc)  # noqa: E731
        self.assertEqual(trigger.collect_slot(at(17, 24)), at(14, 25))
        self.assertEqual(trigger.collect_slot(at(17, 25)), at(17, 25))
        self.assertEqual(trigger.collect_slot(at(1, 0)), at(23, 25, 12))  # 자정을 넘겨도 어제의 마지막 회차

    def test_trigger_sends_only_when_github_has_not_run(self):
        from datetime import datetime, timedelta, timezone

        now = datetime(2026, 11, 13, 18, 21, tzinfo=timezone.utc)
        iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        # GitHub 예약이 돌지 않은 상태: 실시간은 96분 전, 수집은 17:25 회차 이전이 마지막
        runs = {"live.yml": iso(now - timedelta(minutes=96)), "collect.yml": iso(now - timedelta(minutes=95))}
        trigger, sent = self._trigger(runs)
        lines = trigger.tick(now)
        posts = [(path.split("/workflows/")[1], body) for method, path, _, body in sent if method == "POST"]
        self.assertEqual(posts, [("live.yml/dispatches", {"ref": "main"}), ("collect.yml/dispatches", {"ref": "main"})])
        self.assertTrue(all(path.startswith("/repos/teukbo/teukbo.github.io/actions/workflows/") for _, path, _, _ in sent))
        self.assertEqual(len(lines), 2)
        self.assertFalse(any("github_pat_secret" in line for line in lines))  # 토큰은 어디에도 찍지 않는다
        # 바로 다음 확인(30초 뒤)에는 아무 요청도 하지 않는다
        sent.clear()
        self.assertEqual(trigger.tick(now + timedelta(seconds=30)), [])
        self.assertEqual(sent, [])
        # 5분 뒤: 실시간만 다시 보낸다(수집은 다음 회차까지 기다린다)
        runs["live.yml"] = iso(now + timedelta(seconds=3))
        self.assertEqual(trigger.tick(now + timedelta(minutes=5)), [])
        self.assertEqual([path.split("/workflows/")[1] for method, path, _, _ in sent if method == "POST"], ["live.yml/dispatches"])

    def test_trigger_stays_quiet_when_github_schedule_works(self):
        from datetime import datetime, timedelta, timezone

        now = datetime(2026, 11, 13, 18, 21, tzinfo=timezone.utc)
        iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        runs = {"live.yml": iso(now - timedelta(minutes=2)), "collect.yml": iso(now - timedelta(minutes=50))}  # 둘 다 제때 돌았다
        trigger, sent = self._trigger(runs)
        self.assertEqual(trigger.tick(now), [])
        self.assertEqual([m for m, *_ in sent], ["GET", "GET"])  # 확인만 하고 신호는 보내지 않는다

    def test_trigger_explains_problems_once_in_plain_words(self):
        from datetime import datetime, timedelta, timezone

        now = datetime(2026, 11, 13, 18, 21, tzinfo=timezone.utc)
        for status, word in ((401, "토큰"), (403, "권한"), (404, "저장소"), (None, "연결")):
            def request(method, path, token, body=None, status=status):
                return status, {"message": "Bad credentials"}

            from teukbo.config import load_config

            trigger = self.local.Trigger(load_config().settings.raw["local"]["trigger"], "github_pat_secret", request)
            first = trigger.tick(now)
            self.assertEqual(len(first), 1)
            self.assertIn(word, first[0])
            self.assertNotIn("github_pat_secret", first[0])
            self.assertEqual(trigger.tick(now + timedelta(seconds=30)), [])  # 같은 문제를 되풀이해 적지 않는다

    def test_trigger_rejects_a_malformed_repository_name(self):
        with self.assertRaises(ValueError):
            self.local.Trigger({"repository": "teukbo/teukbo.github.io/../x"}, "t")
        with self.assertRaises(ValueError):
            self.local.Trigger({"repository": ""}, "t")

    def test_gitignore_covers_local_keys(self):
        self.assertIn("/local_keys.txt", (ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_windows_launcher_is_ascii_with_crlf(self):
        raw = (ROOT / "start.bat").read_bytes()
        raw.decode("ascii")  # 한글이 섞이면 명령 프롬프트 인코딩에 따라 깨질 수 있다
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b"-m teukbo.local", raw)
        share = (ROOT / "start-share.bat").read_bytes()
        share.decode("ascii")
        self.assertNotIn(b"\n", share.replace(b"\r\n", b""))
        self.assertIn(b"start.bat\" --share", share)


class RepositoryHygieneTest(unittest.TestCase):
    def test_no_secrets_or_keys_in_tracked_config(self):
        for path in (ROOT / "config").glob("*.json"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("serviceKey", text)
            self.assertNotIn("authKey", text)

    def test_gitignore_excludes_deploy_only_outputs(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for line in ("/data/latest.json", "/data/warnings.json", "/data/detail/", "/data-demo/", "/_site/"):
            self.assertIn(line, ignore)


if __name__ == "__main__":
    unittest.main()


class RailPageTest(unittest.TestCase):
    """철도 특보 지도(web/rail.html)와 지도 자료(web/geo)의 짜임새."""

    WEB = ROOT / "web"

    @classmethod
    def setUpClass(cls):
        cls.zones = json.loads((cls.WEB / "geo" / "zones.json").read_text(encoding="utf-8"))
        cls.rail = json.loads((cls.WEB / "geo" / "rail.json").read_text(encoding="utf-8"))
        cls.stations = json.loads((cls.WEB / "geo" / "stations.json").read_text(encoding="utf-8"))
        cls.lines = json.loads((cls.WEB / "geo" / "lines.json").read_text(encoding="utf-8"))

    def test_zone_shapes_cover_every_land_warning_zone(self):
        from teukbo.zones import load_zone_table

        table = load_zone_table()
        self.assertEqual(set(self.zones), set(table.leaf_codes(land_only=True)))
        for code, zone in self.zones.items():
            self.assertTrue(zone["n"], code)
            lon, lat = zone["c"]
            self.assertTrue(124 < lon < 132 and 33 < lat < 39, code)
            self.assertTrue(zone["p"], code)
            for ring in zone["p"]:
                self.assertEqual(len(ring) % 2, 0, code)
                self.assertGreaterEqual(len(ring), 6, code)

    def test_every_region_zone_is_on_the_map(self):
        from teukbo.zones import load_zone_table

        table = load_zone_table()
        owned: dict[str, list[str]] = {}
        for region in FULL.regions:
            on_map = {c for c in region.zone_codes if c in self.zones}
            self.assertTrue(on_map, region.name)
            for code in on_map:
                owned.setdefault(code, []).append(region.id)
            for code in region.zone_codes:
                if code not in self.zones:  # 상위 구역(시·군) 코드 → 이 지역이 그 말단 구역 가운데 하나여야 한다
                    self.assertTrue(set(table.leaves(code)) & on_map, f"{region.name} {code}")
            if not region.id.startswith("L"):  # 시·군 단위로 묶은 지역(전남)은 그 시·군의 말단 구역을 모두 가진다
                for code in region.zone_codes:
                    if code not in self.zones:
                        self.assertTrue(set(table.leaves(code)) <= on_map, f"{region.name} {code}")
        # 전국: 지도의 모든 육상 특보구역이 어느 지역엔가 속한다(빈 곳 없이). 두 지역이 함께 쓰는 구역은 보성(보성군·벌교)뿐
        self.assertEqual(set(owned), set(self.zones))
        self.assertEqual({code: ids for code, ids in owned.items() if len(ids) > 1}, {"L1050900": ["beolgyo", "boseong"]})

    def test_stations_point_to_zones_on_the_map(self):
        self.assertGreater(len(self.stations), 400)
        names = {s["name"]: s for s in self.stations}
        for s in self.stations:
            self.assertIn(s["zone"], self.zones, s["name"])
            self.assertTrue(124 < s["lon"] < 132 and 33 < s["lat"] < 39, s["name"])
        self.assertEqual(names["순천"]["zone"], "L1051200")
        self.assertEqual(names["벌교"]["zone"], "L1050900")
        codes = [s["code"] for s in self.stations if s["code"]]
        self.assertEqual(len(codes), len(set(codes)))

    def test_rail_lines(self):
        names = {line["name"] for line in self.rail}
        for name in ("경부선", "호남선", "전라선", "경전선", "경부고속선", "호남고속선"):
            self.assertIn(name, names)
        self.assertEqual({line["name"] for line in self.rail if line["fast"]}, {"경부고속선", "호남고속선", "수서평택고속선"})
        for line in self.rail:
            for path in line["paths"]:
                self.assertEqual(len(path) % 2, 0, line["name"])
                self.assertGreaterEqual(len(path), 4, line["name"])

    def test_lines_pass_through_known_zones_and_stations(self):
        """노선이 지나는 특보구역·역 자료(노선별 특보 현황, 철도 구간 전망의 바탕)."""
        keys = {s["code"] or f"name:{s['name']}": s for s in self.stations}
        self.assertEqual({l["name"] for l in self.lines}, {l["name"] for l in self.rail})
        for line in self.lines:
            zones = [zone for zone, _ in line["parts"]]
            self.assertEqual(len(zones), len(set(zones)), line["name"])  # 한 구역은 한 번만
            self.assertTrue(zones, line["name"])
            seen = set()
            for zone, station_keys in line["parts"]:
                self.assertIn(zone, self.zones, line["name"])
                for key in station_keys:
                    self.assertIn(key, keys, f"{line['name']} {key}")
                    self.assertEqual(keys[key]["zone"], zone, f"{line['name']} {keys[key]['name']}")  # 역은 자기 구역 아래에만
                    self.assertNotIn(key, seen, f"{line['name']} {key}")
                    seen.add(key)
        by_name = {l["name"]: l for l in self.lines}
        names = lambda line: [[keys[k]["name"] for k in ks] for _, ks in by_name[line]["parts"]]  # noqa: E731
        flat = lambda line: [n for group in names(line) for n in group]  # noqa: E731
        # 전라선: 익산 → … → 순천 → 여수 (북→남), 구례구역은 순천 구역
        jeolla = by_name["전라선"]
        self.assertEqual(flat("전라선")[0], "익산")
        self.assertEqual(flat("전라선")[-1], "여수엑스포")
        self.assertIn(["구례구", "순천"], names("전라선"))
        self.assertEqual([z for z, ks in jeolla["parts"] if "3900271" in ks], ["L1051200"])
        # 경전선: 서→동, 벌교는 보성 구역
        self.assertLess(flat("경전선").index("광주송정"), flat("경전선").index("순천"))
        self.assertLess(flat("경전선").index("순천"), flat("경전선").index("진주"))
        self.assertIn("벌교", next(group for (zone, _), group in zip(by_name["경전선"]["parts"], names("경전선")) if zone == "L1050900"))
        # 역이 없는 구역도 빠지지 않는다 (전라선은 곡성남부를 지나지만 그 안에 역이 없다)
        self.assertIn("L1052920", [z for z, _ in jeolla["parts"]])
        # 이웃 노선의 역이 섞이지 않는다
        self.assertNotIn("거제해맞이", flat("경부고속선"))
        self.assertTrue(by_name["경부고속선"]["fast"])

    def test_page_has_every_element_the_script_uses(self):
        import re

        html = (self.WEB / "rail.html").read_text(encoding="utf-8")
        script = (self.WEB / "rail.js").read_text(encoding="utf-8")
        ids = set(re.findall(r'\$\("([\w-]+)"\)', script))
        self.assertGreaterEqual(len(ids), 10)
        for name in ids:
            self.assertIn(f'id="{name}"', html, name)
        self.assertIn("참고용입니다. 공식 특보·예비특보가 우선합니다.", html)
        self.assertIn("https://gis.korail.com/korailTalk/entrance?route=main", html)  # 실시간 위치는 코레일 공식 화면으로 연결만 한다
        self.assertIn("OpenStreetMap", html)  # ODbL 출처 표시
        self.assertNotIn("gis.korail.com", script)  # 코레일 화면의 내부 주소를 직접 부르지 않는다
        self.assertNotIn("gis.korail.com", (self.WEB / "live.js").read_text(encoding="utf-8"))

    def test_first_screen_is_only_search_probability_and_map(self):
        """첫 화면은 지역 찾기 → 그 지역의 가능성 → 지도, 이것만. 다른 화면으로 가는 메뉴를 두지 않는다."""
        import re

        html = (self.WEB / "index.html").read_text(encoding="utf-8")
        script = (self.WEB / "home.js").read_text(encoding="utf-8")
        self.assertIn("참고용입니다. 공식 특보·예비특보가 우선합니다.", html)
        self.assertNotIn('<nav', html)
        for other in ("full.html", "rail.html", "analysis.html"):
            self.assertNotIn(other, html, other)
        self.assertEqual(re.findall(r'<h2 id="([\w-]+)"', html), ["h-proof"])  # 구역 제목은 맨 아래 '과거 검증' 하나뿐
        self.assertIn("data/backtest.json", script)
        body = html[html.index("<main"):html.index("</main>")]
        self.assertLess(body.index('id="q"'), body.index('id="result"'))  # 찾기 → 가능성 → 지도
        self.assertLess(body.index('id="result"'), body.index('id="map"'))
        self.assertLess(body.index('id="map"'), body.index('id="proof"'))  # 과거 검증은 지도 아래에
        for name in set(re.findall(r'\$\("([\w-]+)"\)', script)):
            self.assertIn(f'id="{name}"', html, name)
        self.assertNotIn("innerHTML", script)  # 받아 온 글은 늘 텍스트로 넣는다
        self.assertIn("Open-Meteo", html)  # 출처 표기(CC BY 4.0)는 남긴다
        self.assertLess(html.index("ui.js"), html.index("home.js"))
        self.assertLess(html.index("live.js"), html.index("home.js"))
        self.assertIn("applyToLatest", script)  # 공식 특보·예비특보가 나오면 가능성보다 먼저 반영한다
        # 전국: 지역을 고르지 않으면 전국 요약과 전국 지도, 고르면 그 지역과 주변 지도 — '전국 보기'로 돌아간다
        self.assertIn('id="back"', html)
        self.assertIn("renderNation", script)
        self.assertIn("nearBox", script)
        worker = (self.WEB / "sw.js").read_text(encoding="utf-8")
        self.assertIn('"home.js?v=__BUILD__"', worker)

    def test_older_pages_are_kept_but_not_linked_from_the_first_screen(self):
        import re

        pages = {name: (self.WEB / name).read_text(encoding="utf-8") for name in ("full.html", "rail.html", "analysis.html")}
        current = {"full.html": ["full.html"], "rail.html": ["rail.html"], "analysis.html": []}
        for name, html in pages.items():
            nav = re.search(r'<nav class="tabs".*?</nav>', html, re.S).group(0)
            self.assertEqual(re.findall(r'<a href="([^"]+)"', nav), ["./", "full.html", "rail.html"], name)
            self.assertEqual(re.findall(r'<a href="([^"]+)" aria-current="page"', nav), current[name], name)
        for name, html in pages.items():
            self.assertIn("참고용입니다. 공식 특보·예비특보가 우선합니다.", html, name)
            self.assertIn('class="lede"', html, name)  # 화면마다 무엇을 보여 주는지 한 줄
        worker = (self.WEB / "sw.js").read_text(encoding="utf-8")
        for name in ("rail.html", "rail.js", "live.js", "analysis.html", "analysis.js", "analysis-core.js", "ui.js", "geo/zones.json", "geo/rail.json", "geo/stations.json", "geo/lines.json"):
            self.assertIn(name, worker)

    def test_analysis_page_has_every_element_the_script_uses(self):
        import re

        html = (self.WEB / "analysis.html").read_text(encoding="utf-8")
        script = (self.WEB / "analysis.js").read_text(encoding="utf-8")
        made = set(re.findall(r'\bid: "([\w-]+)"', script))  # 스크립트가 직접 만드는 요소
        ids = set(re.findall(r'\$\("([\w-]+)"\)', script))
        self.assertGreaterEqual(len(ids), 20)
        for name in ids - made:
            self.assertIn(f'id="{name}"', html, name)
        self.assertLess(html.index("ui.js"), html.index("analysis-core.js"))
        self.assertLess(html.index("analysis-core.js"), html.index("analysis.js?"))
        # 구역 순서: 조건 → 요약 → 날짜별 → 종류별 → 분포 → 노선별 → 특보가 있었던 날 → 그날의 열차
        order = re.findall(r'<h2 id="(h-[a-z]+)"', html)
        self.assertEqual(order[:8], ["h-filter", "h-kpi", "h-daily", "h-type", "h-dist", "h-line", "h-days", "h-day"])
        self.assertNotIn("innerHTML", script)  # 받아 온 글은 늘 텍스트로 넣는다
        self.assertEqual(html.count("data-needs-cube"), 7)  # 운행 기록이 없을 때 감출 구역: ①, ③~⑧ (②에는 안내를 남긴다)
        self.assertIn("[data-needs-cube]", script)
        self.assertIn("원인", html)  # 상관과 원인을 구분하는 설명
        # 분석은 한곳에: 다른 화면에는 통계 구역을 두지 않는다
        self.assertNotIn('id="delay"', (self.WEB / "rail.html").read_text(encoding="utf-8"))
        self.assertNotIn('id="accuracy"', (self.WEB / "full.html").read_text(encoding="utf-8"))

    def test_pages_have_elements_for_live_data(self):
        import re

        index = (self.WEB / "full.html").read_text(encoding="utf-8")  # 예전의 자세한 화면
        rail = (self.WEB / "rail.html").read_text(encoding="utf-8")
        app = (self.WEB / "app.js").read_text(encoding="utf-8")
        for name in set(re.findall(r'\$\("([\w-]+)"\)', app)):
            self.assertIn(f'id="{name}"', index, name)
        for name in ("radar-toggle", "radar-bar", "radar-time", "radar-play", "radar-scale", "now-summary", "line-status"):  # 지도: 레이더 겹쳐 보기, 요약, 노선별 현황
            self.assertIn(f'id="{name}"', rail, name)
        for page in (index, rail):
            self.assertIn('id="live-status"', page)
            self.assertLess(page.index("live.js"), max(page.find("app.js"), page.find("rail.js")))  # 실시간 모듈이 먼저 실려야 한다
        # 코레일 화면은 권한을 줄인 틀 안에 그대로 보여 준다
        frame = re.search(r"<iframe[^>]+>", rail).group(0)
        self.assertIn("https://gis.korail.com/korailTalk/entrance?route=main", frame)
        self.assertIn('sandbox="allow-scripts allow-same-origin"', frame)
        self.assertIn("title=", frame)

    def test_map_projection_matches_official_forecast_grid(self):
        """지도·레이더 좌표 계산(live.js radarProject)을 기상청 동네예보 격자(같은 도법, 5km)와 대조한다."""
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skipTest("node 가 없습니다")
        points = [[r.lon, r.lat] for r in FULL.regions] + [[126.0, 38.0], [124.7, 33.1], [131.87, 37.24]]
        script = """
            const fs = require('fs');
            global.window = {}; global.document = { addEventListener() {} };
            eval(fs.readFileSync(process.argv[1], 'utf8'));
            const L = window.TeukboLive;
            console.log(JSON.stringify({ image: L.RADAR_IMAGE, xy: JSON.parse(process.argv[2]).map(([lon, lat]) => L.radarProject(lon, lat)), scale: L.RADAR_SCALE.map((s) => s[0]) }));
        """
        result = subprocess.run([node, "-e", script, str(self.WEB / "live.js"), json.dumps(points)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        image = out["image"]
        self.assertEqual((image["width"], image["height"]), (635, 620))
        from teukbo.grid import latlon_to_grid

        for (lon, lat), (x, y) in zip(points, out["xy"]):
            east_km = (x - image["x0"]) * image["kmPerPx"]
            north_km = (image["y0"] - y) * image["kmPerPx"]
            grid = (int((east_km / 5.0 + 43 + 0.5) // 1), int((north_km / 5.0 + 136 + 0.5) // 1))
            self.assertEqual(grid, latlon_to_grid(lat, lon), (lon, lat))
            self.assertTrue(0 <= x <= image["width"] and 0 <= y <= image["height"], (lon, lat))  # 우리나라는 모두 영상 안에 들어온다
        self.assertEqual(out["xy"][len(FULL.regions)], [image["x0"], image["y0"]])  # 기준점(126°E, 38°N)
        for region, (x, y) in zip(FULL.regions, out["xy"]):
            self.assertEqual((region.nx, region.ny), latlon_to_grid(region.lat, region.lon), region.name)
        self.assertEqual(out["scale"], sorted(out["scale"]))  # 범례는 약한 비 → 강한 비 순서
        self.assertEqual(len(out["scale"]), 24)

    def test_live_workflow(self):
        text = (ROOT / ".github" / "workflows" / "live.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "*/5 * * * *"', text)
        self.assertIn("python3 -m teukbo.live", text)
        self.assertIn("secrets.DATA_GO_KR_SERVICE_KEY", text)
        self.assertIn("--force", text)  # 기록을 쌓지 않는다
        self.assertNotIn("git push origin main", text)
        collect = (ROOT / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8")
        self.assertIn("TEUKBO_LIVE_URL: https://raw.githubusercontent.com/${{ github.repository }}/live/live.json", collect)

    def test_workflow_runs_rail_statistics_without_blocking_weather(self):
        text = (ROOT / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8")
        step = text[text.index("python3 -m teukbo.rail_run") - 400 : text.index("python3 -m teukbo.rail_run")]
        self.assertIn("continue-on-error: true", step)
        self.assertIn("timeout-minutes:", step)
        self.assertIn("git add data/rail", text)
        self.assertLess(text.index("python3 -m teukbo.run\n"), text.index("python3 -m teukbo.rail_run"))

