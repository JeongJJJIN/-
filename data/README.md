# data/

| 경로 | 내용 | 저장소 커밋 |
|---|---|---|
| `history/YYYY-MM-DD.json` | 그날 수집한 회차별 예측값(모델·지표별 극값), 계산 결과(가능성·등급), 실제 특보·예비특보 기록 | **예** — 지우지 않습니다 (2단계 분석용) |
| `accuracy.json` | 이력으로 집계한 적중률(사전 포착률·오경보율) | 예 |
| `rail/days/YYYY-MM-DD.json` | 그날 열차별 종착 지연과 지나간 구간의 특보(코레일 공식 운행 기록 기준), 노선·특보별 집계 | **예** — 지우지 않습니다 |
| `rail/events/YYYY-MM.json` | 특보 발표·해제 이력 보관분(공식 조회가 약 60일까지만 되므로 받아 둠) | 예 |
| `rail/summary.json` | 열차 지연 누적 통계 — 웹에는 `data/rail.json` 으로 배포 | 예 |
| `latest.json` | 웹이 처음 읽는 최신 결과(전체 지역 요약) | 아니오 — 배포 전용 |
| `warnings.json` | 전국 특보·예비특보(특보구역 코드 단위) — 철도 특보 지도가 읽음 | 아니오 — 배포 전용 |
| `detail/<지역>.json` | 지역별 모델 비교 표·그래프 자료 | 아니오 — 배포 전용 |

`latest.json` 과 `detail/` 은 3시간마다 통째로 바뀌는 파일이라 커밋하면 저장소가 빠르게 커집니다.
그래서 GitHub Pages 에만 올리고(`.gitignore` 처리) 저장소에는 누적 가치가 있는 이력만 남깁니다.
배포된 최신 파일은 `https://<계정>.github.io/<저장소>/data/latest.json` 에서 볼 수 있습니다.

## history 파일 형식

```jsonc
{
  "schema": 1,
  "date": "2026-11-12",
  "runs": [
    {
      "t": "2026-11-12T21:25+09:00",            // 수집 시각(KST)
      "ok": {"open_meteo": true, "kma_forecast": true, "kma_warnings": true},
      "models": ["kma_dfs", "ecmwf_ifs025", "…"], // regions[].h[].r 의 순서
      "ind": ["apparent_max", "gust", "…"],        // regions[].x[모델] 의 순서
      "thresholds": "2026-06-30",                  // 적용한 기준 판
      "regions": {
        "suncheon": {
          "x": {"ecmwf_ifs025": [[12.3, 31], null, "…"]},   // 지표별 [예측 범위 내 극값, 선행시간(h)]
          "h": {"heavy_rain_advisory": {"p": 56, "mg": "caution", "g": "high", "r": [0.9, 1.25, "…"], "w": ["시작", "끝"]}},
          "a": [{"k": "heavy_rain", "l": "advisory", "n": "호우주의보", "f": "발효 시각"}],  // 발효 중 특보
          "pre": [{"k": "heavy_rain", "l": null, "n": "호우", "e": "13일 오전(06시~12시)"}]  // 예비특보
        }
      }
    }
  ]
}
```

- `h` 에는 가능성이 0%가 아닌 특보만 들어갑니다(없으면 0%·낮음).
- `mg` 는 예측값만으로 계산한 등급, `g` 는 기상청 발표(예비특보·특보)를 반영한 최종 등급입니다.
- `a`·`pre` 키 자체가 없으면 그 회차에 특보 조회가 실패한 것입니다(‘특보 없음’과 구별).
- `nat` 은 그 회차의 전국 특보입니다: `{"a": {"호우주의보": ["L1051200", …]}, "p": {"호우": […]}}` (특보구역 코드, `a` 발효 중 · `p` 예비특보).

## rail/days 파일 형식

```jsonc
{
  "schema": 1,
  "date": "2026-11-11",
  "counts": {"planned": 863, "recorded": 860, "measured": 858, "no_record": 3, "no_terminus": 2},
  "all": {"n": 858, "mean": 2.4, "median": 1.0, "max": 47, "late5": 96, "late10": 31, "late20": 8},  // 종착 지연(분)
  "clear": {…},                       // 특보 구간을 지나지 않은 열차
  "by_warning": {"호우": {…}},         // 지나간 구간에 있던 특보 종류별
  "by_line": {"전라선": {…}}, "by_class": {"KTX": {…}},
  "fields": ["trn_no", "line", "class", "origin", "dest", "plan_dep", "plan_arr", "dep_delay", "arr_delay", "warnings"],
  "trains": [["00501", "전라선", "KTX", "용산", "여수엑스포", "05:10", "08:13", 0, 3, ["호우주의보"]]]
}
```

- 지연은 `실제 − 계획`(분)입니다(일찍 도착하면 음수로 남기고, 통계에서는 0으로 셉니다). 역별 계획 시각은 공개되지 않아 시발역 출발·종착역 도착만 계산합니다.
- `warnings` 는 열차가 정차한 역의 특보구역에, 그 역을 지난 시각에 발효 중이던 특보(같은 종류면 가장 높은 단계)입니다.
