"""선로를 따라 본 레이더 강우 — 기상청 레이더 합성영상을 직접 읽어, 철도 노선 위의 강수 강도를 구한다.

기상청은 5분마다 전국 레이더 합성영상(강수 강도 mm/h, 635×620 PNG)을 공개한다.
이 모듈은 그 그림의 화소 색을 범례의 강수 강도 구간으로 되돌리고, 선로를 따라 약 1km 마다 찍어 둔 표본점
(config/rail_samples.json)에서 값을 읽어 '지금 어느 노선의 어느 구역에 얼마나 센 비가 오는지'와
'최근 1시간 동안 얼마나 왔는지(추정)'를 계산한다.

정확도에 관한 사실
- 값은 관측 우량이 아니라 레이더 추정 강수 강도이며, 그림의 색 구간(예: 5~6mm/h)으로만 알 수 있다.
  → 강도는 구간의 아래 값(lo)과 위 값(hi)으로 싣고, 1시간 누적은 구간 가운데 값으로 더한 '추정'이다.
- 영상 좌표: 람베르트 정각원추도법(표준위도 30°·60°, 기준점 126°E 38°N), 1화소 = 1.708km.
  영상 속 해안선에 맞춰 구한 값이며(98.5%가 1화소 이내), 화면(web/live.js)의 값과 같아야 한다(테스트가 대조).
- 표본점마다 둘레 1화소(약 1.7km)까지에서 가장 센 값을 쓴다 — 위치 오차와, 그림의 해안선·경위선이 비구름을 가리는 것을 감안.
"""

from __future__ import annotations

import logging
import math
import struct
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import CONFIG_DIR
from .http import FetchError, get_bytes
from .timeutil import KST, iso

log = logging.getLogger("teukbo.radar")

URL = "https://www.kma.go.kr/repositary/image/rdr/img/RDR_CMP_WRC_{stamp}.png"
SAMPLES_FILE = CONFIG_DIR / "rail_samples.json"

# 영상의 지도 좌표 (web/live.js 의 RADAR_IMAGE 와 같은 값)
KM_PER_PX, X0, Y0, WIDTH, HEIGHT = 1.708, 258.0, 169.75, 635, 620
FRAME = (1, 21, 598, 618)  # 지도 부분의 화소 범위 (x0, y0, x1, y1) — 제목 줄과 오른쪽 범례는 뺀다
_RE, _SLAT1, _SLAT2, _OLON, _OLAT = 6371.00877, 30.0, 60.0, 126.0, 38.0

# 범례: (강수 강도 구간의 아래 값 mm/h, 색). 영상 오른쪽 범례에서 읽은 값 (web/live.js 의 RADAR_SCALE 과 같아야 한다)
SCALE: list[tuple[float, tuple[int, int, int]]] = [
    (0.0, (0, 200, 255)), (0.1, (0, 155, 245)), (0.5, (0, 74, 245)), (1, (0, 255, 0)), (2, (0, 190, 0)), (3, (0, 140, 0)), (4, (0, 90, 0)),
    (5, (255, 255, 0)), (6, (255, 220, 31)), (7, (249, 205, 0)), (8, (224, 185, 0)), (9, (204, 170, 0)), (10, (255, 102, 0)), (15, (255, 50, 0)),
    (20, (210, 0, 0)), (25, (180, 0, 0)), (30, (224, 169, 255)), (40, (201, 105, 255)), (50, (179, 41, 255)), (60, (147, 0, 228)),
    (70, (179, 180, 222)), (90, (76, 78, 177)), (110, (0, 3, 144)), (150, (51, 51, 51)),
]
_RATE_OF = {color: lo for lo, color in SCALE}
_BOUNDS = [lo for lo, _ in SCALE]
RAIN_MIN = 0.1  # 이 값 이상을 '비'로 본다 (0.0~0.1mm/h 구간은 약한 에코)
FRAMES_PER_HOUR = 12


def upper_bound(lo: float) -> float | None:
    """구간의 위 값. 맨 끝 구간(150mm/h 이상)은 None."""
    i = _BOUNDS.index(lo)
    return _BOUNDS[i + 1] if i + 1 < len(_BOUNDS) else None


def midpoint(lo: float) -> float:
    hi = upper_bound(lo)
    return lo if hi is None else (lo + hi) / 2.0


# ---------------------------------------------------------------- PNG 읽기 (표준 라이브러리만)


def decode_png(data: bytes) -> tuple[int, int, int, bytes]:
    """PNG → (너비, 높이, 화소당 바이트 수, 화소 바이트). 8비트 RGB·RGBA, 비월 주사가 아닌 것만 다룬다(기상청 영상의 형식)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG 가 아닙니다")
    pos, width, height, bpp, idat = 8, 0, 0, 0, []
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, color, _comp, _filter, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or color not in (2, 6) or interlace != 0:
                raise ValueError(f"다루지 않는 PNG 형식입니다 (비트 {depth}, 색 {color}, 비월 {interlace})")
            bpp = 3 if color == 2 else 4
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + length
    if not width or not idat:
        raise ValueError("PNG 에 영상 자료가 없습니다")
    raw = zlib.decompress(b"".join(idat))
    stride = width * bpp
    if len(raw) != (stride + 1) * height:
        raise ValueError("PNG 자료 길이가 맞지 않습니다")
    out = bytearray(stride * height)
    prev = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        row = bytearray(raw[start + 1:start + 1 + stride])
        if kind == 1:  # Sub
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 255
        elif kind == 2:  # Up
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 255
        elif kind == 3:  # Average
            for i in range(stride):
                row[i] = (row[i] + (((row[i - bpp] if i >= bpp else 0) + prev[i]) >> 1)) & 255
        elif kind == 4:  # Paeth
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                row[i] = (row[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        elif kind != 0:
            raise ValueError(f"알 수 없는 PNG 필터 {kind}")
        out[y * stride:(y + 1) * stride] = row
        prev = row
    return width, height, bpp, bytes(out)


class Frame:
    """레이더 영상 한 장: 화소 → 강수 강도 구간의 아래 값(mm/h). 비구름이 아닌 화소는 None."""

    def __init__(self, data: bytes):
        self.width, self.height, self.bpp, self.pixels = decode_png(data)
        if (self.width, self.height) != (WIDTH, HEIGHT):
            raise ValueError(f"레이더 영상 크기가 바뀌었습니다: {self.width}x{self.height} (기대 {WIDTH}x{HEIGHT}) — 위치 맞춤을 다시 해야 합니다")

    def rate_at(self, x: int, y: int) -> float | None:
        if not (FRAME[0] <= x <= FRAME[2] and FRAME[1] <= y <= FRAME[3]):
            return None
        i = (y * self.width + x) * self.bpp
        return _RATE_OF.get((self.pixels[i], self.pixels[i + 1], self.pixels[i + 2]))

    def rate_near(self, x: float, y: float) -> float:
        """그 점과 둘레 1화소에서 가장 센 강수 강도(구간의 아래 값). 비구름이 없으면 0."""
        cx, cy = int(round(x)), int(round(y))
        best = 0.0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                rate = self.rate_at(cx + dx, cy + dy)
                if rate is not None and rate > best:
                    best = rate
        return best

    def legend_ok(self) -> bool:
        """영상 오른쪽의 범례가 알고 있는 색 그대로인지(색이 바뀌면 값을 잘못 읽게 된다)."""
        for k, (_lo, color) in enumerate(reversed(SCALE)):
            i = ((21 + 24 * k + 12) * self.width + 603) * self.bpp
            if tuple(self.pixels[i:i + 3]) != color:
                return False
        return True


# ---------------------------------------------------------------- 좌표


def project(lon: float, lat: float) -> tuple[float, float]:
    """경위도 → 영상의 화소 좌표 (web/live.js 의 radarProject 와 같은 식)."""
    d = math.pi / 180.0
    s1, s2, olon, olat = _SLAT1 * d, _SLAT2 * d, _OLON * d, _OLAT * d
    sn = math.log(math.cos(s1) / math.cos(s2)) / math.log(math.tan(math.pi / 4 + s2 / 2) / math.tan(math.pi / 4 + s1 / 2))
    sf = math.tan(math.pi / 4 + s1 / 2) ** sn * math.cos(s1) / sn
    ro = _RE * sf / math.tan(math.pi / 4 + olat / 2) ** sn
    ra = _RE * sf / math.tan(math.pi / 4 + lat * d / 2) ** sn
    theta = (lon * d - olon) * sn
    return X0 + ra * math.sin(theta) / KM_PER_PX, Y0 - (ro - ra * math.cos(theta)) / KM_PER_PX


class RailSamples:
    """선로 표본점: 노선 이름, 경위도, 특보구역, 영상 화소 좌표."""

    def __init__(self, lines: Sequence[Mapping[str, Any]]):
        self.points: list[tuple[int, float, float, str, float, float]] = []  # (노선 번호, 경도, 위도, 구역, x, y)
        self.names: list[str] = []
        for index, line in enumerate(lines):
            self.names.append(str(line["name"]))
            for lon, lat, zone in line["samples"]:
                x, y = project(lon, lat)
                self.points.append((index, lon, lat, zone, x, y))

    @classmethod
    def load(cls, path: Path = SAMPLES_FILE) -> "RailSamples":
        import json

        return cls(json.loads(path.read_text(encoding="utf-8")))

    def read(self, frame: Frame) -> dict[int, float]:
        """영상에서 표본점마다 강수 강도를 읽는다. 비가 있는 점만 {표본점 번호: 구간의 아래 값}."""
        out: dict[int, float] = {}
        for i, (_line, _lon, _lat, _zone, x, y) in enumerate(self.points):
            rate = frame.rate_near(x, y)
            if rate >= RAIN_MIN:
                out[i] = rate
        return out


# ---------------------------------------------------------------- 영상 받기


def stamp_of(t: datetime) -> str:
    t = t.astimezone(KST)
    return t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0).strftime("%Y%m%d%H%M")


def time_of(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=KST)


def fetch_frame(stamp: str, http_opts: Mapping[str, Any] | None = None) -> Frame | None:
    """그 시각의 영상. 아직 없거나 받지 못하면 None.

    아직 올라오지 않은 시각을 물으면 서버가 404 대신 연결을 끊거나 안내 문서를 주기도 한다 → 모두 '없음'으로 본다.
    (모든 후보 시각이 없으면 부르는 쪽에서 오류로 처리한다.)
    """
    opts = http_opts or {}
    try:
        data = get_bytes(URL.format(stamp=stamp), None, timeout=float(opts.get("timeout_seconds", 20)), retries=0, label=f"레이더 {stamp}")
        return Frame(data)
    except FetchError as e:
        log.debug("레이더 %s 없음: %s", stamp, e)
        return None
    except (ValueError, zlib.error) as e:
        log.debug("레이더 %s 읽을 수 없음: %s", stamp, e)
        return None


def latest_stamp(now: datetime, known: Sequence[str], http_opts: Mapping[str, Any] | None = None, tries: int = 7) -> tuple[str, Frame | None] | None:
    """가장 최근 영상의 시각. 이미 가진 시각(known)이면 다시 받지 않는다. 반환: (시각, 새로 받은 영상 또는 None)"""
    base = time_of(stamp_of(now))
    for back in range(tries):
        stamp = (base - timedelta(minutes=5 * back)).strftime("%Y%m%d%H%M")
        if stamp in known:
            return stamp, None
        frame = fetch_frame(stamp, http_opts)
        if frame is not None:
            return stamp, frame
    return None


# ---------------------------------------------------------------- 노선별 집계


def collect(now: datetime, state: Mapping[str, Any] | None, samples: RailSamples, http_opts: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """선로 강우 현황과, 다음 실행에 넘길 상태(최근 1시간의 표본점별 값)를 만든다.

    state: {"frames": {영상 시각: {표본점 번호(문자열): 강수 강도}}} — 이미 읽은 영상은 다시 받지 않는다.
    반환: (live.json 에 실을 rail_rain, 새 상태)
    """
    frames: dict[str, dict[str, float]] = dict((state or {}).get("frames") or {}) if (state or {}).get("points") == len(samples.points) else {}
    try:
        found = latest_stamp(now, list(frames), http_opts)
        if found is None:
            raise FetchError("최근 35분 안의 레이더 영상을 찾지 못했습니다")
        latest, frame = found
        if frame is not None:
            if not frame.legend_ok():
                raise FetchError("레이더 영상의 범례 색이 달라졌습니다 — 값을 읽지 않습니다")
            frames[latest] = {str(i): rate for i, rate in samples.read(frame).items()}
        # 최근 1시간(12장) 가운데 아직 없는 영상을 채운다 (처음 한 번만 여러 장을 받는다)
        wanted = [(time_of(latest) - timedelta(minutes=5 * k)).strftime("%Y%m%d%H%M") for k in range(FRAMES_PER_HOUR)]
        for stamp in wanted[1:]:
            if stamp not in frames:
                older = fetch_frame(stamp, http_opts)
                if older is not None and older.legend_ok():
                    frames[stamp] = {str(i): rate for i, rate in samples.read(older).items()}
    except (FetchError, ValueError) as e:
        log.error("레이더 영상 읽기 실패: %s", e)
        return {"available": False, "checked_at": None, "error": str(e)}, {"points": len(samples.points), "frames": frames}

    frames = {stamp: frames[stamp] for stamp in wanted if stamp in frames}
    return build_result(samples, frames, latest, now), {"points": len(samples.points), "frames": frames}


def build_result(samples: RailSamples, frames: Mapping[str, Mapping[str, float]], latest: str, now: datetime) -> dict[str, Any]:
    """표본점별 값(최근 1시간의 영상들)을 노선별 현황으로 모은다. frames: {영상 시각: {표본점 번호(문자열): 강수 강도}}"""
    now_rates = {int(i): rate for i, rate in frames[latest].items()}
    acc: dict[int, float] = {}  # 표본점별 최근 1시간 추정 누적(mm) — 5분 영상 한 장은 1/12 시간
    for values in frames.values():
        for i, rate in values.items():
            acc[int(i)] = acc.get(int(i), 0.0) + midpoint(rate) / FRAMES_PER_HOUR

    lines: list[dict[str, Any]] = []
    for index, name in enumerate(samples.names):
        parts: dict[str, list[float]] = {}  # 구역 → [지금 최대, 1시간 누적 최대]
        top: tuple[float, float, float, str] | None = None
        for i, (line, lon, lat, zone, _x, _y) in enumerate(samples.points):
            if line != index:
                continue
            rate, total = now_rates.get(i, 0.0), acc.get(i, 0.0)
            if rate < RAIN_MIN and total < 0.05:
                continue
            slot = parts.setdefault(zone, [0.0, 0.0])
            slot[0], slot[1] = max(slot[0], rate), max(slot[1], total)
            if rate >= RAIN_MIN and (top is None or rate > top[0]):
                top = (rate, lon, lat, zone)
        if not parts:
            continue
        entry: dict[str, Any] = {"name": name, "now": top[0] if top else 0.0, "acc60": round(max(v[1] for v in parts.values()), 1),
                                 "parts": [[zone, v[0], round(v[1], 1)] for zone, v in parts.items()]}
        if top:
            entry["hi"] = upper_bound(top[0])
            entry["at"] = [top[1], top[2]]
            entry["zone"] = top[3]
        lines.append(entry)
    lines.sort(key=lambda e: (-e["now"], -e["acc60"], e["name"]))
    points = [[samples.points[i][1], samples.points[i][2], rate] for i, rate in sorted(now_rates.items())]
    return {
        "available": True,
        "frame": iso(time_of(latest)),
        "checked_at": iso(now),
        "frames_1h": len(frames),
        "error": None,
        "lines": lines,
        "points": points,
    }
