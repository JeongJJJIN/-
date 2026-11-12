"""기상청 레이더 합성영상(RDR_CMP_WRC, 635x620)의 화소 ↔ 경위도 대응을 맞춘다. (한 번만 쓰는 도구, numpy·opencv 필요)

    python3 tools/radar_calibrate.py <레이더 영상.png> <시도 경계.geojson> <확인용 그림.png>

결과(축척 km/화소, 기준점의 화소 위치)를 web/live.js 의 RADAR_IMAGE 에 적는다. 기상청이 영상 크기·범위를 바꾸면 다시 맞춘다.

가정: 람베르트 정각원추도법(표준위도 30°·60°, 기준경도 126°, 기준위도 38°) — 기상청 레이더·동네예보가 쓰는 도법.
미지수: 축척(km/화소)과 원점의 화소 위치. 영상의 검은 해안선·시도 경계에 시도 경계 자료가 가장 잘 겹치는 값을 찾는다.
"""
import json, math, sys
import numpy as np
import cv2

img_path, sido_path, out_png = sys.argv[1:4]
img = cv2.imread(img_path)
h, w = img.shape[:2]
black = (img.max(axis=2) < 70).astype(np.uint8)          # 해안선·경계·격자 점선·글자
dist = cv2.distanceTransform(1 - black, cv2.DIST_L2, 3)  # 각 화소에서 가장 가까운 검은 화소까지의 거리

RE, SLAT1, SLAT2, OLON, OLAT = 6371.00877, 30.0, 60.0, 126.0, 38.0
def lcc(lon, lat, slat1=SLAT1, slat2=SLAT2, olon=OLON, olat=OLAT):
    d = math.pi / 180
    s1, s2 = slat1 * d, slat2 * d
    sn = np.log(np.cos(s1) / np.cos(s2)) / np.log(np.tan(np.pi / 4 + s2 / 2) / np.tan(np.pi / 4 + s1 / 2))
    sf = np.tan(np.pi / 4 + s1 / 2) ** sn * np.cos(s1) / sn
    ro = RE * sf / np.tan(np.pi / 4 + olat * d / 2) ** sn
    ra = RE * sf / np.tan(np.pi / 4 + np.asarray(lat) * d / 2) ** sn
    theta = (np.asarray(lon) - olon) * d * sn
    return ra * np.sin(theta), ro - ra * np.cos(theta)   # 동쪽 +x km, 북쪽 +y km

pts = []
for f in json.load(open(sido_path))["features"]:
    g = f["geometry"]
    polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
    for poly in polys:
        for ring in poly:
            ring = np.asarray(ring)
            # 변을 따라 촘촘히(약 1km) 점을 찍는다
            for a, b in zip(ring[:-1], ring[1:]):
                n = max(1, int(np.hypot(*(b - a)) * 100))
                for k in range(n):
                    pts.append(a + (b - a) * k / n)
pts = np.asarray(pts)
xk, yk = lcc(pts[:, 0], pts[:, 1])
print("points", len(pts))

def score(s, x0, y0):
    px = x0 + xk / s
    py = y0 - yk / s
    ok = (px >= 1) & (px < w - 40) & (py >= 20) & (py < h - 1)   # 범례·제목 영역 제외
    if ok.sum() < len(pts) * 0.8:
        return 1e9
    d = dist[py[ok].astype(int), px[ok].astype(int)]
    return float(np.minimum(d, 6).mean())

best = (1e9, None)
for s in np.arange(1.2, 3.2, 0.04):
    for x0 in range(150, 450, 4):
        for y0 in range(100, 400, 4):
            v = score(s, x0, y0)
            if v < best[0]:
                best = (v, (s, x0, y0))
print("coarse", best)
s0, x0c, y0c = best[1]
for step_s, step_p, span_s, span_p in ((0.005, 1.0, 0.06, 6), (0.001, 0.25, 0.01, 1.5)):
    v0, (s0, x0c, y0c) = best
    for s in np.arange(s0 - span_s, s0 + span_s + 1e-9, step_s):
        for x0 in np.arange(x0c - span_p, x0c + span_p + 1e-9, step_p):
            for y0 in np.arange(y0c - span_p, y0c + span_p + 1e-9, step_p):
                v = score(s, x0, y0)
                if v < best[0]:
                    best = (v, (float(s), float(x0), float(y0)))
    print("refined", best)
v, (s, x0, y0) = best
px = x0 + xk / s; py = y0 - yk / s
d = dist[np.clip(py.astype(int), 0, h - 1), np.clip(px.astype(int), 0, w - 1)]
print("within 1px: %.1f%%, within 2px: %.1f%%, median %.2f px" % ((d <= 1.01).mean() * 100, (d <= 2.01).mean() * 100, float(np.median(d))))
vis = img.copy()
for x, y in zip(px.astype(int), py.astype(int)):
    if 0 <= x < w and 0 <= y < h:
        vis[y, x] = (0, 0, 255)
big = cv2.resize(vis, (w * 2, h * 2), interpolation=cv2.INTER_NEAREST)
cv2.imwrite(out_png, big)
print(json.dumps({"km_per_px": round(s, 4), "x0": round(x0, 2), "y0": round(y0, 2), "width": w, "height": h}))
