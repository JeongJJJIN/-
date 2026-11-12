# web/geo — 지도용 지리 자료

철도 특보 지도(`rail.html`)가 읽는 파일입니다. 한 번 만들어 커밋해 둔 것이며 정기 수집과는 무관합니다.
구역이 다시 나뉘거나 노선·역이 새로 생기면 `tools/build_geodata.py` 로 다시 만듭니다.

| 파일 | 내용 | 원자료 |
|---|---|---|
| `zones.json` | 육상 특보구역 235곳의 경계 `{코드: {n: 이름, c: [경도, 위도], p: [[경도, 위도, …], …]}}` | 기상청 날씨누리 특보구역 경계 |
| `rail.json` | 철도 노선 `[{name, fast(고속선 여부), paths: [[경도, 위도, …], …]}]` | OpenStreetMap |
| `stations.json` | 역 `[{code(코레일 역코드, 없으면 null), name, lon, lat, zone(특보구역 코드)}]` | OpenStreetMap + 한국철도공사 역코드 |

`stations.json` 은 수집부(`teukbo/rail_run.py`)도 읽습니다 — 열차가 정차한 역이 어느 특보구역인지 찾는 데 씁니다.

## 출처와 이용 조건

- **특보구역 경계** — 기상청 날씨누리 지도가 쓰는 `https://www.weather.go.kr/wgis-nuri/js/info/wrnArea.geojson` (2026-06-01 기준 구역).
  육상의 가장 작은 구역만 남기고 mapshaper 로 꼭짓점을 약 8% 로 줄였습니다. 화면 표시용이라 실제 경계와 수백 m 다를 수 있습니다.
  파일 자체에 이용 조건 표시는 없어 국가기관이 공개한 자료로 보고 출처를 밝혀 씁니다.
  공식 배포 자료가 필요하면 기상청(공공데이터 제공신청)에 특보구역 경계 파일을 요청해 바꾸면 됩니다.
- **철도 노선·역 위치** — © OpenStreetMap 기여자, [ODbL 1.0](https://www.openstreetmap.org/copyright).
  가공한 자료(`rail.json`, `stations.json` 의 좌표)에도 같은 조건이 적용되며, 화면 하단에 출처를 표시합니다.
  Overpass API 로 받았습니다(2026-11-12).
  - 노선: `rel["type"="route"]["route"="tracks"]["network"="대한민국 국가철도"]` 가운데 여객열차가 다니는 노선(경부선, 호남선, 전라선, 경전선, 고속선 등 31개) — `out geom;`
  - 역: `nwr["railway"~"^(station|halt)$"]` (대한민국 영역) — `out tags center;`
- **역코드** — 한국철도공사 「역코드 정보」(공공데이터포털 15138467). 역 이름으로 OpenStreetMap 의 역과 짝지었습니다.
  역코드표에 아직 없는 새 역(예: 목포보성선의 강진·해남 등)은 `code` 가 `null` 이고 이름으로 연결합니다.

## 다시 만들기

```bash
python3 tools/build_geodata.py --zones wrn_land_8.geojson --zones-full wrnArea.geojson --rail lines_geom.json --stations stations.json --codes station_codes.csv
```

입력 파일은 위 출처에서 직접 받아 준비합니다(자세한 형식은 `tools/build_geodata.py` 맨 위 설명).
만든 뒤 `python3 -m unittest discover -s tests -t .` 로 구역 코드표(`config/warning_zones.json`)와 맞는지 확인합니다.
