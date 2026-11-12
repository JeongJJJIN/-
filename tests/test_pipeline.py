import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from teukbo import run, site
from teukbo.config import ROOT
from teukbo.http import FetchError
from teukbo.sources import kma_forecast, kma_warnings, open_meteo

from .helpers import config, kst

CFG = config()


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

    def test_rail_summary(self):
        doc = json.loads((self.dir / "rail" / "summary.json").read_text(encoding="utf-8"))
        self.assertTrue(doc["demo"])
        self.assertEqual(doc["period"], {"from": "2026-10-29", "to": "2026-11-11", "days": 14})
        self.assertEqual(len(doc["daily"]), 14)
        self.assertEqual(doc["latest_day"]["date"], "2026-11-11")
        self.assertGreater(doc["warned"]["mean"], doc["clear"]["mean"])  # 시나리오: 특보 구간을 지난 열차가 더 늦다
        self.assertEqual(set(doc["by_warning"]), {"호우경보", "호우주의보"})
        self.assertGreater(doc["daily"][-1]["warned"], 0)
        top = doc["latest_day"]["top_delayed"][0]
        self.assertEqual(set(top), {"trn_no", "line", "class", "origin", "dest", "plan_arr", "arr_delay", "warnings"})
        for key in ("late5", "late10", "late20"):
            self.assertIn(key, doc["clear"])

    def test_site_build(self):
        out = Path(tempfile.mkdtemp()) / "_site"
        build = site.build_site(self.dir, out)
        self.assertEqual(len(build), 10)
        for name in ("index.html", "app.js", "style.css", "sw.js", "manifest.webmanifest", "data/latest.json", "data/accuracy.json", "data/warnings.json", "data/detail/suncheon.json",
                     "rail.html", "rail.js", "geo/zones.json", "geo/rail.json", "geo/stations.json", "data/rail.json"):
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

        def fake(regions, model, *_a, **_kw):
            return {r.id: forecasts[model.id][r.id] for r in regions}

        out = io.StringIO()
        with mock.patch.object(open_meteo, "fetch_model", side_effect=fake), mock.patch.dict(
            "os.environ", {"DATA_GO_KR_SERVICE_KEY": "", "KMA_APIHUB_KEY": ""}
        ), contextlib.redirect_stdout(out):
            code = check.main()
        self.assertEqual(code, 0)
        self.assertIn("정상  ecmwf_ifs025", out.getvalue())
        self.assertIn("없는 변수: gust", out.getvalue())  # JMA 는 돌풍 자료가 없다


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
        self.assertEqual(r("/rail.html"), self.web / "rail.html")
        self.assertEqual(r("/geo/zones.json"), self.web / "geo" / "zones.json")

    def test_paths_refused(self):
        r = lambda p: self.local.resolve_path(p, self.web, self.data)  # noqa: E731
        for path in (
            "/data/history/2026-11-12.json",  # 이력은 화면에 필요 없다
            "/data/rail/days/2026-11-11.json",
            "/data/rail/summary.json",
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
        self.assertEqual(self.local.read_keys(path), {"DATA_GO_KR_SERVICE_KEY": "abc+def==", "KMA_APIHUB_KEY": ""})
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

    def test_gitignore_covers_local_keys(self):
        self.assertIn("/local_keys.txt", (ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_windows_launcher_is_ascii_with_crlf(self):
        raw = (ROOT / "start.bat").read_bytes()
        raw.decode("ascii")  # 한글이 섞이면 명령 프롬프트 인코딩에 따라 깨질 수 있다
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b"-m teukbo.local", raw)


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
        for region in CFG.regions:
            on_map = {c for c in region.zone_codes if c in self.zones}
            self.assertTrue(on_map, region.name)
            for code in region.zone_codes:
                if code not in self.zones:  # 세분화 전의 상위 구역 코드 → 그 말단 구역들이 지도에 있어야 한다
                    self.assertTrue(set(table.leaves(code)) <= on_map, f"{region.name} {code}")

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

    def test_both_pages_link_to_each_other_and_work_offline(self):
        index = (self.WEB / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="rail.html"', index)
        self.assertIn('href="./"', (self.WEB / "rail.html").read_text(encoding="utf-8"))
        worker = (self.WEB / "sw.js").read_text(encoding="utf-8")
        for name in ("rail.html", "rail.js", "geo/zones.json", "geo/rail.json", "geo/stations.json"):
            self.assertIn(name, worker)

    def test_workflow_runs_rail_statistics_without_blocking_weather(self):
        text = (ROOT / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8")
        step = text[text.index("python3 -m teukbo.rail_run") - 400 : text.index("python3 -m teukbo.rail_run")]
        self.assertIn("continue-on-error: true", step)
        self.assertIn("timeout-minutes:", step)
        self.assertIn("git add data/rail", text)
        self.assertLess(text.index("python3 -m teukbo.run\n"), text.index("python3 -m teukbo.rail_run"))

