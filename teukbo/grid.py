"""위경도 → 기상청 단기예보 격자(nx, ny) 변환.

기상청 단기예보 조회서비스 활용가이드의 Lambert Conformal Conic 변환식(5km 격자)을 그대로 옮긴 것이다.
regions.json 의 nx, ny 값을 검증하거나 새 지역을 추가할 때 사용한다.

    python -m teukbo.grid 34.9507 127.4872
"""

from __future__ import annotations

import math
import sys

_RE = 6371.00877  # 지구 반경(km)
_GRID = 5.0  # 격자 간격(km)
_SLAT1 = 30.0  # 표준 위도 1
_SLAT2 = 60.0  # 표준 위도 2
_OLON = 126.0  # 기준점 경도
_OLAT = 38.0  # 기준점 위도
_XO = 43  # 기준점 X좌표(격자)
_YO = 136  # 기준점 Y좌표(격자)


def latlon_to_grid(lat: float, lon: float) -> tuple[int, int]:
    degrad = math.pi / 180.0
    re = _RE / _GRID
    slat1 = _SLAT1 * degrad
    slat2 = _SLAT2 * degrad
    olon = _OLON * degrad
    olat = _OLAT * degrad

    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5)
    sf = math.pow(sf, sn) * math.cos(slat1) / sn
    ro = math.tan(math.pi * 0.25 + olat * 0.5)
    ro = re * sf / math.pow(ro, sn)

    ra = math.tan(math.pi * 0.25 + lat * degrad * 0.5)
    ra = re * sf / math.pow(ra, sn)
    theta = lon * degrad - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn

    nx = math.floor(ra * math.sin(theta) + _XO + 0.5)
    ny = math.floor(ro - ra * math.cos(theta) + _YO + 0.5)
    return int(nx), int(ny)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("사용법: python -m teukbo.grid <위도> <경도>")
    print("nx=%d ny=%d" % latlon_to_grid(float(sys.argv[1]), float(sys.argv[2])))
