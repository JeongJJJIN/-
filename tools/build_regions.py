"""config/regions.json 을 전국으로 채운다 — 기상청 특보구역(말단 구역) 하나가 지역 하나.

    python3 tools/build_regions.py          # config/regions.json 을 다시 쓴다
    python3 tools/build_regions.py --check  # 지금 파일이 이 도구가 만드는 것과 같은지만 본다

무엇을 근거로 만드나
- 구역 목록·상하 관계·이름: config/warning_zones.json (기상청 특보구역 코드표)
- 조회 지점(위경도): web/geo/zones.json 의 구역 중심점. 기상청 단기예보 격자(nx, ny)는 그 위경도에서 계산한다.
- 손으로 다듬어 둔 지역(전남 시·군: 시청·군청 위치, 시·군 단위로 묶은 특보구역)은 그대로 두고,
  그 지역들이 쓰지 않는 구역만 새로 만든다. 그래서 다시 돌려도 손으로 고친 값이 사라지지 않는다.

지역별로 달라지는 특보 기준(regions[].params → thresholds.json 의 region_param)
- 이름이 '산지'로 끝나는 구역: 강풍(주의보 17·25m/s, 경보 24·30m/s), 대설경보(30cm) 산지 기준
- 열대야주의보 밤 최저기온: 제주 27℃, 특별·광역시 26℃, 그 밖은 기본값 25℃.
  ※ 공식 기준은 '인구 50만 이상 도시·해안·도서'도 26℃인데 아직 구분하지 못했다(그만큼 열대야 가능성이 높게 나올 수 있다).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from teukbo.grid import latlon_to_grid  # noqa: E402

REGIONS = ROOT / "config" / "regions.json"
ZONE_TABLE = ROOT / "config" / "warning_zones.json"
ZONE_GEO = ROOT / "web" / "geo" / "zones.json"
NATION = "L1000000"

# 도·광역시(코드표의 최상위 구역) → 화면에 적을 짧은 이름, 통보문에서 그 구역을 가리키는 이름들.
# '광주'는 경기도 광주시와 겹치므로 광역시는 정식 이름만 둔다.
GROUPS: dict[str, tuple[str, list[str]]] = {
    "L1010000": ("경기", ["경기도"]),
    "L1014000": ("인천", ["서해5도"]),
    "L1020000": ("강원", ["강원도", "강원특별자치도"]),
    "L1030000": ("충남", ["충청남도", "충남"]),
    "L1040000": ("충북", ["충청북도", "충북"]),
    "L1060000": ("전북", ["전북자치도", "전북특별자치도", "전라북도", "전북"]),
    "L1070000": ("경북", ["경상북도", "경북"]),
    "L1080000": ("경남", ["경상남도", "경남"]),
    "L1090000": ("제주", ["제주도", "제주특별자치도"]),
    "L1100000": ("서울", ["서울", "서울특별시"]),
    "L1110000": ("인천", ["인천", "인천광역시"]),
    "L1120000": ("대전", ["대전", "대전광역시"]),
    "L1130000": ("광주", ["광주광역시"]),
    "L1140000": ("대구", ["대구", "대구광역시"]),
    "L1150000": ("부산", ["부산", "부산광역시"]),
    "L1160000": ("울산", ["울산", "울산광역시"]),
    "L1170000": ("세종", ["세종", "세종특별자치시"]),
    "L1600000": ("경북", ["울릉도.독도"]),
}
METRO = {"L1100000", "L1110000", "L1120000", "L1130000", "L1140000", "L1150000", "L1160000"}  # 특별·광역시
MOUNTAIN = {"strong_wind_min": 17, "strong_gust_min": 25, "strong_wind_warning_min": 24, "strong_gust_warning_min": 30, "heavy_snow_warning_min": 30}

DOC = [
    "대상 지역 목록(전국). tools/build_regions.py 가 기상청 특보구역 코드표에서 만든다 — 구역이 개편되면 그 도구를 다시 돌린다.",
    "손으로 다듬은 지역(전남 시·군)은 도구가 건드리지 않는다. 지역을 직접 추가·수정해도 된다(코드 수정 불필요).",
    "lat/lon: 조회 지점. 전남 시·군은 시청·군청(벌교는 읍 소재지), 그 밖은 특보구역의 중심점. Open-Meteo 조회 지점으로 쓴다.",
    "nx/ny: 기상청 단기예보 격자. `python -m teukbo.grid <위도> <경도>` 로 계산한다(테스트가 위경도와 일치하는지 검사).",
    "warning_zones: 이 지역에 해당하는 기상청 특보구역(코드·이름). 세분된 구역은 상위 구역(시·군)도 함께 적는다.",
    "  그중 한 곳이라도 특보가 발효되면 그 지역의 특보로 표시한다.",
    "group: zone_groups 의 키(도·광역시). 도 전체에 내려진 특보를 이 지역에 연결하고, 화면에 '경기'처럼 적는 데 쓴다.",
    "params: 지역별로 다른 특보 기준. tropical_night_min(열대야 밤 최저기온 ℃), 산지 구역의 강풍·대설경보 기준. 없으면 thresholds.json 의 기본값.",
    "  ※ 열대야 기준은 제주 27·특별광역시 26·그 밖 25로 넣었다. 공식 기준의 '인구 50만 이상 도시·해안·도서 26'은 아직 구분하지 못했다(전남은 반영).",
]


def build() -> dict:
    current = json.loads(REGIONS.read_text(encoding="utf-8"))
    table = {z["code"]: z for z in json.loads(ZONE_TABLE.read_text(encoding="utf-8"))["zones"]}
    geo = json.loads(ZONE_GEO.read_text(encoding="utf-8"))
    parents = {z["up"] for z in table.values() if z["up"]}

    kept = [r for r in current["regions"] if not r.get("generated")]
    taken = {z["code"] for r in kept for z in r["warning_zones"]}
    groups = {key: value for key, value in current.get("zone_groups", {}).items() if any(r.get("group") == key for r in kept)}

    def chain(code: str) -> list[str]:
        """말단 구역에서 도·광역시 바로 아래까지: [말단, 상위 시·군…], 그리고 도·광역시 코드"""
        out = [code]
        while table[out[-1]]["up"] not in (None, NATION):
            out.append(table[out[-1]]["up"])
        return out

    made = []
    for code in sorted(c for c in geo if c.startswith("L") and c not in parents and c not in taken):
        zone = table[code]
        path = chain(code)
        top = path[-1]  # 도·광역시(코드표의 최상위 구역). 말단 구역이 곧 최상위인 곳(울릉도.독도 등)은 자기 자신
        if top not in GROUPS:
            raise SystemExit(f"{code} {zone['name']}: GROUPS 에 없는 상위 구역 {top} {table[top]['name']}")
        own = path if len(path) == 1 else path[:-1]  # 이 지역의 구역: 말단 + 그 위의 시·군(도·광역시는 group 이 맡는다)
        short, names = GROUPS[top]
        groups.setdefault(top, {"name": short, "code": top, "names": names})
        lon, lat = geo[code]["c"]
        nx, ny = latlon_to_grid(lat, lon)
        params: dict[str, float] = {}
        if top == "L1090000":
            params["tropical_night_min"] = 27
        elif top in METRO:
            params["tropical_night_min"] = 26
        if zone["name"].endswith("산지"):
            params.update(MOUNTAIN)
        region = {"id": code, "name": zone["full"], "group": top, "lat": round(lat, 4), "lon": round(lon, 4), "nx": nx, "ny": ny,
                  "warning_zones": [{"code": c, "name": table[c]["name"]} for c in own], "generated": True}
        if params:
            region["params"] = params
        made.append(region)

    sources = dict(current.get("sources", {}))
    sources["generated"] = "tools/build_regions.py — config/warning_zones.json(특보구역 코드표)과 web/geo/zones.json(구역 중심점)"
    return {"_doc": DOC, "sources": sources, "zone_groups": groups, "regions": kept + made}


def dump(doc: dict) -> str:
    head = json.dumps({k: v for k, v in doc.items() if k != "regions"}, ensure_ascii=False, indent=2)
    rows = ",\n".join("    " + json.dumps(r, ensure_ascii=False) for r in doc["regions"])
    return head[:-2] + ',\n  "regions": [\n' + rows + "\n  ]\n}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="전국 지역 목록(config/regions.json) 만들기")
    parser.add_argument("--check", action="store_true", help="파일을 쓰지 않고, 지금 파일과 같은지만 확인")
    args = parser.parse_args()
    text = dump(build())
    if args.check:
        same = REGIONS.read_text(encoding="utf-8") == text
        print("같습니다" if same else "다릅니다 — tools/build_regions.py 를 다시 돌리세요")
        return 0 if same else 1
    REGIONS.write_text(text, encoding="utf-8")
    doc = json.loads(text)
    print(f"지역 {len(doc['regions'])}곳 (손으로 다듬은 지역 {sum(1 for r in doc['regions'] if not r.get('generated'))}곳 포함), 도·광역시 {len(doc['zone_groups'])}곳")
    return 0


if __name__ == "__main__":
    sys.exit(main())
