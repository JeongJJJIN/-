# data/

| 경로 | 내용 | 저장소 커밋 |
|---|---|---|
| `history/YYYY-MM-DD.json` | 그날 수집한 회차별 예측값(모델·지표별 극값), 계산 결과(가능성·등급), 실제 특보·예비특보 기록 | **예** — 지우지 않습니다 (2단계 분석용) |
| `accuracy.json` | 이력으로 집계한 적중률(사전 포착률·오경보율) | 예 |
| `latest.json` | 웹이 처음 읽는 최신 결과(전체 지역 요약) | 아니오 — 배포 전용 |
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
