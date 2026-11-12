"""선로 강우(teukbo.radar): 레이더 영상 읽기, 좌표, 선로 표본점 집계."""

import json
import shutil
import struct
import subprocess
import unittest
import zlib
from pathlib import Path
from unittest import mock

from teukbo import live, radar
from teukbo.config import ROOT
from teukbo.grid import latlon_to_grid

from .helpers import config, kst

CFG = config()
FIXTURE = Path(__file__).parent / "fixtures" / "RDR_CMP_WRC_202611130020.png"  # 실제 영상(2026-11-13 00:20, 남서 해상·동해에 비)


def make_png(pixels: dict[tuple[int, int], tuple[int, int, int]] | None = None, width: int = radar.WIDTH, height: int = radar.HEIGHT, legend: bool = True) -> bytes:
    """시험용 영상: 흰 바탕에 지정한 화소만 칠하고, 오른쪽에 실제와 같은 범례를 그린다."""
    rows = [bytearray(b"\xff" * (width * 3)) for _ in range(height)]
    if legend and width == radar.WIDTH:
        for k, (_lo, color) in enumerate(reversed(radar.SCALE)):
            for y in range(21 + 24 * k, 21 + 24 * k + 24):
                for x in range(600, 608):
                    rows[y][x * 3:x * 3 + 3] = bytes(color)
    for (x, y), color in (pixels or {}).items():
        rows[y][x * 3:x * 3 + 3] = bytes(color)
    raw = b"".join(b"\x00" + bytes(row) for row in rows)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


COLOR = {lo: color for lo, color in radar.SCALE}


class PngTest(unittest.TestCase):
    def test_real_image_is_decoded(self):
        frame = radar.Frame(FIXTURE.read_bytes())
        self.assertEqual((frame.width, frame.height, frame.bpp), (635, 620, 3))
        self.assertTrue(frame.legend_ok())
        self.assertEqual(frame.rate_at(542, 291), 1)  # 동해의 비구름 (OpenCV 로 읽은 값과 대조해 둔 화소)
        self.assertIsNone(frame.rate_at(300, 300))  # 내륙의 흰 바탕
        self.assertIsNone(frame.rate_at(603, 300))  # 범례는 지도 밖 → 읽지 않는다
        self.assertIsNone(frame.rate_at(10, 5))  # 제목 줄
        rainy = sum(1 for y in range(21, 619) for x in range(1, 599) if (frame.rate_at(x, y) or 0) >= 0.1)
        self.assertEqual(rainy, 6016)  # 0.1mm/h 이상 화소 수 (구간별 합: 2235+1830+1753+91+34+15+14+11+7+4+7+8+4+3)

    def test_rejects_other_formats_and_sizes(self):
        with self.assertRaises(ValueError):
            radar.decode_png(b"<html>not found</html>")
        with self.assertRaises(ValueError):
            radar.Frame(make_png(width=100, height=100, legend=False))  # 크기가 바뀌면 위치 맞춤이 무효 → 읽지 않는다

    def test_legend_change_is_detected(self):
        self.assertTrue(radar.Frame(make_png()).legend_ok())
        self.assertFalse(radar.Frame(make_png({(603, 21 + 12): (1, 2, 3)})).legend_ok())

    def test_near_takes_strongest_within_one_pixel(self):
        frame = radar.Frame(make_png({(300, 300): COLOR[5], (301, 301): COLOR[30], (310, 310): (0, 0, 0)}))
        self.assertEqual(frame.rate_near(300.2, 299.8), 30)
        self.assertEqual(frame.rate_near(299.0, 299.0), 5)
        self.assertEqual(frame.rate_near(297.0, 300.0), 0.0)
        self.assertEqual(frame.rate_near(310.0, 310.0), 0.0)  # 해안선 같은 검은 화소는 비가 아니다


class ScaleTest(unittest.TestCase):
    def test_bins(self):
        self.assertEqual(radar.upper_bound(5), 6)
        self.assertEqual(radar.upper_bound(30), 40)
        self.assertIsNone(radar.upper_bound(150))
        self.assertEqual(radar.midpoint(15), 17.5)
        self.assertEqual(radar.midpoint(150), 150)
        self.assertEqual([lo for lo, _ in radar.SCALE], sorted(lo for lo, _ in radar.SCALE))
        self.assertEqual(len({color for _, color in radar.SCALE}), 24)

    def test_same_scale_and_projection_as_the_web_page(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node 가 없습니다")
        points = [[127.4872, 34.9507], [126.0, 38.0], [129.04, 35.115], [126.978, 37.566]]
        script = """
            const fs = require('fs');
            global.window = {}; global.document = { addEventListener() {} };
            eval(fs.readFileSync(process.argv[1], 'utf8'));
            const L = window.TeukboLive;
            console.log(JSON.stringify({ scale: L.RADAR_SCALE, image: L.RADAR_IMAGE, xy: JSON.parse(process.argv[2]).map(([lon, lat]) => L.radarProject(lon, lat)) }));
        """
        out = json.loads(subprocess.run([node, "-e", script, str(ROOT / "web" / "live.js"), json.dumps(points)], capture_output=True, text=True, timeout=60, check=True).stdout)
        self.assertEqual([[lo, "#%02x%02x%02x" % color] for lo, color in radar.SCALE], out["scale"])
        self.assertEqual((out["image"]["kmPerPx"], out["image"]["x0"], out["image"]["y0"]), (radar.KM_PER_PX, radar.X0, radar.Y0))
        frame = out["image"]["frame"]
        self.assertEqual((frame["x"], frame["y"], frame["x"] + frame["w"] - 1, frame["y"] + frame["h"] - 1), radar.FRAME)
        for (lon, lat), (x, y) in zip(points, out["xy"]):
            px, py = radar.project(lon, lat)
            self.assertAlmostEqual(px, x, places=6)
            self.assertAlmostEqual(py, y, places=6)

    def test_projection_matches_official_grid(self):
        for region in CFG.regions:
            x, y = radar.project(region.lon, region.lat)
            east, north = (x - radar.X0) * radar.KM_PER_PX, (radar.Y0 - y) * radar.KM_PER_PX
            self.assertEqual((int((east / 5 + 43.5) // 1), int((north / 5 + 136.5) // 1)), latlon_to_grid(region.lat, region.lon), region.name)


class RailRainTest(unittest.TestCase):
    """선로 표본점에서 강우를 읽고 노선별로 모은다."""

    @classmethod
    def setUpClass(cls):
        cls.samples = radar.RailSamples.load()
        stations = {s["name"]: s for s in json.loads((ROOT / "web" / "geo" / "stations.json").read_text(encoding="utf-8"))}
        cls.suncheon = stations["순천"]
        cls.sx, cls.sy = (int(round(v)) for v in radar.project(cls.suncheon["lon"], cls.suncheon["lat"]))

    def frame(self, rate, at=None):
        x, y = at or (self.sx, self.sy)
        return radar.Frame(make_png({(x, y): COLOR[rate]}))

    def test_samples_cover_every_line(self):
        rail = json.loads((ROOT / "web" / "geo" / "rail.json").read_text(encoding="utf-8"))
        zones = json.loads((ROOT / "web" / "geo" / "zones.json").read_text(encoding="utf-8"))
        self.assertEqual(self.samples.names, [line["name"] for line in rail])
        self.assertGreater(len(self.samples.points), 4000)
        for _line, lon, lat, zone, x, y in self.samples.points:
            self.assertIn(zone, zones)
            self.assertTrue(radar.FRAME[0] <= x <= radar.FRAME[2] and radar.FRAME[1] <= y <= radar.FRAME[3], (lon, lat))  # 모두 영상의 지도 부분 안

    def test_rain_at_a_station_is_read_on_the_lines_through_it(self):
        found = self.samples.read(self.frame(15))
        lines = {self.samples.names[self.samples.points[i][0]] for i in found}
        self.assertEqual(lines, {"전라선", "경전선"})  # 순천역을 지나는 두 노선
        self.assertEqual(set(found.values()), {15})
        for i in found:
            _line, lon, lat, zone, _x, _y = self.samples.points[i]
            self.assertLess(abs(lon - self.suncheon["lon"]) + abs(lat - self.suncheon["lat"]), 0.08)  # 순천역에서 몇 km 안
            self.assertEqual(zone, "L1051200")
        self.assertEqual(self.samples.read(self.frame(0.0)), {})  # 0.1mm/h 미만의 약한 에코는 비로 세지 않는다
        self.assertEqual(self.samples.read(radar.Frame(FIXTURE.read_bytes())), {})  # 실제 영상: 그 시각 선로 위에는 비가 없었다

    def collect(self, now, state, frames):
        """frames: {영상 시각: Frame 또는 None}. 받으려 한 시각을 self.asked 에 남긴다."""
        self.asked = []

        def fake(stamp, _opts=None):
            self.asked.append(stamp)
            return frames.get(stamp)

        with mock.patch.object(radar, "fetch_frame", side_effect=fake):
            return radar.collect(now, state, self.samples)

    def test_collect_now_and_one_hour_estimate(self):
        now = kst(2026, 11, 13, 1, 33)
        frames = {"202611130125": self.frame(15), "202611130120": self.frame(15), "202611130115": self.frame(5), "202611130110": self.frame(0.0)}
        result, state = self.collect(now, None, frames)
        self.assertEqual((result["available"], result["frame"], result["checked_at"], result["frames_1h"], result["error"]),
                         (True, "2026-11-13T01:25+09:00", "2026-11-13T01:33+09:00", 4, None))
        self.assertEqual(self.asked[:2], ["202611130130", "202611130125"])  # 가장 최근 시각부터 찾는다
        names = [line["name"] for line in result["lines"]]
        self.assertEqual(sorted(names), ["경전선", "전라선"])
        line = result["lines"][0]
        self.assertEqual((line["now"], line["hi"], line["zone"]), (15, 20, "L1051200"))
        self.assertEqual(line["acc60"], round((17.5 + 17.5 + 5.5) / 12, 1))  # 구간 가운데 값 × 5분씩
        self.assertEqual(line["parts"], [["L1051200", 15, line["acc60"]]])
        self.assertLess(abs(line["at"][0] - self.suncheon["lon"]) + abs(line["at"][1] - self.suncheon["lat"]), 0.08)
        self.assertTrue(result["points"])
        self.assertEqual({p[2] for p in result["points"]}, {15})
        self.assertEqual(state["points"], len(self.samples.points))
        self.assertEqual(sorted(state["frames"]), ["202611130110", "202611130115", "202611130120", "202611130125"])
        json.dumps([result, state])

    def test_known_frames_are_not_fetched_again_and_old_ones_drop_out(self):
        first, state = self.collect(kst(2026, 11, 13, 1, 33), None, {"202611130125": self.frame(15), "202611130030": self.frame(5)})
        self.assertEqual(first["frames_1h"], 2)
        second, state2 = self.collect(kst(2026, 11, 13, 1, 38), state, {"202611130130": self.frame(1)})
        self.assertEqual(second["frame"], "2026-11-13T01:30+09:00")
        self.assertNotIn("202611130125", self.asked)  # 이미 읽은 영상
        self.assertNotIn("202611130030", state2["frames"])  # 1시간이 지난 영상은 버린다
        self.assertEqual(sorted(state2["frames"]), ["202611130125", "202611130130"])
        line = second["lines"][0]
        self.assertEqual(line["now"], 1)
        self.assertEqual(line["acc60"], round((17.5 + 1.5) / 12, 1))
        # 새 영상이 아직 없으면 직전 영상 그대로(다시 받지 않고)
        third, _ = self.collect(kst(2026, 11, 13, 1, 39), state2, {})
        self.assertEqual((third["frame"], third["frames_1h"]), ("2026-11-13T01:30+09:00", 2))

    def test_rain_that_stopped_still_counts_in_the_hour_total(self):
        result, _ = self.collect(kst(2026, 11, 13, 1, 33), None, {"202611130125": radar.Frame(make_png()), "202611130100": self.frame(30)})
        line = result["lines"][0]
        self.assertEqual((line["now"], line["acc60"]), (0.0, round(35 / 12, 1)))
        self.assertNotIn("at", line)
        self.assertEqual(result["points"], [])

    def test_no_recent_image_is_reported_not_invented(self):
        result, state = self.collect(kst(2026, 11, 13, 1, 33), None, {})
        self.assertFalse(result["available"])
        self.assertIn("찾지 못했습니다", result["error"])
        self.assertEqual(len(self.asked), 7)  # 최근 35분만 찾아본다

    def test_changed_legend_is_refused(self):
        bad = radar.Frame(make_png({(603, 21 + 12): (1, 2, 3), (self.sx, self.sy): COLOR[15]}))
        result, _ = self.collect(kst(2026, 11, 13, 1, 33), None, {"202611130125": bad})
        self.assertFalse(result["available"])
        self.assertIn("범례", result["error"])

    def test_state_from_other_sample_set_is_ignored(self):
        _, state = self.collect(kst(2026, 11, 13, 1, 33), {"points": 10, "frames": {"202611130125": {"3": 15}}}, {"202611130125": self.frame(5)})
        self.assertIn("202611130125", self.asked)  # 표본점이 바뀌었으면 옛 상태를 쓰지 않고 다시 읽는다
        self.assertEqual(state["points"], len(self.samples.points))

    def test_fetch_treats_missing_or_broken_image_as_none(self):
        from teukbo.http import FetchError

        with mock.patch.object(radar, "get_bytes", side_effect=FetchError("연결 끊김")):
            self.assertIsNone(radar.fetch_frame("202611130130"))
        with mock.patch.object(radar, "get_bytes", return_value=b"<html>error</html>"):
            self.assertIsNone(radar.fetch_frame("202611130130"))
        with mock.patch.object(radar, "get_bytes", return_value=FIXTURE.read_bytes()) as got:
            self.assertEqual(radar.fetch_frame("202611130020").rate_at(542, 291), 1)
        self.assertIn("RDR_CMP_WRC_202611130020.png", got.call_args[0][0])


class LiveIntegrationTest(unittest.TestCase):
    def test_failure_keeps_recent_value_with_old_check_time(self):
        now = kst(2026, 11, 13, 1, 45)
        prev = {"schema": 1, "rail_rain": {"available": True, "frame": "2026-11-13T01:25+09:00", "checked_at": "2026-11-13T01:33+09:00", "lines": [], "points": [], "error": None}}
        with mock.patch.object(radar, "collect", return_value=({"available": False, "checked_at": None, "error": "영상 없음"}, {"points": 1, "frames": {}})):
            rain, _ = live.collect_rail_rain(CFG, now, prev, None)
            self.assertEqual((rain["available"], rain["checked_at"], rain["error"]), (True, "2026-11-13T01:33+09:00", "영상 없음"))
            late, _ = live.collect_rail_rain(CFG, kst(2026, 11, 13, 2, 30), prev, None)  # 30분 넘게 못 받으면 싣지 않는다
            self.assertFalse(late["available"])

    def test_summary_line_mentions_rain(self):
        doc = {"warnings": {"available": False}, "obs": {"available": False}, "fcst": {"available": False},
               "rail_rain": {"available": True, "checked_at": "2026-11-13T01:33+09:00", "frame": "2026-11-13T01:25+09:00", "lines": [{"name": "전라선", "now": 15}, {"name": "호남선", "now": 0.0}]}}
        self.assertIn("선로 강우 01:33 확인(영상 01:25, 비 오는 노선 1개(최대 전라선 15mm/h 이상))", live.summary_line(doc))
