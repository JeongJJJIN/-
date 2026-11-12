"""지도용 지리 자료 만들기 (한 번만 실행해 결과를 web/geo/ 에 커밋한다. 정기 수집과 무관).

입력 (직접 내려받아 준비 — 출처와 받는 방법은 web/geo/README.md 참고)
  --zones     기상청 특보구역 경계 GeoJSON (mapshaper 로 단순화한 것; 속성 regId 가 특보구역 코드)
  --zones-full  같은 자료의 원본(단순화 전) — 역이 속한 구역을 정확히 찾는 데 사용
  --rail      OpenStreetMap Overpass 결과(JSON, 'out geom' 형식의 route=tracks relation 들)
  --stations  OpenStreetMap Overpass 결과(JSON, railway=station|halt)
  --codes     한국철도공사 역코드 CSV (공공데이터포털 15138467; 역코드, 역명, 역약어명 / cp949)

출력
  web/geo/zones.json     특보구역 다각형 {code: {n: 이름, c: [경도, 위도] 대표점, p: [[고리…]…]}}  (고리 = [경도,위도,경도,위도,…])
  web/geo/rail.json      철도 노선 [{name, fast, paths: [[경도,위도,…]…]}]
  web/geo/stations.json  역 [{code, name, lon, lat, zone}]  — 수집부(teukbo.rail)도 이 파일로 역 → 특보구역을 찾는다
  web/geo/lines.json     노선이 지나는 특보구역(순서대로)과 그 안의 역 [{name, fast, parts: [[구역 코드, [역 키…]], …]}]
  config/rail_samples.json   선로를 따라 약 1km 마다 찍은 표본점 [{name, samples: [[경도, 위도, 구역 코드], …]}] — 레이더 강우 읽기용

    python3 tools/build_geodata.py --zones wrn_land_8.geojson --zones-full wrnArea.geojson \\
        --rail lines_geom.json --stations stations.json --codes station_codes.csv
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from teukbo.zones import load_zone_table  # noqa: E402

ZONE_DECIMALS = 3  # 약 100m — 전국·도 단위 지도에 충분
RAIL_DECIMALS = 4
RAIL_TOLERANCE = 0.0006  # 노선 단순화 허용 오차(도) ≈ 60m
RAIL_DEDUPE_CELL = 0.0015  # 복선·측선처럼 나란한 선로를 하나로 보는 격자 크기(도) ≈ 150m


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"  {path.relative_to(ROOT)}: {path.stat().st_size / 1024:.0f} KB")


# ---------------------------------------------------------------- 특보구역


def ring_area(ring: list[list[float]]) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]))) / 2.0


def ring_centroid(ring: list[list[float]]) -> tuple[float, float]:
    a = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(a) < 1e-12:
        return ring[0][0], ring[0][1]
    return cx / (3 * a), cy / (3 * a)


def flat(ring: list[list[float]], decimals: int) -> list[float]:
    out: list[float] = []
    last = None
    for x, y in ring:
        p = (round(x, decimals), round(y, decimals))
        if p != last:
            out.extend(p)
            last = p
    return out


def zone_code(feature: dict) -> str | None:
    props = feature.get("properties") or {}
    return props.get("regId") or props.get("regid") or props.get("id")


def polygons_of(geometry: dict) -> list[list[list[list[float]]]]:
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    return []


def build_zones(path: Path, table) -> dict:
    leaves = set(table.leaf_codes(land_only=True))
    features = json.loads(path.read_text(encoding="utf-8"))["features"]
    zones: dict[str, dict] = {}
    for f in features:
        code = zone_code(f)
        if code not in leaves:
            continue
        polys = polygons_of(f["geometry"])
        biggest = max((poly[0] for poly in polys), key=ring_area)
        cx, cy = ring_centroid(biggest)
        rings = []
        for poly in polys:
            outer = flat(poly[0], ZONE_DECIMALS)
            if len(outer) >= 8:  # 점 4개 이상 (반올림으로 사라진 작은 섬은 제외)
                rings.append(outer)
        if not rings:
            rings = [flat(biggest, ZONE_DECIMALS)]
        zones[code] = {"n": table.name(code), "c": [round(cx, 3), round(cy, 3)], "p": rings}
    missing = sorted(leaves - set(zones))
    if missing:
        print(f"  ※ 경계가 없는 구역 {len(missing)}곳: {', '.join(table.name(c) for c in missing[:10])}")
    return dict(sorted(zones.items()))


def point_in_ring(x: float, y: float, ring: list[list[float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


class ZoneLocator:
    """좌표가 속한 특보구역(말단, 육상)을 찾는다. 경계 밖(매립지·교량 위 역 등)은 가장 가까운 구역."""

    def __init__(self, path: Path, table):
        leaves = set(table.leaf_codes(land_only=True))
        self.shapes: list[tuple[str, tuple[float, float, float, float], list]] = []
        for f in json.loads(path.read_text(encoding="utf-8"))["features"]:
            code = zone_code(f)
            if code not in leaves:
                continue
            polys = polygons_of(f["geometry"])
            xs = [p[0] for poly in polys for p in poly[0]]
            ys = [p[1] for poly in polys for p in poly[0]]
            self.shapes.append((code, (min(xs), min(ys), max(xs), max(ys)), polys))

    def center(self, code: str) -> tuple[float, float]:
        """구역의 대략적인 가운데(테두리 상자의 중심)."""
        x0, y0, x1, y1 = next(box for c, box, _ in self.shapes if c == code)
        return (x0 + x1) / 2, (y0 + y1) / 2

    def find(self, lon: float, lat: float) -> tuple[str | None, bool]:
        for code, (x0, y0, x1, y1), polys in self.shapes:
            if not (x0 <= lon <= x1 and y0 <= lat <= y1):
                continue
            for poly in polys:
                if point_in_ring(lon, lat, poly[0]) and not any(point_in_ring(lon, lat, hole) for hole in poly[1:]):
                    return code, True
        best, best_d = None, 1e9
        for code, _, polys in self.shapes:
            for poly in polys:
                for px, py in poly[0][::3]:
                    d = math.hypot((px - lon) * 0.82, py - lat)
                    if d < best_d:
                        best, best_d = code, d
        return (best, False) if best_d < 0.05 else (None, False)  # 약 5km 안에서만


# ---------------------------------------------------------------- 철도 노선


def simplify(points: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Douglas–Peucker."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy) or 1e-12
        far, far_i = 0.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            dist = abs((px - ax) * dy - (py - ay) * dx) / norm
            if dist > far:
                far, far_i = dist, i
        if far > tol:
            keep[far_i] = True
            stack.append((a, far_i))
            stack.append((far_i, b))
    return [p for p, k in zip(points, keep) if k]


def length(points: list[tuple[float, float]]) -> float:
    return sum(math.hypot((x2 - x1) * 0.82, y2 - y1) for (x1, y1), (x2, y2) in zip(points, points[1:]))


def cells_of(points: list[tuple[float, float]], cell: float) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        steps = max(1, int(max(abs(x2 - x1), abs(y2 - y1)) / (cell / 2)))
        for i in range(steps + 1):
            out.add((int((x1 + (x2 - x1) * i / steps) / cell), int((y1 + (y2 - y1) * i / steps) / cell)))
    return out


def join_ways(ways: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """끝점이 맞닿는 선로 조각을 이어 긴 선으로 만든다."""
    lines = [list(w) for w in ways if len(w) > 1]
    ends: dict[tuple[float, float], list[int]] = {}

    def key(p):
        return (round(p[0], 5), round(p[1], 5))

    changed = True
    while changed:
        changed = False
        ends = {}
        for i, line in enumerate(lines):
            if line is None:
                continue
            for end in (0, -1):
                k = key(line[end])
                other = ends.get(k)
                if other is not None and other[0] != i and lines[other[0]] is not None:
                    j, jend = other
                    a, b = lines[j], line
                    if jend == -1 and end == 0:
                        merged = a + b[1:]
                    elif jend == 0 and end == -1:
                        merged = b + a[1:]
                    elif jend == 0 and end == 0:
                        merged = b[::-1] + a[1:]
                    else:
                        merged = a + b[::-1][1:]
                    lines[j], lines[i] = merged, None
                    changed = True
                    break
                ends[k] = (i, end)
            if changed:
                break
    return [line for line in lines if line]


def build_rail(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for rel in data["elements"]:
        if rel.get("type") != "relation":
            continue
        name = rel.get("tags", {}).get("name", "")
        ways = [[(g["lon"], g["lat"]) for g in m["geometry"]] for m in rel.get("members", []) if m.get("type") == "way" and m.get("geometry")]
        raw_km = sum(length(w) for w in ways) * 111.2
        # 긴 조각부터 놓고, 이미 놓인 선과 거의 겹치는(나란한) 조각은 건너뛴다 → 복선이 한 줄로
        joined = sorted(join_ways(ways), key=length, reverse=True)
        covered: set[tuple[int, int]] = set()
        kept = []
        for line in joined:
            cells = cells_of(line, RAIL_DEDUPE_CELL)
            near = sum(1 for c in cells if any((c[0] + i, c[1] + j) in covered for i in (-1, 0, 1) for j in (-1, 0, 1)))
            if cells and near / len(cells) > 0.8:
                continue
            covered |= cells
            kept.append(line)
        paths = []
        for line in join_ways(kept):
            pts = simplify(line, RAIL_TOLERANCE)
            coords = flat([[x, y] for x, y in pts], RAIL_DECIMALS)
            if len(coords) >= 4 and length(pts) * 111.2 >= 0.3:
                paths.append(coords)
        kept_km = sum(length(list(zip(p[0::2], p[1::2]))) for p in paths) * 111.2
        out.append({"name": name, "fast": "고속" in name, "paths": paths})
        print(f"    {name:22s} 원본 선로 {raw_km:7.1f}km → {kept_km:6.1f}km, {len(paths):3d}개 선, 점 {sum(len(p) // 2 for p in paths)}개")
    return sorted(out, key=lambda r: r["name"])


# ---------------------------------------------------------------- 역


# 운행 기록에 쓰이는 역 이름 가운데 역코드표·지도 이름과 달라 따로 이어 줘야 하는 것 (운행 기록의 이름 → 역코드표의 이름)
RUN_NAME_ALIASES = {"판교(경기)": "신판교"}


def read_codes(path: Path) -> list[tuple[str, str, str]]:
    """(역코드, 역명, 역약어명) 목록 (공공데이터포털 한국철도공사 역코드 CSV). 한 코드에 이름이 여럿이거나 한 이름에 코드가 여럿일 수 있다."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    rows: list[tuple[str, str, str]] = []
    for line in text.splitlines()[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0] and parts[1]:
            rows.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    return rows


def build_stations(path: Path, code_rows: list[tuple[str, str, str]], locator: ZoneLocator) -> list[dict]:
    """한국철도공사 여객(간선) 역. 좌표는 OSM, 코드는 공식 역코드표에서 이름으로 연결한다.

    같은 이름의 역이 둘 이상이거나(판교: 서천/성남) 한 이름에 코드가 둘인 경우(울산: 옛 울산역/KTX 울산역)가 있어,
    후보 코드가 적은(확실한) 역부터 코드를 차지하게 한다. OSM 의 railway:ref(역 약칭)도 실마리로 쓴다.
    """
    import re

    by_name: dict[str, list[str]] = {}
    names_of: dict[str, list[str]] = {}
    for code, name, abbr in code_rows:
        names_of.setdefault(code, []).append(name)
        for key in (name, abbr):
            if key and code not in by_name.setdefault(key, []):
                by_name[key].append(code)

    def codes_for(names: list[str]) -> list[str]:
        out: list[str] = []
        for n in names:
            for code in by_name.get(n, []):
                if code not in out:
                    out.append(code)
        return out

    cands: list[dict] = []
    for e in json.loads(path.read_text(encoding="utf-8"))["elements"]:
        tags = e.get("tags", {})
        name = tags.get("name")
        operator = tags.get("operator")
        if not name or (operator and operator != "한국철도공사"):
            continue
        weak = not operator  # 운영자 표시가 없는 간이역 등 — 역코드표의 이름과 정확히 같을 때만 쓴다
        if weak and not (tags.get("railway") == "halt" or tags.get("train") == "yes"):
            continue
        kinds = tags.get("station", "")
        if kinds and "train" not in kinds:
            continue  # 전철 전용 역
        lon = e.get("lon") or (e.get("center") or {}).get("lon")
        lat = e.get("lat") or (e.get("center") or {}).get("lat")
        if lon is None or lat is None:
            continue
        base = name[:-1] if name.endswith("역") and len(name) > 2 else name
        plain = re.sub(r"[()\s]", "", base)  # 김천(구미) → 김천구미
        short = re.sub(r"\(.*?\)", "", base).strip()  # 진부(오대산) → 진부
        exact = [n for n in dict.fromkeys([tags.get("railway:ref"), base, "S" + base, plain]) if n]  # SRT 정차역은 'S수서' 처럼 적힌다
        cands.append({"base": base, "lon": round(lon, 4), "lat": round(lat, 4), "weak": weak, "exact": codes_for(exact),
                      "loose": codes_for([short]) if short and short != base else [], "got": []})

    stations: dict[str, dict] = {}

    def claim(cand: dict, codes: list[str]) -> None:
        zone, _exact = locator.find(cand["lon"], cand["lat"])
        for code in codes:
            if code in stations:
                continue
            names = names_of[code]
            # 표시 이름: 역코드표에 지도 이름(또는 SRT 표기)이 있으면 그것, 없으면 지도 이름
            shown = next((n for n in (cand["base"], "S" + cand["base"]) if n in names), cand["base"])
            alt = sorted({n for n in names if n != shown} | {run for run, ref in RUN_NAME_ALIASES.items() if ref in names})
            stations[code] = {"code": code, "name": shown, "lon": cand["lon"], "lat": cand["lat"], "zone": zone, **({"alt": alt} if alt else {})}
            cand["got"].append(code)

    strong = [c for c in cands if not c["weak"]]
    for cand in sorted(strong, key=lambda c: len(c["exact"]) or 99):  # 1) 이름이 정확히 맞는 역, 확실한 것부터
        claim(cand, cand["exact"])
    for cand in strong:  # 2) 괄호 안 별칭을 뗀 이름으로 (다른 역이 차지하지 않은 코드만)
        if not cand["got"]:
            claim(cand, cand["loose"])
    for cand in cands:  # 3) 운영자 표시가 없는 역
        if cand["weak"]:
            claim(cand, cand["exact"])
    unmatched: list[str] = []
    for cand in strong:
        if not cand["got"]:
            if any(st["name"] in (cand["base"], "S" + cand["base"]) and abs(st["lon"] - cand["lon"]) < 0.01 and abs(st["lat"] - cand["lat"]) < 0.01 for st in stations.values()):
                continue  # 같은 역이 지도에 두 번 그려져 있는 것(이미 실었다)
            # 역코드표(2024-09)보다 나중에 생긴 역: 코드 없이 이름으로만 싣는다(운행 기록의 역명으로 연결)
            unmatched.append(cand["base"])
            zone, _exact = locator.find(cand["lon"], cand["lat"])
            stations.setdefault("name:" + cand["base"], {"code": None, "name": cand["base"], "lon": cand["lon"], "lat": cand["lat"], "zone": zone})
    if unmatched:
        print(f"  ※ 역코드표에 없어 이름으로만 실은 역 {len(unmatched)}곳: {', '.join(sorted(set(unmatched)))}")
    return sorted(stations.values(), key=lambda s: (s["code"] or "~", s["name"]))


# ---------------------------------------------------------------- 노선이 지나는 특보구역·역 (노선별 특보 현황용)

LINE_SAMPLE_STEP = 0.01  # 노선을 따라 구역을 확인하는 간격(도) ≈ 1km
LINE_STATION_KM = 0.15  # 선로에서 이 거리 안의 역을 그 노선 위의 역으로 본다 (실제 역은 0.1km 안, 이웃 노선의 역은 0.5km 이상 떨어져 있다)
LINE_JOIN_KM = 5.0  # 끝점이 이만큼 가까운 선은 이어진 것으로 본다


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((a[0] - b[0]) * math.cos(math.radians((a[1] + b[1]) / 2)) * 111.2, (a[1] - b[1]) * 111.2)


def _chain(paths: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """끝점이 가까운 선끼리 이어 붙여 한 줄로 만든다(방향을 맞춰서). 이어지지 않는 선은 따로 남는다."""
    rest = sorted(paths, key=length, reverse=True)
    chains: list[list[tuple[float, float]]] = []
    while rest:
        chain = rest.pop(0)
        grown = True
        while grown and rest:
            grown = False
            best = None
            for i, cand in enumerate(rest):
                for at_tail, flip, d in ((True, False, _km(chain[-1], cand[0])), (True, True, _km(chain[-1], cand[-1])), (False, False, _km(chain[0], cand[-1])), (False, True, _km(chain[0], cand[0]))):
                    if d <= LINE_JOIN_KM and (best is None or d < best[0]):
                        best = (d, i, at_tail, flip)
            if best is not None:
                _, i, at_tail, flip = best
                cand = rest.pop(i)
                cand = cand[::-1] if flip else cand
                chain = chain + cand if at_tail else cand + chain
                grown = True
        chains.append(chain)
    return chains


def build_lines(rail: list[dict], stations: list[dict], locator: ZoneLocator) -> tuple[list[dict], list[dict]]:
    """노선마다, 지나가는 특보구역을 순서대로 적고 그 구역 안의 역을 붙인다. 함께, 선로를 따라 찍은 표본점도 돌려준다.

    표본점 [{name, samples: [[경도, 위도, 구역 코드], …]}] 은 레이더 영상에서 선로 위의 강우를 읽는 데 쓴다(teukbo.radar).

    [{name, fast, parts: [[구역 코드, [역 키…]], …]}] — 역 키는 화면(rail.js)과 같은 규칙: 역코드, 없으면 "name:역명".
    구역은 노선을 따라 약 1km 마다 확인하므로, 역이 없는 구역도 빠지지 않는다.
    """
    seen_place: dict[tuple[str, float, float], str] = {}
    places = []  # 같은 자리의 같은 이름(코드만 다른 역)은 한 번만
    for st in stations:
        label = st["name"][1:] if st["name"].startswith("S") and len(st["name"]) > 1 and "가" <= st["name"][1] <= "힣" else st["name"]
        spot = (label, round(st["lon"], 2), round(st["lat"], 2))
        if spot in seen_place:
            continue
        seen_place[spot] = st["code"] or f"name:{st['name']}"
        places.append((seen_place[spot], (st["lon"], st["lat"]), st["zone"]))

    out = []
    sample_out = []
    for line in rail:
        parts: list[list] = []
        points: list[list] = []
        for chain in _chain([list(zip(c[0::2], c[1::2])) for c in line["paths"]]):
            # 노선을 따라 표본점(누적 거리, 구역)
            samples: list[tuple[float, str]] = []
            along = 0.0
            cum = [0.0]
            for a, b in zip(chain[:-1], chain[1:]):
                seg = _km(a, b)
                n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / LINE_SAMPLE_STEP))
                for k in range(n):
                    t = k / n
                    lon, lat = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
                    zone, _exact = locator.find(lon, lat)
                    if zone:
                        samples.append((along + seg * t, zone))
                        points.append([round(lon, 3), round(lat, 3), zone])
                along += seg
                cum.append(along)
            runs: list[list] = []  # [구역, 시작 거리, 끝 거리, [역…]]
            for at, zone in samples:
                if runs and runs[-1][0] == zone:
                    runs[-1][2] = at
                else:
                    runs.append([zone, at, at, []])
            # 이 선에 붙은 역: 선분에 내린 수선의 발까지 거리와 누적 거리
            for key, pos, own_zone in places:
                best = None
                for i, (a, b) in enumerate(zip(chain[:-1], chain[1:])):
                    cos = math.cos(math.radians(pos[1]))
                    ax, ay, bx, by, px, py = a[0] * cos, a[1], b[0] * cos, b[1], pos[0] * cos, pos[1]
                    dx, dy = bx - ax, by - ay
                    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
                    d = math.hypot(px - (ax + dx * t), py - (ay + dy * t)) * 111.2
                    if best is None or d < best[0]:
                        best = (d, cum[i] + (cum[i + 1] - cum[i]) * t)
                if best is None or best[0] > LINE_STATION_KM:
                    continue
                # 역은 늘 자기 특보구역 아래에 적는다(구역 경계 근처에서는 선로 위의 가장 가까운 점이 옆 구역일 수 있다)
                same = [r for r in runs if r[0] == own_zone]
                if same:
                    run = min(same, key=lambda r: 0.0 if r[1] <= best[1] <= r[2] else min(abs(best[1] - r[1]), abs(best[1] - r[2])))
                else:
                    run = [own_zone, best[1], best[1], []]
                    runs.append(run)
                run[3].append((best[1], key))
            for zone, _start, _end, found in runs:
                keys = [k for _, k in sorted(found)]
                if parts and parts[-1][0] == zone:
                    parts[-1][1].extend(k for k in keys if k not in parts[-1][1])
                else:
                    parts.append([zone, keys])
        # 한 구역은 한 번만(노선이 구역을 드나들거나 선을 이어 붙인 자리에서 겹친다), 한 역도 한 번만
        merged: dict[str, list[str]] = {}
        for zone, keys in parts:
            bucket = merged.setdefault(zone, [])
            bucket.extend(k for k in keys if k not in bucket)
        used: set[str] = set()
        for zone in merged:
            merged[zone] = [k for k in merged[zone] if not (k in used or used.add(k))]
        # 읽기 좋게 노선의 긴 방향(동서 또는 남북)으로 늘어놓는다 — 북→남, 서→동
        spot = {key: pos for key, pos, _zone in places}
        centers = {zone: locator.center(zone) for zone in merged}
        lons = [c[0] for c in centers.values()]
        lats = [c[1] for c in centers.values()]
        east_west = (max(lons) - min(lons)) * math.cos(math.radians(sum(lats) / len(lats))) > (max(lats) - min(lats))
        axis = (lambda pos: pos[0]) if east_west else (lambda pos: -pos[1])
        parts = [[zone, sorted(merged[zone], key=lambda k: axis(spot[k]))] for zone in sorted(merged, key=lambda z: axis(centers[z]))]
        out.append({"name": line["name"], "fast": line["fast"], "parts": parts})
        sample_out.append({"name": line["name"], "samples": points})
        print(f"    {line['name']:22s} 구역 {len(parts):3d}개, 역 {sum(len(p[1]) for p in parts):3d}곳, 표본점 {len(points):4d}개")
    return out, sample_out


def main() -> int:
    parser = argparse.ArgumentParser(description="지도용 지리 자료 만들기")
    parser.add_argument("--zones", type=Path, required=True)
    parser.add_argument("--zones-full", type=Path, required=True)
    parser.add_argument("--rail", type=Path, required=True)
    parser.add_argument("--stations", type=Path, required=True)
    parser.add_argument("--codes", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "web" / "geo")
    args = parser.parse_args()

    table = load_zone_table()
    print("특보구역")
    zones = build_zones(args.zones, table)
    write(args.out / "zones.json", zones)
    print(f"  구역 {len(zones)}곳, 점 {sum(len(r) // 2 for z in zones.values() for r in z['p'])}개")

    print("철도 노선")
    rail = build_rail(args.rail)
    write(args.out / "rail.json", rail)

    print("역")
    stations = build_stations(args.stations, read_codes(args.codes), ZoneLocator(args.zones_full, table))
    write(args.out / "stations.json", stations)
    print("노선이 지나는 구역·역")
    lines, samples = build_lines(rail, stations, ZoneLocator(args.zones_full, table))
    write(args.out / "lines.json", lines)
    write(ROOT / "config" / "rail_samples.json", samples)  # 서버(teukbo.radar)만 쓴다 → 웹 폴더에 두지 않는다
    without = [s["name"] for s in stations if not s["zone"]]
    print(f"  역 {len(stations)}곳" + (f", 특보구역을 찾지 못한 역: {', '.join(without)}" if without else ", 모두 특보구역 연결됨"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
