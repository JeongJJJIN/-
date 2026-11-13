/* 첫 화면 — 지역을 찾으면 그 지역의 특보 가능성만 보여 준다. 지도에서 눌러도 된다.
 *
 * 자료
 *  data/latest.json   3시간마다 계산한 지역별 가능성(등급·%·예상 시간대) — 전국의 기상청 특보구역 단위
 *  live.json          몇 분마다 확인한 공식 특보·예비특보(있으면 덮어 쓴다: 발효 중이면 '발효 중', 예비특보면 '높음' 이상)
 *  geo/zones.json     특보구역 경계(지도)
 *  geo/rail.json · geo/stations.json  철도 노선과 역 위치(지도에 겹쳐 그린다. 역 이름으로도 지역을 찾는다). 없어도 화면은 동작한다
 *  data/detail/<지역>.json  그 지역의 시간별 예보 값(모델별) — 고른 지역의 '앞으로 48시간 날씨'를 적는 데 쓴다. 없으면 그 칸을 뺀다
 *  data/backtest.json 과거 검증 — 지난 예보로 가능성을 다시 계산해 실제 특보와 맞춰 본 결과(teukbo.backtest). 없으면 그 구역을 숨긴다
 * 화면의 상태는 둘뿐이다: 지역을 고르지 않았으면 전국 지도와 전국 요약, 골랐으면 그 지역의 가능성과 그 주변 지도.
 * 원칙: 받아 온 글은 늘 텍스트로 넣는다. 공식 특보가 있으면 가능성보다 먼저 보여 준다.
 */
(() => {
  "use strict";
  const { h, s, $, clear, fmtClock, fmtAgo, getJson } = window.TeukboUI;
  const Live = window.TeukboLive, P = Live.radarProject;
  const GRADE = { low: "낮음", caution: "주의", high: "높음", active: "발효 중", unknown: "확인 불가" };
  const RANK = { unknown: -1, low: 0, caution: 1, high: 2, active: 3 };
  const WEEKDAY = ["일", "월", "화", "수", "목", "금", "토"];
  const RELOAD_MS = 10 * 60 * 1000, STORE_KEY = "teukbo.region", MAX_MATCHES = 8, MAX_OTHERS = 4, MAX_NOTABLE = 4, MAX_STATIONS = 12;
  const MAX_OTHER_STATION_NAMES = 8, ZONE_FONT = 12, STATION_FONT = 11;
  const ZOOM_MIN_W = 5, LABEL_MAX_W = 120, ZOOM_STEP = 1.6;  // 가장 가까이 볼 때의 너비(지도 단위), 이름표·역을 적는 가장 넓은 범위, ＋/－ 한 번의 배율
  const RAIN_MODEL_MM = 1.0, RAIN_START_MM = 0.3, SNOW_MIN_CM = 1.0;  // 날씨 요약: 48시간 합계가 이만큼이면 '비 예상'으로, 3시간 합계가 이만큼 되면 '시작'으로 본다  // 지도 위 이름표의 글자 크기(px) — style.css 의 .map-label · .map-station-name 과 같게
  const NEAR_COUNT = 16, NEAR_MIN = 12, NEAR_MAX = 42;  // 고른 지역 둘레를 얼마나 넓게 보여 줄지(이웃 지역 수 기준, 지도 단위 ≈ 1.7km)
  const MIN_PROOF_CASES = 30, MIN_PROOF_DAYS = 3;  // 과거 검증 값을 가능성 옆에 적으려면: 표본 30회 이상, 특보가 있었던 날 3일 이상
  const state = { base: null, latest: null, live: null, zones: null, proof: null, region: null, query: "", paths: new Map(), centers: new Map(), labels: new Map(), svg: null, overlay: null, wideBox: null, liveTimer: null,
    rail: [], stations: [], stationsOf: new Map(),
    view: null,  // 지도를 직접 확대·이동했을 때의 보는 범위 [x, y, w, h]. null 이면 기본 범위(전국 또는 고른 지역 둘레)
    details: new Map() };  // `${지역 id}@${계산 시각}` → 시간별 예보(받는 중이면 "loading", 못 받았으면 null)  // stations: [{name, x, y, region}], stationsOf: 지역 id → 그 지역의 역 이름들

  const put = (node, ...kids) => { node.append(...kids.flat(Infinity).filter((kid) => kid != null && kid !== false)); return node; };
  const plain = (text) => String(text || "").replace(/[\s()·.]/g, "").toLowerCase();
  const stationName = (name) => String(name).replace(/^S(?=[가-힣])/, "");
  const shortName = (name) => String(name).replace(/\(.*\)/, "").replace(/(특별자치시|광역시|특별시|시|군)$/, "") || String(name);

  /** 한국 시각으로 '13일(금) 19시' — 보는 기기의 시간대와 상관없이 같게 */
  function dayHour(iso) {
    const t = new Date(new Date(iso).getTime() + 9 * 3600 * 1000);
    return { day: `${t.getUTCDate()}일(${WEEKDAY[t.getUTCDay()]})`, hour: `${String(t.getUTCHours()).padStart(2, "0")}시` };
  }
  function rangeText(from, to) {
    const a = dayHour(from), b = dayHour(to || from);
    if (!to || to === from) return `${a.day} ${a.hour} 무렵`;
    return a.day === b.day ? `${a.day} ${a.hour} ~ ${b.hour}` : `${a.day} ${a.hour} ~ ${b.day} ${b.hour}`;
  }
  const hazardName = (id) => (state.latest.hazards.find((x) => x.id === id) || {}).name || id;
  const regionOf = (id) => (id ? state.latest.regions.find((r) => r.id === id) || null : null);
  const fullName = (region) => `${region.name}${region.province ? ` · ${region.province}` : ""}`;

  // ------------------------------------------------------------ 지역 찾기

  const zoneNames = (r) => (r.zones || []).map((code) => (state.zones[code] || {}).n || "").join("");
  const nameHit = (r, q) => plain(`${r.name}${r.province || ""}${zoneNames(r)}`).includes(q);
  /** 그 지역 안의 역 가운데 찾는 글자가 든 역 이름('순천역'처럼 '역'을 붙여 적어도 된다). 이름이 똑같은 역을 먼저. */
  function stationHit(r, q) {
    const word = q.length > 1 && q.endsWith("역") ? q.slice(0, -1) : q;
    const names = state.stationsOf.get(r.id) || [];
    return names.find((name) => plain(name) === word) || names.find((name) => plain(name).includes(word)) || null;
  }

  /** 지역 이름·도 이름·특보구역 이름, 그리고 그 지역 안의 역 이름 어디에든 들어 있으면 찾는다. 이름이 그 글자로 시작하는 지역을 앞에 둔다. */
  function matches() {
    const q = plain(state.query);
    if (!q) return [];
    const score = (r) => (plain(r.name).startsWith(q) ? 3 : nameHit(r, q) ? 2 : plain(stationHit(r, q) || "") === (q.endsWith("역") && q.length > 1 ? q.slice(0, -1) : q) ? 1 : 0);
    return state.latest.regions.filter((r) => nameHit(r, q) || stationHit(r, q)).sort((a, b) => score(b) - score(a));
  }

  function renderMatches() {
    const box = clear($("matches")), found = matches(), q = plain(state.query);
    const viaStation = (r) => { const name = nameHit(r, q) ? null : stationHit(r, q); return name ? h("small", null, ` ${name}역`) : null; };  // 역 이름으로 찾았으면 어느 역인지 적는다
    if (!plain(state.query)) return;
    if (!found.length) {
      box.append(h("p", { class: "finder-none", text: `‘${state.query.trim()}’(으)로 찾은 지역이 없습니다. 시·군이나 역 이름으로 입력해 보세요(예: 순천, 파주, 오송역).` }));
      return;
    }
    put(box, found.slice(0, MAX_MATCHES).map((r) => h("button", { class: "chip", type: "button", "aria-pressed": String(r.id === state.region), onclick: () => pick(r.id) }, fullName(r), viaStation(r))),
      found.length > MAX_MATCHES ? h("span", { class: "hint", text: `외 ${found.length - MAX_MATCHES}곳 — 이름을 더 입력하세요` }) : null);
  }

  /** 지역을 고른다(null 이면 전국으로 돌아간다). */
  function pick(id, fromMap) {
    state.region = regionOf(id) ? id : null;
    state.view = null;  // 지역을 고르거나 전국으로 돌아가면 지도도 기본 범위로
    try { if (state.region) localStorage.setItem(STORE_KEY, state.region); else localStorage.removeItem(STORE_KEY); } catch (_) { /* 저장소를 못 써도 화면은 동작한다 */ }
    state.query = ""; $("q").value = "";
    render();
    // 지도에서 골랐는데 가능성 칸이 화면 위로 밀려나 있으면 보이게 올려 준다
    if (fromMap && $("result").getBoundingClientRect().top < 0) $("result").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ------------------------------------------------------------ 가능성

  /** 그 지역에서 보여 줄 특보 종류: 발효 중·예비특보이거나 가능성이 0보다 큰 것. 등급 높은 순, 같으면 % 높은 순. */
  function rowsOf(region) {
    return state.latest.hazards.map((hazard) => ({ hazard, sum: region.hazards[hazard.id] }))
      .filter((x) => x.sum && (x.sum.g === "active" || x.sum.official || (x.sum.p || 0) > 0))
      .sort((a, b) => RANK[b.sum.g] - RANK[a.sum.g] || (b.sum.p || 0) - (a.sum.p || 0));
  }

  const gradePill = (grade) => h("span", { class: `pill g-${grade}`, text: GRADE[grade] || GRADE.unknown });
  const percent = (sum) => (sum.g === "active" ? "발효 중" : sum.p == null ? "–" : `${sum.p}%`);
  const topText = (top) => `${hazardName(top.hazard)} ${top.grade === "active" ? "발효 중" : `${top.p == null ? "" : `${top.p}% `}${GRADE[top.grade]}`}`;

  function renderRegion(region) {
    const rows = rowsOf(region), top = rows[0], official = region.official || {};
    const card = h("div", { class: `card result g-${top ? top.sum.g : "low"}` });
    card.append(h("p", { class: "where" }, h("b", null, region.name), region.province ? h("small", null, region.province) : null));

    if (!top) {
      put(card, h("p", { class: "headline" }, h("span", { class: "big" }, "낮음")),
        h("p", { class: "when", text: `앞으로 ${state.latest.horizon_hours}시간, 모든 특보 종류의 가능성이 0%입니다.` }));
    } else {
      const sum = top.sum, active = sum.g === "active";
      put(card, h("p", { class: "hz", text: top.hazard.name }),
        h("p", { class: "headline" }, h("span", { class: active ? "big word" : "big" }, active ? "발효 중" : sum.p == null ? "–" : String(sum.p), !active && sum.p != null ? h("small", null, "%") : null), active ? null : gradePill(sum.g)),
        active ? (sum.official_from ? h("p", { class: "when", text: `${rangeText(sum.official_from)} 발효` }) : null)
          : sum.from ? h("p", { class: "when", text: `${rangeText(sum.from, sum.to)} 기준 도달 예상` }) : null);
    }

    // 공식 특보·예비특보(있으면 가능성보다 이것이 우선이다)
    const lines = [];
    for (const item of official.active || []) lines.push(h("li", null, h("span", { class: "pill g-active", text: "발효 중" }), ` ${item.name}`));
    for (const item of official.preliminary || []) lines.push(h("li", null, h("span", { class: "pill g-pre", text: "예비특보" }), ` ${item.name}${item.expected ? ` · ${item.expected}` : ""}`));
    if (lines.length) card.append(h("ul", { class: "official-lines", "aria-label": "기상청 공식 특보" }, lines));
    else if (official.available === false) card.append(h("p", { class: "basis", text: "기상청 공식 특보는 지금 확인하지 못했습니다." }));

    const others = rows.slice(1);
    if (others.length) {
      put(card, h("ul", { class: "others", "aria-label": "다른 특보 종류" }, others.slice(0, MAX_OTHERS).map((x) => h("li", null, h("span", { class: "name", text: x.hazard.name }), h("b", null, percent(x.sum)), x.sum.g === "active" ? null : gradePill(x.sum.g)))),
        others.length > MAX_OTHERS ? h("p", { class: "basis", text: `외 ${others.length - MAX_OTHERS}종` }) : null);
    }
    const models = (region.models || []).length;
    const evidence = top && top.sum.g !== "active" ? proofLine(top.hazard, top.sum.p) : null;
    if (evidence) card.append(h("p", { class: "evidence", text: evidence }));
    card.append(h("p", { class: "basis", text: `앞으로 ${state.latest.horizon_hours}시간${models ? ` · 예보 ${models}개 비교` : ""} · 참고값` }));
    const weather = renderWeather(region);
    if (weather) card.append(weather);
    const here = state.stationsOf.get(region.id) || [];
    if (here.length) card.append(h("p", { class: "stations-line" }, h("span", { class: "map-station key", "aria-hidden": "true" }), `이 지역의 역: ${here.slice(0, MAX_STATIONS).join(", ")}${here.length > MAX_STATIONS ? ` 외 ${here.length - MAX_STATIONS}곳` : ""}`));
    return card;
  }

  /** 지역을 고르지 않았을 때: 전국에서 지금 눈여겨볼 곳 */
  function renderNation() {
    const regions = state.latest.regions, count = { active: 0, high: 0, caution: 0 };
    for (const r of regions) if (r.top && count[r.top.grade] != null) count[r.top.grade] += 1;
    const worst = count.active ? "active" : count.high ? "high" : count.caution ? "caution" : "low";
    const card = h("div", { class: `card result nation g-${worst}` });
    card.append(h("p", { class: "where" }, h("b", null, "전국"), h("small", null, `${regions.length}개 지역`)));
    const parts = [["active", "발효 중"], ["high", "높음"], ["caution", "주의"]].filter(([grade]) => count[grade]);
    if (!parts.length) {
      put(card, h("p", { class: "headline" }, h("span", { class: "big" }, "낮음")), h("p", { class: "when", text: `앞으로 ${state.latest.horizon_hours}시간, 전국 모든 지역의 특보 가능성이 낮습니다.` }));
    } else {
      card.append(h("p", { class: "tally" }, parts.map(([grade, label]) => h("span", null, gradePill(grade), h("b", null, ` ${count[grade]}`), "곳"))));
      const notable = regions.filter((r) => r.top && RANK[r.top.grade] >= RANK.caution).sort((a, b) => RANK[b.top.grade] - RANK[a.top.grade] || (b.top.p || 0) - (a.top.p || 0));
      put(card, h("ul", { class: "others notable", "aria-label": "눈여겨볼 지역" }, notable.slice(0, MAX_NOTABLE).map((r) => h("li", null,
        h("button", { class: "linklike name", type: "button", onclick: () => pick(r.id) }, fullName(r)), h("b", null, r.top.grade === "active" ? "" : r.top.p == null ? "" : `${r.top.p}%`), h("span", { class: "hz-small", text: `${hazardName(r.top.hazard)}${r.top.grade === "active" ? " 발효 중" : ""}` })))),
        notable.length > MAX_NOTABLE ? h("p", { class: "basis", text: `외 ${notable.length - MAX_NOTABLE}곳 — 지도에서 색이 칠해진 곳입니다` }) : null);
    }
    card.append(h("p", { class: "basis", text: "지역 이름을 입력하거나 지도에서 눌러 보세요." }));
    return card;
  }

  function renderResult() {
    const region = regionOf(state.region);
    clear($("result")).append(region ? renderRegion(region) : renderNation());
  }


  // ------------------------------------------------------------ 날씨(앞으로 48시간)

  /** 고른 지역의 시간별 예보를 받아 둔다. 처음이면 받기 시작하고, 받으면 화면을 다시 그린다. */
  function detailOf(region) {
    const key = `${region.id}@${state.base.generated_at}`;
    if (state.details.has(key)) return state.details.get(key);
    state.details.set(key, "loading");
    getJson(`data/detail/${encodeURIComponent(region.id)}.json`).then((doc) => doc, () => null).then((doc) => {
      state.details.set(key, doc && Array.isArray(doc.times) && doc.series ? doc : null);
      if (state.region === region.id) renderResult();
    });
    return "loading";
  }

  const median = (values) => { const v = values.filter((x) => x != null && Number.isFinite(x)).sort((a, b) => a - b); return v.length ? (v.length % 2 ? v[(v.length - 1) / 2] : (v[v.length / 2 - 1] + v[v.length / 2]) / 2) : null; };
  /** '오늘 18시' '내일 06시' — 한국 시각 기준 */
  function whenText(iso) {
    const day = (t) => Math.floor((new Date(t).getTime() + 9 * 3600 * 1000) / 86400000);
    const diff = day(iso) - day(Date.now()), t = dayHour(iso);
    return `${diff === 0 ? "오늘" : diff === 1 ? "내일" : diff === 2 ? "모레" : t.day} ${t.hour}`;
  }

  /** 시간별 예보(모델별) → 날씨 요약. 값은 모델들의 중간값이고, 비·눈은 몇 개 모델이 예상하는지도 센다.
   *  rain_3h 는 '그 시각까지 3시간 동안 내린 양'이라, 3시간 간격으로 집으면 겹치지 않는 시간대별 강수량이 된다. */
  function weatherOf(doc) {
    const times = doc.times, series = doc.series || {}, n = times.length;
    const models = (key) => Object.values(series[key] || {}).filter((row) => Array.isArray(row) && row.some((v) => v != null));
    const mid = (key) => { const rows = models(key); return rows.length ? times.map((_, i) => median(rows.map((row) => row[i]))) : null; };
    const peak = (row, sign) => { let best = null; (row || []).forEach((v, i) => { if (i > 0 && v != null && (best == null || sign * v > sign * row[best])) best = i; }); return best == null ? null : { v: row[best], t: times[best] }; };
    const out = { blocks: [], rain: null, snow: null, tempMin: null, tempMax: null, wind: null, gust: null };

    const rainRows = models("i:rain_3h");
    if (rainRows.length) {
      for (let i = 3; i < n; i += 3) out.blocks.push({ from: times[i - 3], to: times[i], mm: median(rainRows.map((row) => row[i])) });
      const totals = rainRows.map((row) => { let sum = 0; for (let i = 3; i < n; i += 3) sum += row[i] || 0; return sum; });
      const wet = rainRows.map((row, k) => (totals[k] >= RAIN_MODEL_MM ? row.findIndex((v, i) => i > 0 && v != null && v >= RAIN_START_MM) : -1)).filter((i) => i > 0).sort((a, b) => a - b);
      out.rain = { models: rainRows.length, wet: wet.length, start: wet.length ? times[wet[Math.floor((wet.length - 1) / 2)]] : null, min: Math.min(...totals), max: Math.max(...totals) };
    }
    const snowRows = models("i:snow_24h"), snowMax = snowRows.map((row) => Math.max(0, ...row.slice(1).filter((v) => v != null)));
    if (snowRows.length) out.snow = { models: snowRows.length, wet: snowMax.filter((v) => v >= SNOW_MIN_CM).length, max: Math.max(0, ...snowMax) };
    const temp = mid("v:temp"), wind = mid("i:wind"), gust = mid("i:gust");
    out.tempMin = peak(temp, -1); out.tempMax = peak(temp, 1); out.wind = peak(wind, 1); out.gust = peak(gust, 1);
    return out;
  }

  const mm = (v) => (v < 1 ? (v < 0.05 ? "0" : v.toFixed(1)) : String(Math.round(v)));

  function renderWeather(region) {
    const doc = detailOf(region);
    if (!doc) return null;
    const box = h("div", { class: "weather" }, h("p", { class: "weather-title" }, "날씨", h("small", null, ` 앞으로 ${state.latest.horizon_hours}시간 · 예보들의 중간값`)));
    if (doc === "loading") return put(box, h("p", { class: "basis", text: "불러오는 중…" }));
    const w = weatherOf(doc), lines = [];
    if (w.rain) {
      const r = w.rain, amount = r.max < 0.5 ? "" : ` · ${state.latest.horizon_hours}시간 ${mm(r.min) === mm(r.max) ? `${mm(r.max)}mm` : `${mm(r.min)}~${mm(r.max)}mm`}`;
      const text = !r.wet ? "비 소식 없음" : r.wet * 2 >= r.models ? `${whenText(r.start)} 무렵부터${amount}` : `올 수도 있음${amount}`;
      lines.push(h("li", null, h("span", { class: "name", text: "비" }), h("b", { text }), r.wet ? h("small", null, `예보 ${r.models}개 중 ${r.wet}개가 비 예상`) : null));
    }
    if (w.snow && w.snow.wet) lines.push(h("li", null, h("span", { class: "name", text: "눈" }), h("b", { text: `최대 ${mm(w.snow.max)}cm` }), h("small", null, `예보 ${w.snow.models}개 중 ${w.snow.wet}개가 눈 예상(24시간 신적설)`)));
    if (w.tempMin && w.tempMax) lines.push(h("li", null, h("span", { class: "name", text: "기온" }), h("b", { text: `${Math.round(w.tempMin.v)}~${Math.round(w.tempMax.v)}℃` }), h("small", null, `최저 ${whenText(w.tempMin.t)} · 최고 ${whenText(w.tempMax.t)}`)));
    if (w.wind) lines.push(h("li", null, h("span", { class: "name", text: "바람" }), h("b", { text: `최대 ${Math.round(w.wind.v)}m/s` }), h("small", null, `${whenText(w.wind.t)}${w.gust ? ` · 순간 ${Math.round(w.gust.v)}m/s` : ""}`)));
    if (!lines.length) return null;
    box.append(h("ul", { class: "weather-lines" }, lines));

    // 시간대별 강수량(3시간씩) — 언제 오는지 한눈에. 막대가 없으면 비가 없는 시간대
    const wet = w.blocks.filter((b) => b.mm != null && b.mm >= 0.1);
    if (wet.length) {
      const top = Math.max(5, ...w.blocks.map((b) => b.mm || 0));
      box.append(h("div", { class: "rain-strip", role: "img", "aria-label": `시간대별 강수량(3시간씩): ${wet.map((b) => `${whenText(b.from)}부터 ${mm(b.mm)}mm`).join(", ")}` },
        w.blocks.map((b) => {
          const t = dayHour(b.from), value = b.mm || 0, first = t.hour === "00시" || b === w.blocks[0];
          return h("div", { class: "rain-col", title: `${t.day} ${t.hour}부터 3시간: ${mm(value)}mm` },
            h("span", { class: "rain-value", text: value >= 0.5 ? mm(value) : "" }),
            h("span", { class: "rain-bar", style: `height:${value >= 0.1 ? Math.max(3, Math.round((value / top) * 44)) : 0}px` }),
            h("span", { class: "rain-hour", text: t.hour.replace("시", "") }),
            h("span", { class: "rain-day", text: first ? t.day : "" }));
        })),
        h("p", { class: "basis", text: "막대: 3시간 동안의 강수량(mm) · 아래 숫자는 시작 시각" }));
    }
    return box;
  }

  // ------------------------------------------------------------ 과거 검증

  /** 몇 번 가운데 몇 번인지를 %로. 0번이 아닌데 0%로, 전부가 아닌데 100%로 적지 않는다. */
  function share(hits, total) {
    if (!total) return "–";
    if (!hits) return "0%";
    const p = (hits / total) * 100;
    return p < 0.1 ? "0.1% 미만" : p < 1 ? `${p.toFixed(1)}%` : p > 99 && hits < total ? "99%" : `${Math.round(p)}%`;
  }
  const rowFor = (rows, p) => (rows || []).find((row) => p >= row.from && p <= row.to) || null;
  const solid = (row) => Boolean(row && row.cases >= MIN_PROOF_CASES && row.event_days >= MIN_PROOF_DAYS);

  /** 가능성 숫자 옆에 적는 근거 한 줄: 과거에 이 구간의 가능성이 나왔을 때 실제 특보로 이어진 비율. 근거가 모자라면 적지 않는다. */
  function proofLine(hazard, p) {
    const proof = state.proof;
    if (!proof || p == null || p <= 0) return null;
    const own = rowFor(((proof.by_type || {})[hazard.type] || {}).calibration, p), all = rowFor(proof.calibration, p);
    const row = solid(own) ? own : solid(all) ? all : null;
    if (!row) return null;
    const scope = row === own ? `${(proof.by_type[hazard.type] || {}).name || ""} 특보` : "특보";
    return `과거 검증: 가능성이 ${row.from === row.to ? `${row.from}%` : `${row.from}~${row.to}%`}였던 ${row.cases.toLocaleString("ko-KR")}번 가운데 ${row.hits.toLocaleString("ko-KR")}번(${share(row.hits, row.cases)})이 48시간 안에 실제 ${scope}로 이어졌습니다.`;
  }

  function renderProof() {
    const proof = state.proof, section = $("proof"), box = clear($("proof-body"));
    section.hidden = !proof || !proof.period || !proof.period.replays;
    if (section.hidden) return;
    const day = (iso) => { const t = new Date(new Date(iso).getTime() + 9 * 3600 * 1000); return `${t.getUTCMonth() + 1}월 ${t.getUTCDate()}일`; };
    const count = (n) => Number(n || 0).toLocaleString("ko-KR");
    const lead = (proof.capture || {})["24"] || {}, alerts = (proof.false_alarm || {}).caution_or_higher || {}, high = (proof.false_alarm || {}).high || {};
    const right = (a) => (a.alerts || 0) - (a.false || 0);
    const tile = (label, value, sub) => h("div", { class: "tile" }, h("p", { class: "label", text: label }), h("p", { class: value === "–" ? "value pending" : "value", text: value }), h("p", { class: "sub", text: sub }));
    put(box,
      h("p", { class: "proof-lede", text: `${day(proof.period.from)} ~ ${day(proof.period.to)}, 전국 ${count(proof.regions)}곳을 6시간마다 그때의 예보로 다시 계산해(${count(proof.period.replays)}회) 기상청의 실제 특보 기록과 맞춰 본 결과입니다. 실제 특보 ${count(proof.events)}건 · 특보가 시작된 날 ${count(proof.event_days)}일.` }),
      h("div", { class: "tiles proof-tiles" },
        tile("하루 전에 잡아낸 비율", share(lead.captured, lead.events), lead.events ? `실제 특보 ${count(lead.events)}건 중 ${count(lead.captured)}건이 24시간 전 ‘주의’ 이상` : "해당 사례 없음"),
        tile("‘주의’ 이상이 맞은 비율", share(right(alerts), alerts.alerts), alerts.alerts ? `${count(alerts.alerts)}번 표시 중 ${count(right(alerts))}번이 48시간 안에 실제 특보` : "해당 사례 없음"),
        tile("‘높음’이 맞은 비율", share(right(high), high.alerts), high.alerts ? `${count(high.alerts)}번 표시 중 ${count(right(high))}번이 실제 특보` : "해당 사례 없음")));
    const rows = (proof.calibration || []).filter((row) => row.cases);
    if (rows.length) {
      box.append(h("div", { class: "table-scroll" }, h("table", { class: "fit proof-table" },
        h("thead", null, h("tr", null, h("th", { scope: "col", text: "표시한 가능성" }), h("th", { scope: "col", text: "횟수" }), h("th", { scope: "col" }, "실제 특보로 이어짐", h("small", null, "48시간 안")))),
        h("tbody", null, rows.map((row) => h("tr", null, h("th", { scope: "row", text: row.bucket }), h("td", { text: count(row.cases) }), h("td", null, share(row.hits, row.cases), h("small", null, `${count(row.hits)}번 · 특보일 ${row.event_days}일`))))))));
    }
    const types = Object.values(proof.by_type || {}).filter((info) => info.events).sort((a, b) => b.events - a.events);
    if (types.length) {
      box.append(h("div", { class: "table-scroll" }, h("table", { class: "fit proof-table" },
        h("thead", null, h("tr", null, h("th", { scope: "col", text: "특보 종류" }), h("th", { scope: "col", text: "실제 특보" }), h("th", { scope: "col" }, "하루 전 포착", h("small", null, "‘주의’ 이상")), h("th", { scope: "col" }, "‘주의’ 이상이 맞음"))),
        h("tbody", null, types.map((info) => {
          const c = (info.capture || {})["24"] || {}, a = (info.false_alarm || {}).caution_or_higher || {};
          // 표본이 적으면(30 미만) %를 적지 않고 횟수만 적는다 — 몇 건 안 되는 비율은 믿을 숫자가 아니다
          const cell = (hits, total, none) => (!total ? h("td", null, "–", h("small", null, none))
            : total < MIN_PROOF_CASES ? h("td", null, `${count(hits)}/${count(total)}`, h("small", null, "표본 적음"))
              : h("td", null, share(hits, total), h("small", null, `${count(hits)}/${count(total)}`)));
          return h("tr", null, h("th", { scope: "row", text: info.name }),
            h("td", null, `${count(info.events)}건`, info.event_days != null ? h("small", null, `특보일 ${info.event_days}일`) : null),
            cell(c.captured, c.events, "해당 없음"), cell(right(a), a.alerts, "표시 없음"));
        })))));
    }
    put(box, h("details", { class: "proof-notes" }, h("summary", { text: "어떻게 검증했나 · 한계" }),
      h("ul", null, (proof.notes || []).map((text) => h("li", { text })),
        (proof.types_without_events || []).length ? h("li", { text: `이 기간에 실제 특보가 없어 검증하지 못한 종류: ${proof.types_without_events.join(", ")}` }) : null,
        h("li", { text: "가능성 옆의 ‘과거 검증’ 한 줄은 표본 30회 이상·특보가 있었던 날 3일 이상일 때만 적습니다." }))));
  }

  // ------------------------------------------------------------ 지도

  const pathOf = (rings) => rings.map((ring) => { let d = ""; for (let i = 0; i < ring.length; i += 2) { const [x, y] = P(ring[i], ring[i + 1]); d += `${i ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`; } return `${d}Z`; }).join("");

  const lineOf = (flat) => { let d = ""; for (let i = 0; i < flat.length; i += 2) { const [x, y] = P(flat[i], flat[i + 1]); d += `${i ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`; } return d; };

  /** 지도는 한 번만 그린다(경계·노선은 바뀌지 않는다). 색·고른 표시·보는 범위는 paintMap 이, 이름표와 역은 layoutOverlay 가 바꾼다. */
  function drawMap() {
    const holder = clear($("map")), zones = state.zones;
    state.paths.clear(); state.centers.clear(); state.labels.clear(); state.stationsOf.clear();
    const owner = new Map();  // 특보구역 코드 → 그 구역을 쓰는 첫 지역
    for (const region of state.latest.regions) for (const code of region.zones || []) if (!owner.has(code) && zones[code]) owner.set(code, region.id);
    if (!owner.size) { state.svg = null; state.overlay = null; return; }
    const svg = s("svg", { role: "group", "aria-label": "지역별 특보 가능성 지도. 지역을 누르면 그 지역의 가능성을 보여 줍니다." });
    // 바탕: 구역 경계를 단순하게 줄이면서 생긴 구역 사이의 작은 틈으로 페이지 바탕이 비치지 않게 지도 안에 직접 깐다
    const ground = s("rect", { class: "ground", x: "-2000", y: "-2000", width: "6000", height: "6000", "aria-hidden": "true" });
    const around = s("g", { class: "around", "aria-hidden": "true" }), covered = s("g", { class: "covered" }), rails = s("g", { class: "rails", "aria-hidden": "true" });
    for (const [code, zone] of Object.entries(zones)) if (!owner.has(code)) around.append(s("path", { d: pathOf(zone.p) }));
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const [code, id] of owner) {
      const zone = zones[code], region = regionOf(id), [cx, cy] = P(zone.c[0], zone.c[1]);
      x0 = Math.min(x0, cx); y0 = Math.min(y0, cy); x1 = Math.max(x1, cx); y1 = Math.max(y1, cy);
      const path = s("path", { d: pathOf(zone.p), "data-region": id, tabindex: "0", role: "button", onclick: () => pick(id, true),
        onkeydown: (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); pick(id, true); } } });
      covered.append(path);
      if (!state.paths.has(id)) state.paths.set(id, []);
      state.paths.get(id).push(path);
      if (!state.centers.has(id)) {  // 구역이 여럿인 지역은 첫 구역에 이름을 적는다
        state.centers.set(id, [cx, cy]);
        const single = (region.zones || []).filter((c) => zones[c]).length === 1;
        state.labels.set(id, single ? zone.n : shortName(region.name));
      }
    }
    for (const line of state.rail) for (const flat of line.paths || []) rails.append(s("path", { d: lineOf(flat), class: line.fast ? "fast" : "" }));
    state.stations = [];
    for (const st of state.stationList || []) {
      const id = owner.get(st.zone), name = stationName(st.name), [x, y] = P(st.lon, st.lat);
      state.stations.push({ name, x, y, region: id || null });
      if (!id) continue;
      if (!state.stationsOf.has(id)) state.stationsOf.set(id, []);
      if (!state.stationsOf.get(id).includes(name)) state.stationsOf.get(id).push(name);
    }
    const pad = 14;
    state.wideBox = [x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad];
    svg.append(ground, around, covered, rails);
    state.overlay = h("div", { class: "map-overlay", "aria-hidden": "true" });
    const tools = h("div", { class: "map-zoom" },
      h("button", { type: "button", "aria-label": "지도 확대", title: "확대", onclick: () => zoomBy(ZOOM_STEP) }, "+"),
      h("button", { type: "button", "aria-label": "지도 축소", title: "축소", onclick: () => zoomBy(1 / ZOOM_STEP) }, "−"));
    holder.append(svg, state.overlay, tools);
    state.svg = svg;
    bindMapGestures(svg);
    put(clear($("legend")), ["low", "caution", "high", "active"].map((grade) => h("li", null, h("span", { class: `swatch map-key g-${grade}` }), GRADE[grade])),
      state.stations.length ? h("li", null, h("span", { class: "map-station key" }), "역") : null);
  }

  /** 지역 이름표와 역(점·이름)을 지도 위에 얹는다 — 지역을 골라 가까이 볼 때만(전국 지도에는 200곳이 넘어 적지 않는다).
   *  지도 그림(SVG) 안의 글자가 아니라 보통 글자로 얹는다: 브라우저가 화면을 강제로 어둡게 바꿔도(일부 휴대폰) 글자와 바탕이 함께 바뀌어 읽힌다.
   *  겹치지 않게: 고른 지역 이름 → 다른 지역 이름 → 고른 지역의 역 이름 → 다른 역 이름 순으로, 자리가 있으면 적는다. 역의 점은 모두 찍는다. */
  function layoutOverlay() {
    const layer = state.overlay;
    if (!layer || !state.svg) return;
    clear(layer);
    const picked = state.centers.has(state.region) ? state.region : null;
    if (currentBox()[2] > LABEL_MAX_W) return;  // 넓게 볼 때는 적지 않는다
    const ctm = state.svg.getScreenCTM(), host = layer.getBoundingClientRect();
    if (!ctm || !host.width) return;
    const at = ([x, y]) => [ctm.a * x + ctm.e - host.left, ctm.d * y + ctm.f - host.top];
    const inside = ([x, y], margin) => x >= margin && y >= margin && x <= host.width - margin && y <= host.height - margin;
    const widthOf = (text, size) => [...text].reduce((w, ch) => w + (ch.charCodeAt(0) > 0x2e80 ? 1 : 0.62), 0) * size + 8;
    const taken = [];  // 이미 적은 이름표의 [가운데 x, 가운데 y, 너비, 높이]
    const free = (x, y, w, hgt) => x - w / 2 >= 0 && x + w / 2 <= host.width && y - hgt / 2 >= 0 && y + hgt / 2 <= host.height
      && !taken.some((b) => Math.abs(b[0] - x) < (b[2] + w) / 2 && Math.abs(b[1] - y) < (b[3] + hgt) / 2);
    const mark = (cls, text, x, y) => h("span", { class: cls, style: `left:${x.toFixed(1)}px;top:${y.toFixed(1)}px`, text });
    const dots = [], stationNames = [], zoneNames = [];  // 쌓는 순서: 역의 점 → 역 이름 → 지역 이름(맨 위)

    const focus = picked ? at(state.centers.get(picked)) : null;
    const [px, py] = focus && inside(focus, 0) ? focus : [host.width / 2, host.height / 2];  // 가까운 것부터 적는 기준점: 고른 지역(화면 밖이면 화면 가운데)
    const near = (a, b) => Math.hypot(a[0] - px, a[1] - py) - Math.hypot(b[0] - px, b[1] - py);
    const zoneMarks = [...state.centers].map(([id, c]) => ({ id, text: state.labels.get(id), xy: at(c) })).filter((m) => m.text && (m.id === picked || inside(m.xy, 4))).sort((a, b) => near(a.xy, b.xy));
    const stops = state.stations.map((st) => ({ st, mine: st.region === picked, xy: at([st.x, st.y]) })).filter((m) => inside(m.xy, 2)).sort((a, b) => near(a.xy, b.xy));
    const zoneName = (m, always) => {
      const w = widthOf(m.text, ZONE_FONT), [x, y] = m.xy;
      if (!always && !free(x, y, w, ZONE_FONT + 6)) return;
      taken.push([x, y, w, ZONE_FONT + 6]);
      zoneNames.push(mark(m.id === picked ? "map-label picked" : "map-label", m.text, x, y));
    };
    const stationNameAt = (m) => {
      const [x, y] = m.xy, text = `${m.st.name}역`, w = widthOf(text, STATION_FONT), hgt = STATION_FONT + 5, dx = 7 + w / 2, dy = 6 + hgt / 2;
      // 이름 자리: 점의 오른쪽 → 왼쪽 → 아래 → 위 → 대각선
      const spot = [[x + dx, y], [x - dx, y], [x, y + dy], [x, y - dy], [x + dx, y + dy], [x + dx, y - dy], [x - dx, y + dy], [x - dx, y - dy]].find(([lx, ly]) => free(lx, ly, w, hgt));
      if (!spot) return false;
      taken.push([spot[0], spot[1], w, hgt]);
      stationNames.push(mark(m.mine ? "map-station-name mine" : "map-station-name", text, spot[0], spot[1]));
      return true;
    };
    // 자리 잡는 순서: 고른 지역 이름 → 고른 지역의 역(점·이름) → 다른 지역 이름 → 다른 역(점·이름).
    // 지역 이름은 다른 지역의 역 점 위에 덮어 적는다(수도권처럼 역이 촘촘한 곳에서 지역 이름이 빠지지 않게).
    const dot = (m) => {
      taken.push([m.xy[0], m.xy[1], m.mine ? 11 : 7, m.mine ? 11 : 7]);  // 역의 점 위에는 역 이름을 적지 않는다
      dots.push(mark(m.mine ? "map-station mine" : "map-station", "", m.xy[0], m.xy[1]));
    };
    zoneMarks.filter((m) => m.id === picked).forEach((m) => zoneName(m, true));
    stops.filter((m) => m.mine).forEach(dot);
    stops.filter((m) => m.mine).forEach(stationNameAt);
    zoneMarks.filter((m) => m.id !== picked).forEach((m) => zoneName(m, false));
    stops.filter((m) => !m.mine).forEach(dot);
    let others = 0;  // 다른 지역의 역 이름은 가까운 몇 곳만
    for (const m of stops) if (!m.mine && others < MAX_OTHER_STATION_NAMES && stationNameAt(m)) others += 1;
    layer.append(...dots, ...stationNames, ...zoneNames);
  }

  /** 고른 지역 둘레의 보는 범위: 이웃 지역 NEAR_COUNT 곳쯤이 들어오게(지역이 촘촘한 수도권은 좁게, 넓은 산지는 넓게). */
  function nearBox(id) {
    const [cx, cy] = state.centers.get(id);
    const far = [...state.centers.values()].map(([x, y]) => Math.max(Math.abs(x - cx), Math.abs(y - cy))).sort((a, b) => a - b);
    const half = Math.max(NEAR_MIN, Math.min(NEAR_MAX, (far[Math.min(NEAR_COUNT, far.length - 1)] || NEAR_MIN) + 3));
    return [cx - half, cy - half, 2 * half, 2 * half];
  }

  const defaultBox = () => (state.centers.has(state.region) ? nearBox(state.region) : state.wideBox);
  const currentBox = () => state.view || defaultBox();

  /** 보는 범위만 바꾼다(확대·이동할 때마다 불린다). 넓게 보면 이름표·역을 숨기고 선을 가늘게 한다. */
  function applyView() {
    if (!state.svg) return;
    const box = currentBox();
    state.svg.setAttribute("viewBox", box.map((v) => v.toFixed(2)).join(" "));
    state.svg.setAttribute("class", `${box[2] > LABEL_MAX_W ? "wide" : "near"}${state.view ? " zoomed" : ""}`);
    $("back").hidden = !state.view && !state.centers.has(state.region);
    // 이름표·역의 자리는 화면을 한 번 그릴 때 한 번만 다시 잡는다(손가락으로 확대·이동하는 동안 버벅이지 않게)
    if (!state.overlayFrame) state.overlayFrame = requestAnimationFrame(() => { state.overlayFrame = 0; layoutOverlay(); });
  }

  /** 보는 범위를 정한다: 너무 가까이·너무 멀리 가지 않게, 지도 밖으로 벗어나지 않게. */
  function setView(box) {
    const wide = state.wideBox, ratio = box[3] / box[2];
    const w = Math.max(ZOOM_MIN_W, Math.min(box[2], Math.max(wide[2], wide[3] / ratio))), hgt = w * ratio;
    const cx = Math.max(wide[0], Math.min(wide[0] + wide[2], box[0] + box[2] / 2)), cy = Math.max(wide[1], Math.min(wide[1] + wide[3], box[1] + box[3] / 2));
    state.view = [cx - w / 2, cy - hgt / 2, w, hgt];
    applyView();
  }

  /** 배율만큼 확대(1보다 크면)·축소한다. 화면의 한 점(손가락·마우스 위치)을 주면 그 점이 제자리에 있게. */
  function zoomBy(factor, clientX, clientY) {
    if (!state.svg) return;
    const rect = state.svg.getBoundingClientRect(), [x, y, w, hgt] = currentBox();
    const fx = clientX == null ? 0.5 : (clientX - rect.left) / rect.width, fy = clientY == null ? 0.5 : (clientY - rect.top) / rect.height;
    setView([x + (w - w / factor) * fx, y + (hgt - hgt / factor) * fy, w / factor, hgt / factor]);
  }

  /** 지도 손짓: 두 손가락으로 벌리고 오므려 확대·축소(함께 움직이면 이동), 확대한 뒤에는 한 손가락으로 끌어 이동.
   *  확대하기 전의 한 손가락은 페이지를 넘기는 데 쓰게 둔다(지도가 화면을 꽉 채워도 아래로 내려갈 수 있게). 마우스는 끌어서 이동, Ctrl+휠로 확대. */
  function bindMapGestures(svg) {
    const pointers = new Map();
    let start = null, moved = 0;
    const begin = () => { start = { box: [...currentBox()], pts: [...pointers.values()].map((p) => ({ ...p })) }; };
    svg.addEventListener("pointerdown", (event) => {
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pointers.size === 1) moved = 0;
      begin();
    });
    window.addEventListener("pointermove", (event) => {
      if (!pointers.has(event.pointerId) || !start) return;
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      const rect = svg.getBoundingClientRect(), scale = start.box[2] / rect.width, pts = [...pointers.values()];
      if (pts.length === 1 && start.pts.length === 1) {
        if (event.pointerType === "touch" && !state.view) return;  // 확대 전의 한 손가락은 페이지 넘기기
        const dx = pts[0].x - start.pts[0].x, dy = pts[0].y - start.pts[0].y;
        moved = Math.max(moved, Math.hypot(dx, dy));
        if (moved > 4) setView([start.box[0] - dx * scale, start.box[1] - dy * scale, start.box[2], start.box[3]]);
      } else if (pts.length >= 2 && start.pts.length >= 2) {
        moved = 99;
        const d0 = Math.hypot(start.pts[0].x - start.pts[1].x, start.pts[0].y - start.pts[1].y) || 1, d1 = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
        const w = start.box[2] / (d1 / d0), hgt = start.box[3] / (d1 / d0), s2 = w / rect.width;
        const mx0 = (start.pts[0].x + start.pts[1].x) / 2, my0 = (start.pts[0].y + start.pts[1].y) / 2, mx1 = (pts[0].x + pts[1].x) / 2, my1 = (pts[0].y + pts[1].y) / 2;
        // 처음 두 손가락 가운데가 가리키던 지점이 지금 가운데 아래에 오도록
        const gx = start.box[0] + (mx0 - rect.left) * scale, gy = start.box[1] + (my0 - rect.top) * scale;
        setView([gx - (mx1 - rect.left) * s2, gy - (my1 - rect.top) * s2, w, hgt]);
      }
    });
    const end = (event) => { if (pointers.delete(event.pointerId)) begin(); };
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    // 두 손가락 손짓과 확대한 뒤의 끌기는 브라우저가 페이지를 움직이는 데 쓰지 않게 한다
    svg.addEventListener("touchmove", (event) => { if (event.touches.length >= 2 || state.view) event.preventDefault(); }, { passive: false });
    svg.addEventListener("wheel", (event) => {  // 그냥 휠은 페이지를 넘기는 데 쓰게 두고, Ctrl+휠(트랙패드 오므리기 포함)일 때만 확대
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      zoomBy(event.deltaY < 0 ? 1.25 : 0.8, event.clientX, event.clientY);
    }, { passive: false });
    svg.addEventListener("click", (event) => { if (moved > 6) { event.stopPropagation(); event.preventDefault(); } }, true);  // 끌어서 옮긴 것은 누른 것으로 치지 않는다
    svg.addEventListener("keydown", (event) => { if (event.key === "+" || event.key === "=") zoomBy(ZOOM_STEP); else if (event.key === "-") zoomBy(1 / ZOOM_STEP); });
  }

  function paintMap() {
    if (!state.svg) return;
    const picked = state.centers.has(state.region) ? state.region : null;
    for (const [id, paths] of state.paths) {
      const region = regionOf(id), top = region && region.top, grade = top ? top.grade : "low", isPicked = id === picked;
      const label = region ? `${fullName(region)}: ${top && RANK[top.grade] > 0 ? topText(top) : "가능성 낮음"}` : id;
      for (const path of paths) {
        path.setAttribute("class", `zone g-${grade}${isPicked ? " picked" : ""}`);
        path.setAttribute("aria-label", label);
        path.setAttribute("aria-pressed", String(isPicked));
        if (isPicked && path.parentNode.lastChild !== path) path.parentNode.append(path);  // 고른 지역의 테두리가 이웃에 가리지 않게 맨 위로
      }
    }
    applyView();
  }

  // ------------------------------------------------------------ 머리말 · 전체

  function renderHead() {
    const base = state.base, alerts = clear($("alerts"));
    const liveCfg = base.live || {}, usable = state.live && Live.fresh(state.live.warnings, liveCfg.stale_after_minutes || 30);
    const checked = usable ? fmtClock(state.live.warnings.checked_at).split(" ").pop() : null;
    $("updated").textContent = `계산 ${fmtClock(base.generated_at)} · ${fmtAgo(base.generated_at)}${checked ? ` · 특보 확인 ${checked}` : ""}`;
    if (base.demo) alerts.append(h("div", { class: "banner demo", role: "status" }, h("div", null, "데모 화면입니다. 표시된 수치는 실제 예보가 아닌 가상 자료입니다.")));
    const hours = (Date.now() - new Date(base.generated_at).getTime()) / 3600000;
    if (hours > (base.stale_after_hours || 7)) alerts.append(h("div", { class: "banner", role: "status" }, h("div", null, `자료가 ${Math.floor(hours)}시간 전 것입니다. 최신 예보가 반영되지 않았을 수 있습니다.`)));
  }

  function render() {
    if (!state.latest) return;
    if (!regionOf(state.region)) state.region = null;
    renderHead(); renderMatches(); renderResult(); paintMap(); renderProof();
  }

  function applyLive() {
    state.latest = Live.applyToLatest(state.base, state.live, (state.base.live || {}).stale_after_minutes || 30);
    render();
  }

  async function refreshLive() {
    const config = state.base && state.base.live;
    if (!config || !config.url) return;
    try { state.live = (await Live.load(config)) || state.live; } catch (_) { /* 실시간 자료를 못 받아도 3시간 자료로 보여 준다 */ }
    applyLive();
  }

  async function load() {
    try {
      const [latest, zones, proof, rail, stations] = await Promise.all([getJson("data/latest.json"), state.zones || getJson("geo/zones.json"), getJson("data/backtest.json").catch(() => null),
        state.rail.length ? state.rail : getJson("geo/rail.json").catch(() => []), state.stationList || getJson("geo/stations.json").catch(() => [])]);
      state.proof = proof || state.proof;
      state.rail = Array.isArray(rail) ? rail : []; state.stationList = Array.isArray(stations) ? stations : [];
      const first = !state.base;
      state.base = latest; state.zones = zones;
      state.latest = Live.applyToLatest(latest, state.live, (latest.live || {}).stale_after_minutes || 30);
      if (first) {
        try { state.region = localStorage.getItem(STORE_KEY); } catch (_) { /* 무시 */ }
        drawMap();
      }
      render();
      refreshLive();
      const poll = Math.max(30, Number((latest.live || {}).poll_seconds) || 120) * 1000;
      if (!state.liveTimer && latest.live && latest.live.url) state.liveTimer = setInterval(refreshLive, poll);
    } catch (error) {
      if (!state.base) {
        $("updated").textContent = "";
        clear($("result")).append(h("div", { class: "panel" }, h("p", { class: "empty", text: "자료를 불러오지 못했습니다. 잠시 뒤 다시 열어 주세요." })));
      }
    }
  }

  $("q").addEventListener("input", (event) => { state.query = event.target.value; if (state.latest) renderMatches(); });
  $("q").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !state.latest) return;
    const found = matches();
    if (found.length) { event.preventDefault(); pick(found[0].id); event.target.blur(); }
  });
  $("back").addEventListener("click", () => pick(null));
  // 화면 크기가 바뀌면(휴대폰을 돌리거나 창을 줄이면) 이름표·역의 자리를 다시 잡는다
  if (window.ResizeObserver) new ResizeObserver(() => layoutOverlay()).observe($("map")); else window.addEventListener("resize", layoutOverlay);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.base) { load(); } });
  setInterval(load, RELOAD_MS);
  load();
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => { /* 오프라인 지원 없이 계속 */ }));
  }
})();
