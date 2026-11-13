# tools/cache — 호우 연구에 쓴 과거 강수 예보 (저장본)

`tools/rain_study.py` 가 2026-11-13 에 Open-Meteo Previous Runs API 에서 받아 둔 강수량 과거 예보입니다
(전국 226곳, 2026-09-12 ~ 11-13, 시간별, '하루 전에 예측한 값'과 '이틀 전에 예측한 값').
서비스는 이 파일을 읽지 않습니다 — 호우주의보 기준(`config/thresholds.json` 의 `types.heavy_rain.area_score`)을
어떻게 정했는지 누구나 다시 계산해 볼 수 있게 남겨 둔 것입니다.

| 파일 | 모델 |
|---|---|
| `rain_base.json.gz` | 서비스가 쓰는 4개: ECMWF IFS, GFS, ICON, JMA |
| `rain_extra.json.gz` | 더해 본 5개: JMA MSM, UKMO, GEM, Météo-France, CMA GRAPES (더해도 더 잘 잡히지 않아 쓰지 않기로 함) |

다시 계산하기(네트워크 없이):

```bash
python3 tools/rain_study.py --load tools/cache/rain_base.json.gz
python3 tools/rain_study.py --load tools/cache/rain_base.json.gz,tools/cache/rain_extra.json.gz
```

새 기간으로 다시 받기(여름철 자료가 쌓인 뒤 기준을 다시 맞출 때):

```bash
python3 tools/rain_study.py --models base --save tools/cache/rain_base.json.gz
```

출처: [Weather data by Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0). 실제 특보는 `data/rail/events` 의 기상청 발표 기록.
