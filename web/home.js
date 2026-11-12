/* 첫 화면 — 지역을 찾으면 그 지역의 특보 가능성만 보여 준다. 지도에서 눌러도 된다.
 *
 * 자료
 *  data/latest.json   3시간마다 계산한 지역별 가능성(등급·%·예상 시간대) — 전국의 기상청 특보구역 단위
 *  live.json          몇 분마다 확인한 공식 특보·예비특보(있으면 덮어 쓴다: 발효 중이면 '발효 중', 예비특보면 '높음' 이상)
 *  geo/zones.json     특보구역 경계(지도)
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
  const RELOAD_MS = 10 * 60 * 1000, STORE_KEY = "teukbo.region", MAX_MATCHES = 8, MAX_OTHERS = 4, MAX_NOTABLE = 4;
  const NEAR_COUNT = 16, NEAR_MIN = 12, NEAR_MAX = 42;  // 고른 지역 둘레를 얼마나 넓게 보여 줄지(이웃 지역 수 기준, 지도 단위 ≈ 1.7km)
  const state = { base: null, latest: null, live: null, zones: null, region: null, query: "", paths: new Map(), centers: new Map(), labels: new Map(), svg: null, wideBox: null, liveTimer: null };

  const put = (node, ...kids) => { node.append(...kids.flat(Infinity).filter((kid) => kid != null && kid !== false)); return node; };
  const plain = (text) => String(text || "").replace(/[\s()·.]/g, "").toLowerCase();
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

  /** 이름·도 이름·특보구역 이름 어디에든 들어 있으면 찾는다. 이름이 그 글자로 시작하는 지역을 앞에 둔다. */
  function matches() {
    const q = plain(state.query);
    if (!q) return [];
    const zoneNames = (r) => (r.zones || []).map((code) => (state.zones[code] || {}).n || "").join("");
    return state.latest.regions.filter((r) => plain(`${r.name}${r.province || ""}${zoneNames(r)}`).includes(q))
      .sort((a, b) => Number(plain(b.name).startsWith(q)) - Number(plain(a.name).startsWith(q)));
  }

  function renderMatches() {
    const box = clear($("matches")), found = matches();
    if (!plain(state.query)) return;
    if (!found.length) {
      box.append(h("p", { class: "finder-none", text: `‘${state.query.trim()}’(으)로 찾은 지역이 없습니다. 시·군 이름으로 입력해 보세요(예: 순천, 파주, 서울).` }));
      return;
    }
    put(box, found.slice(0, MAX_MATCHES).map((r) => h("button", { class: "chip", type: "button", "aria-pressed": String(r.id === state.region), onclick: () => pick(r.id) }, fullName(r))),
      found.length > MAX_MATCHES ? h("span", { class: "hint", text: `외 ${found.length - MAX_MATCHES}곳 — 이름을 더 입력하세요` }) : null);
  }

  /** 지역을 고른다(null 이면 전국으로 돌아간다). */
  function pick(id, fromMap) {
    state.region = regionOf(id) ? id : null;
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
    card.append(h("p", { class: "basis", text: `앞으로 ${state.latest.horizon_hours}시간${models ? ` · 예보 ${models}개 비교` : ""} · 참고값` }));
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

  // ------------------------------------------------------------ 지도

  const pathOf = (rings) => rings.map((ring) => { let d = ""; for (let i = 0; i < ring.length; i += 2) { const [x, y] = P(ring[i], ring[i + 1]); d += `${i ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`; } return `${d}Z`; }).join("");

  /** 지도는 한 번만 그린다(경계는 바뀌지 않는다). 색·고른 표시·보는 범위는 paintMap 이 바꾼다. */
  function drawMap() {
    const holder = clear($("map")), zones = state.zones;
    state.paths.clear(); state.centers.clear(); state.labels.clear();
    const owner = new Map();  // 특보구역 코드 → 그 구역을 쓰는 첫 지역
    for (const region of state.latest.regions) for (const code of region.zones || []) if (!owner.has(code) && zones[code]) owner.set(code, region.id);
    if (!owner.size) { state.svg = null; return; }
    const svg = s("svg", { role: "group", "aria-label": "지역별 특보 가능성 지도. 지역을 누르면 그 지역의 가능성을 보여 줍니다." });
    const around = s("g", { class: "around", "aria-hidden": "true" }), covered = s("g", { class: "covered" }), labels = s("g", { class: "labels", "aria-hidden": "true" });
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
        const text = s("text", { x: cx.toFixed(1), y: cy.toFixed(1), "text-anchor": "middle", "dominant-baseline": "central" }, single ? zone.n : shortName(region.name));
        labels.append(text);
        state.labels.set(id, text);
      }
    }
    const pad = 14;
    state.wideBox = [x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad];
    svg.append(around, covered, labels);
    holder.append(svg);
    state.svg = svg;
    put(clear($("legend")), ["low", "caution", "high", "active"].map((grade) => h("li", null, h("span", { class: `swatch map-key g-${grade}` }), GRADE[grade])));
  }

  /** 고른 지역 둘레의 보는 범위: 이웃 지역 NEAR_COUNT 곳쯤이 들어오게(지역이 촘촘한 수도권은 좁게, 넓은 산지는 넓게). */
  function nearBox(id) {
    const [cx, cy] = state.centers.get(id);
    const far = [...state.centers.values()].map(([x, y]) => Math.max(Math.abs(x - cx), Math.abs(y - cy))).sort((a, b) => a - b);
    const half = Math.max(NEAR_MIN, Math.min(NEAR_MAX, (far[Math.min(NEAR_COUNT, far.length - 1)] || NEAR_MIN) + 3));
    return [cx - half, cy - half, 2 * half, 2 * half];
  }

  function paintMap() {
    if (!state.svg) return;
    const picked = state.centers.has(state.region) ? state.region : null;
    const box = picked ? nearBox(picked) : state.wideBox;
    state.svg.setAttribute("viewBox", box.map((v) => v.toFixed(1)).join(" "));
    state.svg.setAttribute("class", picked ? "near" : "wide");
    state.svg.style.setProperty("--label", `${(box[2] * (picked ? 0.034 : 0.03)).toFixed(2)}px`);  // 글자 크기를 보는 범위에 맞춘다
    for (const [id, paths] of state.paths) {
      const region = regionOf(id), top = region && region.top, grade = top ? top.grade : "low", isPicked = id === picked;
      const label = region ? `${fullName(region)}: ${top && RANK[top.grade] > 0 ? topText(top) : "가능성 낮음"}` : id;
      for (const path of paths) {
        path.setAttribute("class", `zone g-${grade}${isPicked ? " picked" : ""}`);
        path.setAttribute("aria-label", label);
        path.setAttribute("aria-pressed", String(isPicked));
        if (isPicked) path.parentNode.append(path);  // 고른 지역의 테두리가 이웃에 가리지 않게 맨 위로
      }
      const text = state.labels.get(id);
      if (text) text.setAttribute("class", isPicked ? "picked" : "");
    }
    $("back").hidden = !picked;
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
    renderHead(); renderMatches(); renderResult(); paintMap();
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
      const [latest, zones] = await Promise.all([getJson("data/latest.json"), state.zones || getJson("geo/zones.json")]);
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
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.base) { load(); } });
  setInterval(load, RELOAD_MS);
  load();
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => { /* 오프라인 지원 없이 계속 */ }));
  }
})();
